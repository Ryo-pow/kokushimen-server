import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, status
from fastapi.security import APIKeyHeader
import os
import time
import json
import random
import asyncio
from typing import Union, Dict, Set

# 外部ライブラリ
import numpy as np
from faster_whisper import WhisperModel
import google.generativeai as genai
from dotenv import load_dotenv
import httpx

# .envファイルから環境変数を読み込む
load_dotenv()

# --- アプリケーションの初期化 ---
app = FastAPI()

# --- 認証設定 ---
# 環境変数から期待するAPIキーを取得
EXPECTED_API_KEY = os.getenv("SERVER_AUTH_TOKEN", "dev-token-secret")
# "Authorization" ヘッダーからAPIキーを読み取るための設定
api_key_header = APIKeyHeader(name="Authorization", auto_error=False)

async def get_api_key(auth_header: str = Depends(api_key_header)):
    """ヘッダーを検証し、トークンが不正ならWebSocket接続を拒否する"""
    if not auth_header or len(auth_header.split()) != 2:
        return None
    
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() == "bearer" and token == EXPECTED_API_KEY:
        return token
    return None

# --- 接続管理 ---
class ConnectionManager:
    """WebSocket接続を管理するクラス"""
    def __init__(self):
        self.senders: Dict[str, WebSocket] = {}
        self.playbacks: Set[WebSocket] = set()
        self.audio_buffers: Dict[str, bytearray] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()

    def disconnect(self, websocket: WebSocket):
        sender_to_remove = next((stream_id for stream_id, ws in self.senders.items() if ws == websocket), None)
        if sender_to_remove:
            del self.senders[sender_to_remove]
            if sender_to_remove in self.audio_buffers:
                del self.audio_buffers[sender_to_remove]
        
        self.playbacks.discard(websocket)

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
        data = self.audio_buffers.get(stream_id, b'')
        if data:
            self.audio_buffers[stream_id].clear()
        return bytes(data)

    async def broadcast_audio_chunks(self, audio_data: bytes, chunk_size: int):
        """再生クライアント全員に音声データをチャンクで送信"""
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            # チャンクごとに非同期タスクを作成して同時に送信
            tasks = [ws.send_bytes(chunk) for ws in self.playbacks]
            await asyncio.gather(*tasks, return_exceptions=True)
            # チャンクの再生時間に合わせて待機 (16kHz/16bit/mono)
            await asyncio.sleep(chunk_size / (16000 * 2))

    async def broadcast_json(self, json_data: dict):
        """再生クライアント全員にJSONデータを送信"""
        tasks = [ws.send_json(json_data) for ws in self.playbacks]
        await asyncio.gather(*tasks, return_exceptions=True)

manager = ConnectionManager()

# --- 設定 ---
VOICEVOX_BASE_URL = "http://127.0.0.1:50021"
# 16kHz, 16-bit, mono の 200ms 分のバイト数
TTS_CHUNK_SIZE = 16000 * 2 * 1 * 200 // 1000 

# --- モデルとAPIの準備 ---
try:
    # CPU: "int8", GPU: "float16" or "int8_float16"
    whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    print("✅ faster-whisperモデルのロード完了。")
except Exception as e:
    whisper_model = None
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

# --- ヘルパー関数 ---
async def generate_voicevox_audio(text: str, speaker_id: int) -> Union[bytes, None]:
    """VOICEVOX APIを呼び出して音声を生成する"""
    async with httpx.AsyncClient() as client:
        try:
            # 1. audio_queryの作成
            params = {"text": text, "speaker": speaker_id}
            res_query = await client.post(f"{VOICEVOX_BASE_URL}/audio_query", params=params, timeout=10.0)
            res_query.raise_for_status()
            audio_query = res_query.json()
            
            # 2. synthesisの実行
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
        except httpx.RequestError as e:
            print(f"❌ VOICEVOX API呼び出しでエラー: {e}")
            return None

def pcm_s16le_to_float32(audio_data: bytes) -> np.ndarray:
    """生PCM(s16le)データをWhisperが処理できるfloat32形式に変換"""
    return np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

