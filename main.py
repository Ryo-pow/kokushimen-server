import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, status
from fastapi.security import APIKeyHeader
import os
import time
from faster_whisper import WhisperModel
import google.generativeai as genai
from dotenv import load_dotenv
import httpx
from typing import Union, Dict, Set
import random
import json
import numpy as np

# .envファイルから環境変数を読み込む
load_dotenv()

app = FastAPI()

# --- 認証 ---
# 環境変数から期待するAPIキーを取得
EXPECTED_API_KEY = os.getenv("SERVER_AUTH_TOKEN", "dev-token-secret")
# "Authorization" ヘッダーからAPIキーを読み取るための設定
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)

async def get_api_key(auth_header: str = Depends(api_key_header)):
    """ヘッダーを検証し、トークンが不正ならWebSocket接続を拒否する"""
    if not auth_header:
        return None
    
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
        
    if parts[1] == EXPECTED_API_KEY:
        return parts[1]
    return None

# --- 接続管理 ---
class ConnectionManager:
    def __init__(self):
        self.senders: Dict[str, WebSocket] = {}
        self.playbacks: Set[WebSocket] = set()
        self.audio_buffers: Dict[str, bytearray] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()

    def disconnect(self, websocket: WebSocket):
        sender_to_remove = None
        for stream_id, ws in self.senders.items():
            if ws == websocket:
                sender_to_remove = stream_id
                break
        if sender_to_remove:
            del self.senders[sender_to_remove]
            if sender_to_remove in self.audio_buffers:
                del self.audio_buffers[sender_to_remove]
        
        if websocket in self.playbacks:
            self.playbacks.remove(websocket)

    def add_sender(self, stream_id: str, websocket: WebSocket):
        self.senders[stream_id] = websocket
        self.audio_buffers[stream_id] = bytearray()
        print(f"✅ 送信クライアント '{stream_id}' が登録されました。")

    def add_playback(self, websocket: WebSocket):
        self.playbacks.add(websocket)
        print("✅ 再生クライアントが登録されました。")

    def append_audio_data(self, stream_id: str, data: bytes):
        if stream_id in self.audio_buffers:
            self.audio_buffers[stream_id].extend(data)

    def get_and_clear_audio_data(self, stream_id: str) -> bytes:
        if stream_id in self.audio_buffers:
            data = bytes(self.audio_buffers[stream_id])
            self.audio_buffers[stream_id].clear()
            return data
        return b''

    async def broadcast_audio_chunks(self, audio_data: bytes, chunk_size: int):
        """再生クライアント全員に音声データをチャンクで送信"""
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            for ws in self.playbacks:
                try:
                    await ws.send_bytes(chunk)
                except Exception as e:
                    print(f"再生クライアントへのチャンク送信エラー: {e}")
            # 実際の時間経過に合わせて少し待つことで、クライアントのバッファ溢れを防ぐ
            await asyncio.sleep(0.18) # 200msチャンクより少し短く設定

    async def broadcast_json(self, json_data: dict):
        """再生クライアント全員にJSONデータを送信"""
        for ws in self.playbacks:
            try:
                await ws.send_json(json_data)
            except Exception as e:
                print(f"再生クライアントへのJSON送信エラー: {e}")

manager = ConnectionManager()

# --- 設定 ---
AVAILABLE_SPEAKER_IDS = [2, 3, 1, 8, 10, 14]
VOICEVOX_BASE_URL = "http://127.0.0.1:50021"
# 16kHz, 16-bit, mono の 200ms 分のバイト数
TTS_CHUNK_SIZE = 16000 * 2 * 1 * 200 // 1000 

# --- モデルとAPIの準備 ---
try:
    # ▼▼▼ 改善点1: faster-whisper に変更 ▼▼▼
    # CPU: "int8", GPU: "float16" or "int8_float16"
    model = WhisperModel("base", device="cpu", compute_type="int8")
    print("✅ faster-whisperモデルのロード完了。")
except Exception as e:
    model = None
    print(f"❌ faster-whisperモデルのロード失敗: {e}")

try:
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ValueError("環境変数にGEMINI_API_KEYが設定されていません。")
    genai.configure(api_key=gemini_api_key)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    print("✅ Geminiモデルの準備完了。")
except Exception as e:
    gemini_model = None
    print(f"❌ Geminiモデルの準備失敗: {e}")

