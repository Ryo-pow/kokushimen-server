import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, status
from starlette.websockets import WebSocketState
import os
import whisper
import google.generativeai as genai
from dotenv import load_dotenv
import httpx
from typing import Union, Dict, Optional, List
import random
import asyncio
import json
import wave
import io

# .envファイルから環境変数を読み込む
load_dotenv()

app = FastAPI()
# --- クライアントの audio_io.py と同じ設定 ---
RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit (int16) = 2 bytes

# --- 設定 ---
AVAILABLE_SPEAKER_IDS = [2, 3, 1, 8, 10, 14]
VOICEVOX_BASE_URL = "http://127.0.0.1:50021"
# クライアントが送るべき認証トークン (クライアント側と合わせる)
SERVER_AUTH_TOKEN = os.getenv("SERVER_AUTH_TOKEN", "dev-token") 

# --- モデルとAPIの準備 ---
try:
    model = whisper.load_model("base")
    print("✅ Whisperモデルのロード完了。")
except Exception as e:
    model = None
    print(f"❌ Whisperモデルのロード失敗: {e}")

try:
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ValueError("環境変数にGEMINI_API_KEYが設定されていません。")
    genai.configure(api_key=gemini_api_key)
    gemini_model = genai.GenerativeModel("gemini-flash-latest")
    print("✅ Geminiモデルの準備完了。")
except Exception as e:
    gemini_model = None
    print(f"❌ Geminiモデルの準備失敗: {e}")