# --- WebSocketエンドポイント ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Depends(get_api_key)):
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

            # JSON形式の制御メッセージを処理
            if "text" in data:
                try:
                    msg_json = json.loads(data["text"])
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
                    
                    # 発話終了の通知を受け取ったら、一連の処理を開始
                    elif msg_type == "stop" and is_sender and stream_id:
                        print(f"🎤 マイク '{stream_id}' から発話終了通知を受信。")
                        
                        full_audio_data = manager.get_and_clear_audio_data(stream_id)
                        
                        if len(full_audio_data) < 1600: # 100ms未満の音声は無視
                            print("音声データが短すぎるためスキップします。")
                            continue

                        if not whisper_model or not gemini_model:
                            print("モデルが準備できていないため処理をスキップします。")
                            continue
                        
                        t_start = time.time()

                        # 1. Whisperで文字起こし
                        audio_np = pcm_s16le_to_float32(full_audio_data)
                        segments, _ = whisper_model.transcribe(audio_np, beam_size=5, language="ja", vad_filter=True)
                        transcribed_text = "".join([s.text for s in segments]).strip()
                        t_asr = time.time()

                        if not transcribed_text:
                            print("文字起こし結果が空でした。")
                            continue
                        print(f"✨ 文字起こし結果: {transcribed_text}")

                        # 2. Geminiで応答生成と感情分析
                        prompt = f"""
                        ユーザーの発言「{transcribed_text}」を分析してください。
                        以下の2つの項目を含むJSON形式で、結果だけを出力してください。

                        1. "emotion": 発言から最も強く感じられる感情を「喜び」「怒り」「悲しみ」「平常」のいずれか一つで示してください。
                        2. "reply": 親切で簡潔なアシスタントとしての応答メッセージを作成してください。
                        """
                        response = await gemini_model.generate_content_async(prompt)
                        ai_response_json_str = response.text

                        try:
                            # Geminiの出力からJSON部分だけを安全に抜き出す
                            if "```json" in ai_response_json_str:
                                ai_response_json_str = ai_response_json_str.split('```json\n')[1].split('\n```')[0]
                            
                            ai_response_data = json.loads(ai_response_json_str)
                            ai_emotion = ai_response_data.get("emotion", "平常")
                            ai_response_text = ai_response_data.get("reply", "すみません、うまく聞き取れませんでした。")
                        except Exception as e:
                            print(f"❌ Geminiの応答(JSON)の解析に失敗しました: {e}")
                            ai_emotion = "平常"
                            ai_response_text = "すみません、少し調子が悪いようです。"

                        t_llm = time.time()
                        print(f"😊 感情分析結果: {ai_emotion}")
                        print(f"💬 Geminiからの応答: {ai_response_text}")

                        # 3. VOICEVOXで音声合成 (感情に応じて話者を変更)
                        speaker_map = {"喜び": 3, "悲しみ": 1, "怒り": 8, "平常": 2} # 例: ずんだもん(3), 四国めたん(1), 玄野武宏(8), 雨晴はう(2)
                        selected_speaker_id = speaker_map.get(ai_emotion, speaker_map["平常"])
                        print(f"🗣️  話者ID '{selected_speaker_id}' を選択しました。")
                        
                        voice_data = await generate_voicevox_audio(ai_response_text, speaker_id=selected_speaker_id)
                        t_tts = time.time()

                        # 4. 再生クライアントに音声データをブロードキャスト
                        if voice_data:
                            print(f"🔊 全再生クライアントに音声データ ({len(voice_data)} bytes) を分割送信します。")
                            await manager.broadcast_audio_chunks(voice_data, TTS_CHUNK_SIZE)
                            await manager.broadcast_json({"type": "tts_done"})
                            print("ℹ️  ミュート解除のための完了通知を送信しました。")
                        
                        t_end = time.time()
                        print(f"⏱️  パフォーマンス: [ASR: {t_asr - t_start:.2f}s] [LLM: {t_llm - t_asr:.2f}s] [TTS: {t_tts - t_llm:.2f}s] [Total: {t_end - t_start:.2f}s]")

                except Exception as e:
                    print(f"制御メッセージ処理中にエラー: {e}")

            # バイナリ形式の音声データをバッファに追加
            elif "bytes" in data and is_sender and stream_id:
                manager.append_audio_data(stream_id, data["bytes"])

    except WebSocketDisconnect:
        print(f"クライアントが切断しました。")
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}")
    finally:
        manager.disconnect(websocket)
        print("接続をクリーンアップしました。")

# --- サーバーの起動 ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)