# --- 関数定義 ---
async def generate_voicevox_audio(text: str, speaker_id: int) -> Union[bytes, None]:
    async with httpx.AsyncClient() as client:
        try:
            params = {"text": text, "speaker": speaker_id}
            res_query = await client.post(f"{VOICEVOX_BASE_URL}/audio_query", params=params)
            res_query.raise_for_status()
            audio_query = res_query.json()
            
            headers = {"Content-Type": "application/json"}
            res_synth = await client.post(
                f"{VOICEVOX_BASE_URL}/synthesis",
                params={"speaker": speaker_id},
                json=audio_query,
                headers=headers,
                timeout=20.0
            )
            res_synth.raise_for_status()
            
            return res_synth.content
        except Exception as e:
            print(f"❌ VOICEVOX API呼び出しでエラー: {e}")
            return None

def pcm_s16le_to_float32(audio_data: bytes) -> np.ndarray:
    """生PCM(s16le)データをWhisperが処理できるfloat32形式に変換"""
    return np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

# --- WebSocketエンドポイント ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Depends(get_api_key)):
    # ▼▼▼ 改善点4: 認証処理 ▼▼▼
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        print("❌ 認証トークンが無効なため接続を拒否しました。")
        return

    await manager.connect(websocket)
    stream_id = None
    is_sender = False

    try:
        while True:
            data = await websocket.receive()

            if isinstance(data, str) or (isinstance(data, bytes) and data.startswith(b'{')):
                try:
                    msg_text = data.decode('utf-8') if isinstance(data, bytes) else data
                    msg_json = json.loads(msg_text)
                    msg_type = msg_json.get("type")

                    if msg_type == "hello":
                        role = msg_json.get("role")
                        if role == "sender":
                            stream_id = msg_json.get("stream_id")
                            if stream_id:
                                manager.add_sender(stream_id, websocket)
                                is_sender = True
                        elif role == "playback":
                            manager.add_playback(websocket)
                    
                    elif msg_type == "stop" and is_sender and stream_id:
                        print(f"🎤 マイク '{stream_id}' から発話終了通知を受信。")
                        
                        full_audio_data = manager.get_and_clear_audio_data(stream_id)
                        
                        if len(full_audio_data) < 1600: # 100ms未満の音声は無視
                            print("音声データが短すぎるためスキップします。")
                            continue

                        if not model or not gemini_model:
                            print("モデルが準備できていないため処理をスキップします。")
                            continue
                        
                        # ▼▼▼ 改善点3: パフォーマンス計測 ▼▼▼
                        t_start = time.time()

                        # 1. Whisperで文字起こし
                        audio_np = pcm_s16le_to_float32(full_audio_data)
                        segments, _ = model.transcribe(audio_np, beam_size=5, language="ja", vad_filter=True)
                        transcribed_text = "".join([s.text for s in segments]).strip()
                        t_asr = time.time()

                        if not transcribed_text:
                            print("文字起こし結果が空でした。")
                            continue
                        print(f"✨ 文字起こし結果: {transcribed_text}")

                        # 2. Geminiで応答生成
                        prompt = f"ユーザーの発言「{transcribed_text}」に対して、親切で簡潔なアシスタントとして応答してください。"
                        response = gemini_model.generate_content(prompt)
                        ai_response_text = response.text
                        t_llm = time.time()
                        print(f"💬 Geminiからの応答: {ai_response_text}")

                        # 3. VOICEVOXで音声合成
                        selected_speaker_id = random.choice(AVAILABLE_SPEAKER_IDS)
                        voice_data = await generate_voicevox_audio(ai_response_text, speaker_id=selected_speaker_id)
                        t_tts = time.time()

                        # 4. 再生クライアントに音声データをブロードキャスト
                        if voice_data:
                            # ▼▼▼ 改善点2: 音声をチャンクで分割送信 ▼▼▼
                            print(f"🔊 全再生クライアントに音声データ ({len(voice_data)} bytes) を分割送信します。")
                            await manager.broadcast_audio_chunks(voice_data, TTS_CHUNK_SIZE)
                            await manager.broadcast_json({"type": "tts_done"})
                            print("ℹ️  ミュート解除のための完了通知を送信しました。")
                        
                        t_end = time.time()
                        # パフォーマンスログを出力
                        print(f"⏱️  パフォーマンス: [ASR: {t_asr - t_start:.2f}s] [LLM: {t_llm - t_asr:.2f}s] [TTS: {t_tts - t_llm:.2f}s] [Total: {t_end - t_start:.2f}s]")

                except Exception as e:
                    print(f"制御メッセージ処理中にエラー: {e}")

            elif isinstance(data, bytes) and is_sender and stream_id:
                manager.append_audio_data(stream_id, data)

    except WebSocketDisconnect:
        print(f"クライアントが切断しました。")
    finally:
        manager.disconnect(websocket)
        print("接続をクリーンアップしました。")

if __name__ == "__main__":
    import asyncio
    uvicorn.run(app, host="0.0.0.0", port=8000)
