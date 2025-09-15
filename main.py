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
EXPECTED_API_KEY = os.getenv("SERVER_AUTH_TOKEN", "dev-token")

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
# 16kHz, 16-bit, mono の 200ms 分のバイト数（6400B）
TTS_CHUNK_SIZE = 16000 * 2 * 1 * 200 // 1000
# ストリーミング起動のしきい値（ms）: 既定 1200ms 貯まったら自動処理
MIN_PROCESS_MS = int(os.getenv("MIN_PROCESS_MS", "1200"))
# 20ms=640B のフレームを基準に計算
STREAM_THRESHOLD_BYTES = max(640, (MIN_PROCESS_MS // 20) * 640)

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
def pcm_s16le_to_float32(audio_data: bytes) -> np.ndarray:
    """生PCM(s16le)データをWhisperが処理できるfloat32形式に変換"""
    return np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

async def generate_voicevox_audio(text: str, speaker_id: int) -> Union[bytes, None]:
    """VOICEVOX APIを呼び出して音声を生成する"""
    async with httpx.AsyncClient() as client:
        try:
            # 1. audio_queryの作成
            params = {"text": text, "speaker": speaker_id}
            res_query = await client.post(f"{VOICEVOX_BASE_URL}/audio_query", params=params, timeout=10.0)
            res_query.raise_for_status()
            audio_query = res_query.json()
            # 出力を 16kHz/mono に統一（クライアント仕様に合わせる）
            try:
                audio_query["outputSamplingRate"] = 16000
                audio_query["outputStereo"] = False
            except Exception:
                pass
            
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

# --- WebSocketエンドポイント ---
@app.websocket("/ws/{mic_id}")
async def websocket_endpoint(websocket: WebSocket, mic_id: str):
    # ヘッダーから認証トークンを取得し、手動で検証する
    auth_header = websocket.headers.get("Authorization")
    token = None
    if auth_header:
        try:
            scheme, _, token_value = auth_header.partition(" ")
            if scheme.lower() == "bearer":
                token = token_value
        except Exception:
            pass  # 無効なヘッダー形式

    # トークンが期待値と一致しない場合は接続を拒否
    if token != EXPECTED_API_KEY:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        print(f"❌ 認証トークンが無効なため接続を拒否しました: {auth_header}")
        return

    await manager.connect(websocket)
    # URLのmic_idをstream_idとして即時登録
    stream_id = mic_id
    manager.add_sender(stream_id, websocket)

    # ストリームごとの処理ロック
    processing_lock = asyncio.Lock()

    async def run_pipeline(full_audio_data: bytes):
        try:
            if len(full_audio_data) < 1600:  # 100ms 未満は無視
                print("音声データが短すぎるためスキップします。")
                return
            if not whisper_model or not gemini_model:
                print("モデルが準備できていないため処理をスキップします。")
                return

            t_start = time.time()
            # 1. Whisper 文字起こし
            audio_np = pcm_s16le_to_float32(full_audio_data)
            segments, _ = whisper_model.transcribe(audio_np, beam_size=5, language="ja", vad_filter=True)
            transcribed_text = "".join([s.text for s in segments]).strip()
            t_asr = time.time()
            if not transcribed_text:
                print("文字起こし結果が空でした。")
                return
            print(f"✨ 文字起こし結果: {transcribed_text}")

            # 2. Gemini で応答 + 感情
            ai_response_text = "すみません、AIの応答生成でエラーが発生しました。"
            ai_emotion = "平常"
            try:
                prompt = f'''
                ユーザーの発言「{transcribed_text}」を分析してください。
                以下の2つの項目を含むJSON形式で、結果だけを出力してください。

                1. "emotion": 発言から最も強く感じられる感情を「喜び」「怒り」「悲しみ」「平常」のいずれか一つで示してください。
                2. "reply": あなたは高性能な会話を補助するアシスタントです。ユーザーの発言「{transcribed_text}」に対して、出力は必ず日本語で、顔文字、絵文字、専門的な機械語は使用しないでください。1文は60〜90文字程度に収め、読点（、）は1文に2つまでとして、簡潔にしてください。場面としては、日常会話でカジュアルで自然な日本語に変換してください。ユーザーの発話が質問や依頼の場合は、相手に失礼のないよう、丁寧な表現に変換してください。ユーザーの発話が否定的な内容の場合は、相手を不快にさせないよう、柔らかい表現に変換してください。
                '''
                response = await gemini_model.generate_content_async(prompt)
                ai_response_json_str = response.text
                
                # JSONパース処理
                if "```json" in ai_response_json_str:
                    ai_response_json_str = ai_response_json_str.split('```json\n')[1].split('\n```')[0]
                ai_response_data = json.loads(ai_response_json_str)
                ai_emotion = ai_response_data.get("emotion", "平常")
                ai_response_text = ai_response_data.get("reply", "すみません、うまく聞き取れませんでした。")

            except Exception as e:
                print(f"❌ Gemini APIの呼び出し、または応答の解析中にエラーが発生しました: {type(e).__name__}: {e}")

            t_llm = time.time()
            print(f"😊 感情分析結果: {ai_emotion}")
            print(f"💬 Geminiからの応答: {ai_response_text}")

            # 3. VOICEVOX 合成（感情で話者切替）
            speaker_map = {"喜び": 3, "悲しみ": 1, "怒り": 8, "平常": 2}
            selected_speaker_id = speaker_map.get(ai_emotion, speaker_map["平常"])
            print(f"🗣️  話者ID '{selected_speaker_id}' を選択しました。")
            voice_wav = await generate_voicevox_audio(ai_response_text, speaker_id=selected_speaker_id)
            t_tts = time.time()

            # 4. WAV → PCM 抽出し 200ms チャンクで配信
            if voice_wav:
                import io, wave
                with wave.open(io.BytesIO(voice_wav), "rb") as wf:
                    sr = wf.getframerate()
                    ch = wf.getnchannels()
                    sw = wf.getsampwidth()
                    pcm = wf.readframes(wf.getnframes())
                    if not (sr == 16000 and ch == 1 and sw == 2):
                        print(f"⚠️ VOICEVOX出力の形式が想定外: sr={sr} ch={ch} sw={sw}")
                # 200ms=6400B に分割送信
                await manager.broadcast_audio_chunks(pcm, TTS_CHUNK_SIZE)
                await manager.broadcast_json({"type": "tts_done"})
                print("ℹ️  ミュート解除のための完了通知を送信しました。")

            t_end = time.time()
            print(f"⏱️  パフォーマンス: [ASR: {t_asr - t_start:.2f}s] [LLM: {t_llm - t_asr:.2f}s] [TTS: {t_tts - t_llm:.2f}s] [Total: {t_end - t_start:.2f}s]")
        except Exception as e:
            print(f"PIPELINE ERROR: {e}")

    try:
        while True:
            message = await websocket.receive()

            # メッセージタイプに応じて処理を分岐
            if message["type"] == "websocket.disconnect":
                break

            # テキストメッセージ(JSON)を処理
            text_data = message.get("text")
            if text_data:
                try:
                    msg_json = json.loads(text_data)
                    msg_type = msg_json.get("type")
                    
                    if msg_type == "stop":
                        # 処理中でなければロックを取得してパイプラインを実行
                        if not processing_lock.locked():
                            async with processing_lock:
                                print(f"🎤 マイク '{stream_id}' から発話終了通知を受信。")
                                full_audio_data = manager.get_and_clear_audio_data(stream_id)
                                await run_pipeline(full_audio_data)
                        else:
                            print(f"⚠️ '{stream_id}' は現在処理中のため、stop通知をスキップします。")

                except Exception as e:
                    print(f"制御メッセージ処理中にエラー: {e}")

            # バイナリメッセージ(音声データ)を処理
            bytes_data = message.get("bytes")
            if bytes_data:
                manager.append_audio_data(stream_id, bytes_data)

    except WebSocketDisconnect:
        print(f"クライアントが切断しました: {mic_id}")
    except Exception as e:
        print(f"予期せぬエラーが発生しました ({mic_id}): {e}")
    finally:
        manager.disconnect(websocket)
        print(f"接続をクリーンアップしました: {mic_id}")

# --- サーバーの起動 ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