# --- 認証 ---
async def validate_token(websocket: WebSocket) -> str:
    """WebSocket接続ヘッダーからトークンを検証する"""
    auth_header = websocket.headers.get("Authorization")
    if not auth_header:
        print("❌ 認証ヘッダーがありません。")
        raise WebSocketDisconnect(code=status.WS_1008_POLICY_VIOLATION, reason="Missing Authorization header")
    
    try:
        scheme, token = auth_header.split()
        if scheme.lower() != "bearer" or token != SERVER_AUTH_TOKEN:
            raise ValueError("Invalid token")
    except ValueError:
        print(f"❌ 無効なトークンです: {auth_header}")
        raise WebSocketDisconnect(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
    
    print("✅ トークン認証成功。")
    return token 

# --- 接続管理 ---
class ConnectionManager:
    def __init__(self):
        # mic_id をキーに、再生用(playback)WebSocketを保持
        self.playback_connections: Dict[str, WebSocket] = {}

    async def connect_playback(self, websocket: WebSocket, mic_id: str):
        """
        再生用(Playback)クライアントを接続
        (注: acceptは呼び出し元の websocket_endpoint で行います)
        """
        # await websocket.accept()  # ★★★ ここでは accept しない ★★★
        
        if mic_id in self.playback_connections:
            # すでに同じIDの再生クライアントがいれば古い方を切断
            print(f"⚠️  '{mic_id}' の古い再生接続を強制切断します。")
            try:
                await self.playback_connections[mic_id].close(code=status.WS_1001_GOING_AWAY, reason="New connection replaced")
            except Exception:
                pass # すでに切れていても無視
        self.playback_connections[mic_id] = websocket
        print(f"✅ [Playback] マイク '{mic_id}' が接続しました。")

    def disconnect_playback(self, mic_id: str):
        """再生用(Playback)クライアントを切断"""
        if mic_id in self.playback_connections:
            del self.playback_connections[mic_id]
            print(f"❌ [Playback] マイク '{mic_id}' が切断しました。")

    async def get_playback_ws(self, mic_id: str) -> Optional[WebSocket]:
        """指定されたmic_idの再生用WebSocketを取得する"""
        ws = self.playback_connections.get(mic_id)
        if ws and ws.client_state == WebSocketState.CONNECTED:
            return ws
        elif ws:
            # 接続が切れている場合は辞書から削除
            self.disconnect_playback(mic_id)
            return None
        return None

    async def broadcast_text(self, mic_id: str, text: str):
        """指定されたmic_idの再生クライアントにテキストを送信"""
        ws = await self.get_playback_ws(mic_id)
        if ws:
            try:
                await ws.send_json({"type": "ai_text", "text": text})
                print(f"ℹ️  [Playback] '{mic_id}' へテキスト送信: {text[:20]}...")
            except Exception as e:
                print(f"❌ [Playback] '{mic_id}' へのテキスト送信失敗: {e}")
                self.disconnect_playback(mic_id)

    async def broadcast_audio(self, mic_id: str, audio_data: bytes):
        """指定されたmic_idの再生クライアントに音声(bytes)を送信"""
        ws = await self.get_playback_ws(mic_id)
        if ws:
            try:
                await ws.send_bytes(audio_data)
                print(f"🔊 [Playback] '{mic_id}' へ音声 {len(audio_data)} bytes 送信。")
            except Exception as e:
                print(f"❌ [Playback] '{mic_id}' への音声送信失敗: {e}")
                self.disconnect_playback(mic_id)

    async def broadcast_tts_done(self, mic_id: str):
        """指定されたmic_idの再生クライアントにtts_doneを送信"""
        ws = await self.get_playback_ws(mic_id)
        if ws:
            try:
                await ws.send_json({"type": "tts_done"})
                print(f"ℹ️  [Playback] '{mic_id}' へ tts_done 送信。")
            except Exception as e:
                print(f"❌ [Playback] '{mic_id}' への tts_done 送信失敗: {e}")
                self.disconnect_playback(mic_id)

manager = ConnectionManager()


# --- 関数定義 (VOICEVOX) ---
async def generate_voicevox_audio(text: str, speaker_id: int) -> Union[bytes, None]:
    """VOICEVOX APIを呼び出し、音声(WAV)データを生成する"""
    async with httpx.AsyncClient() as client:
        try:
            params = {"text": text, "speaker": speaker_id}
            res_query = await client.post(f"{VOICEVOX_BASE_URL}/audio_query", params=params)
            res_query.raise_for_status()
            audio_query = res_query.json()
            
            # クライアント(16000Hz)と合わせるため、
            # VOICEVOXの出力サンプルレートを RATE (16000) に変更する
            audio_query["outputSamplingRate"] = RATE
            
            headers = {"Content-Type": "application/json"}
            res_synth = await client.post(
                f"{VOICEVOX_BASE_URL}/synthesis",
                params={"speaker": speaker_id},
                json=audio_query, # 変更済みの audio_query を送信
                headers=headers,
                timeout=20.0
            )
            res_synth.raise_for_status()
            
            print(f"✅ VOICEVOXによる音声合成に成功しました (Rate: {RATE}Hz)。")
            return res_synth.content
            
        except httpx.RequestError as e:
            print(f"❌ VOICEVOX APIへの接続に失敗しました: {e} (VOICEVOXアプリは起動していますか？)")
            return None
        except httpx.HTTPStatusError as e:
            print(f"❌ VOICEVOX APIからエラーが返されました: {e.response.status_code}")
            return None
# --- AI処理のコアロジック ---

async def process_audio_to_ai_response(mic_id: str, audio_data: bytes):
    """音声データを受け取り、AI処理を行い、再生クライアントに応応答を送信する"""
    
    if not model or not gemini_model:
        print("モデルが準備できていないため処理をスキップします。")
        return

    print(f"🎤 [AI Process] マイク '{mic_id}' の処理開始 (音声: {len(audio_data)} bytes)")

    try:
        # 1. Whisperで文字起こし
        temp_path = f"temp_{mic_id}.wav"
        try:
            with wave.open(temp_path, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(SAMPLE_WIDTH)
                wf.setframerate(RATE)
                wf.writeframes(audio_data)
        except Exception as e:
            print(f"❌ .wav ファイルの書き込みに失敗しました: {e}")
            return
        
        result = model.transcribe(temp_path, fp16=False)
        transcribed_text = result["text"].strip()
        os.remove(temp_path)
        
        if not transcribed_text:
            print("文字起こし結果が空でした。")
            return
        print(f"✨ 文字起こし結果 ({mic_id}): {transcribed_text}")

        # 2. Geminiで応答生成と感情分析
        prompt = f"""# 命令
あなたは、入力された日本語のテキストを分析し、指定されたルールに従ってJSONオブジェクトを生成するAIです。
以下の指示を厳格に守り、処理を実行してください。

# 禁止事項
- あなた自身の言葉で応答したり、会話を試みてはいけません。
- 分析結果のJSON以外に、説明、前置き、相槌などの余計なテキストを絶対に出力してはいけません。

# 処理対象テキスト
{transcribed_text}

# JSON生成ルール
## emotion
- 「処理対象テキスト」から話者の感情を推測し、「喜び」「怒り」「悲しみ」「平常」のいずれかを選択してください。

## UserSpeech
- 「処理対象テキスト」の内容を、以下のルールに従って自然で丁寧な日本語の文章に変換してください。
  - 顔文字、絵文字、専門的な機械語は使用しない。
  - 1文を60〜90文字程度に収め、読点は1文に2つまでにする。
  - 意図を尊重しつつ、カジュアルすぎず、ビジネスすぎない表現にする。
  - 質問や依頼の場合は、相手に失礼のない丁寧な表現に変換する。
  - 否定的な内容の場合は、相手を不快にさせない柔らかい表現に変換する。

# 出力形式
- 生成したJSONオブジェクトのみを出力してください。
- Markdownのコードブロック(```json)は不要です。

{{
  "emotion": "（ここに推測した感情）",
  "UserSpeech": "（ここに変換した日本語テキスト）"
}}
"""
        response = gemini_model.generate_content(prompt)
        
        ai_response_text = ""
        try:
            # ```json ... ``` のようなコードフェンスを削除
            cleaned_response = response.text.strip().removeprefix("```json").removesuffix("```").strip()
            
            data = json.loads(cleaned_response)
            emotion = data.get("emotion", "不明")
            ai_response_text = data.get("UserSpeech", "")

            print(f"😃 感情分析結果 ({mic_id}): {emotion}")
            print(f"💬 Geminiからの応答 ({mic_id}): {ai_response_text}")

        except (json.JSONDecodeError, AttributeError) as e:
            print(f"⚠️ Geminiからの応答のJSONパースに失敗しました: {e}")
            # パース失敗時は、応答テキストをそのまま使う
            ai_response_text = response.text

        if not ai_response_text:
            print("AIの応答が空でした。処理を中断します。")
            return

        # 3. (★目標達成) Geminiのテキストをクライアントに送信
        await manager.broadcast_text(mic_id, ai_response_text)

        # 4. VOICEVOXで音声合成
        selected_speaker_id = random.choice(AVAILABLE_SPEAKER_IDS)
        print(f"🗣️  今回選択された話者ID: {selected_speaker_id}")
        voice_data = await generate_voicevox_audio(ai_response_text, speaker_id=selected_speaker_id)

        # 5. クライアントに音声データを送り返す
        if voice_data:
            
            raw_pcm_data = b""
            duration_s = 0.0

            try:
                # 5a. メモリ上でWAVデータをパースし、raw PCMデータだけを抽出
                with io.BytesIO(voice_data) as wav_bytes:
                    with wave.open(wav_bytes, "rb") as wf:
                        # クライアントが期待するフォーマットか一応確認
                        if wf.getframerate() != RATE or wf.getsampwidth() != SAMPLE_WIDTH or wf.getnchannels() != CHANNELS:
                            print(f"❌ VOICEVOXの形式 ({wf.getframerate()}Hz, {wf.getsampwidth()}byte, {wf.getnchannels()}ch) が期待値と異なります。")
                            raw_pcm_data = b"" # ★ 処理を中断するために空にする
                        else:
                            # 形式が正しい場合のみデータを読み込む
                            raw_pcm_data = wf.readframes(wf.getnframes())
                            bytes_per_second = RATE * CHANNELS * SAMPLE_WIDTH
                            duration_s = len(raw_pcm_data) / bytes_per_second

            except wave.Error as e:
                print(f"❌ VOICEVOXからのWAVデータの解析に失敗しました: {e}")
            except Exception as e:
                print(f"❌ WAVデータの処理中に予期せぬエラー: {e}")


            if raw_pcm_data:
                # 5b. raw PCM データを送信
                await manager.broadcast_audio(mic_id, raw_pcm_data)
                
                # 5c. 音声の再生時間だけ待機
                # JitterBuffer(200ms) + ネットワーク遅延を考慮し少し余裕を持たせる
                await asyncio.sleep(duration_s + 0.3) 
                
                # 5d. ミュート解除通知を送信
                await manager.broadcast_tts_done(mic_id)
        
        print(f"✅ [AI Process] マイク '{mic_id}' の処理完了。")

    except Exception as e:
        print(f"❌ [AI Process] 処理中に予期せぬエラーが発生しました: {e}")


# --- WebSocketエンドポイント ---

@app.websocket("/ws/{mic_id}")
async def websocket_endpoint(
    websocket: WebSocket, 
    mic_id: str,
    token: str = Depends(validate_token) # 認証を実行
):
    
    # どんな接続でも、まずハンドシェイクを完了させる
    await websocket.accept()
    
    is_playback_client = False
    audio_buffer: List[bytes] = []

    try:
        # 1. 接続が確立した後、最初のメッセージを受け取る
        first_msg_raw = await websocket.receive() # dict が返る
        
        if first_msg_raw.get("type") != "websocket.receive":
            # おそらく切断メッセージなど
            raise WebSocketDisconnect(code=status.WS_1011_INTERNAL_ERROR, reason="Unexpected message type")

        first_msg_text = first_msg_raw.get("text")
        first_msg_bytes = first_msg_raw.get("bytes")

        if first_msg_text is not None:
            # --- Playbackクライアントの処理 ---
            try:
                data = json.loads(first_msg_text)
                if data.get("type") == "hello" and data.get("role") == "playback":
                    is_playback_client = True
                    await manager.connect_playback(websocket, mic_id)
                else:
                    print(f"❌ '{mic_id}' から不明なJSON。切断します: {first_msg_text}")
                    raise WebSocketDisconnect(code=status.WS_1003_UNSUPPORTED_DATA, reason="Unknown JSON message")
            except json.JSONDecodeError:
                print(f"❌ '{mic_id}' から不正なJSONテキスト。切断します: {first_msg_text}")
                raise WebSocketDisconnect(code=status.WS_1003_UNSUPPORTED_DATA, reason="Invalid JSON message")
        
        elif first_msg_bytes is not None:
            # --- Senderクライアントの処理 (最初のフレーム) ---
            print(f"✅ [Sender] マイク '{mic_id}' が接続しました (最初の音声受信)。")
            audio_buffer.append(first_msg_bytes)
        
        else:
            # bytes/str以外は切断
            print(f"❌ '{mic_id}' から空のメッセージ。切断します。")
            raise WebSocketDisconnect(code=status.WS_1003_UNSUPPORTED_DATA, reason="Empty message")

        # 2. 接続タイプごと（Playback / Sender）のメインループ
        if is_playback_client:
            # --- Playbackクライアントのループ ---
            while True:
                msg_raw = await websocket.receive() # dict が返る
                if msg_raw.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect(code=msg_raw.get("code", 1000))
                
                # Playback側からメッセージが来ることは想定していない
                text_data = msg_raw.get("text")
                if text_data:
                    print(f"ℹ️  [Playback] '{mic_id}' から予期せぬメッセージ: {text_data}")

        else:
            # --- Senderクライアントのループ ---
            while True:
                msg_raw = await websocket.receive() # dict が返る
                if msg_raw.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect(code=msg_raw.get("code", 1000))

                bytes_data = msg_raw.get("bytes")
                text_data = msg_raw.get("text")
                
                if bytes_data is not None:
                    # 音声データはバッファに追加
                    audio_buffer.append(bytes_data)
                
                elif text_data is not None:
                    # テキストデータ（制御メッセージ）
                    try:
                        msg_data = json.loads(text_data)
                        if msg_data.get("type") == "stop":
                            # VADの終了通知
                            print(f"ℹ️  [Sender] '{mic_id}' から 'stop' を受信。")
                            if not audio_buffer:
                                print("⚠️  [Sender] 'stop' を受信しましたが、音声バッファが空です。")
                                continue
                            
                            full_audio_data = b"".join(audio_buffer)
                            audio_buffer.clear()
                            
                            # AI処理タスクを非同期で実行
                            asyncio.create_task(process_audio_to_ai_response(mic_id, full_audio_data))
                        
                        else:
                            print(f"⚠️  [Sender] '{mic_id}' から不明な制御メッセージ: {text_data}")
                    
                    except json.JSONDecodeError:
                         print(f"⚠️  [Sender] '{mic_id}' から不正なJSON: {text_data}")
                
                else:
                    print(f"⚠️  [Sender] '{mic_id}' から空のメッセージ: {msg_raw}")


    except WebSocketDisconnect:
        if is_playback_client:
            manager.disconnect_playback(mic_id)
        else:
            print(f"❌ [Sender] マイク '{mic_id}' が切断しました。")
            # Senderが切断した際、バッファにデータが残っていれば処理する
            if audio_buffer:
                print(f"ℹ️  [Sender] '{mic_id}' 切断時に残バッファを処理します。")
                full_audio_data = b"".join(audio_buffer)
                audio_buffer.clear()
                asyncio.create_task(process_audio_to_ai_response(mic_id, full_audio_data))

    except Exception as e:
        print(f"予期せぬエラーが発生しました ({mic_id}, Playback={is_playback_client}): {e}")
        if is_playback_client:
            manager.disconnect_playback(mic_id)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)