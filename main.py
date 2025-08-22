import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import os
import whisper
import google.generativeai as genai
from dotenv import load_dotenv
import httpx
from typing import Union
import random

# .envファイルから環境変数を読み込む
load_dotenv()

app = FastAPI()

# --- 設定 ---
# ランダムに選択したい話者IDのリスト
AVAILABLE_SPEAKER_IDS = [2, 3, 1, 8, 10, 14]
# VOICEVOX APIのベースURL
VOICEVOX_BASE_URL = "http://127.0.0.1:50021"

# --- モデルとAPIの準備 ---
# Whisperモデル
try:
    model = whisper.load_model("base")
    print("✅ Whisperモデルのロード完了。")
except Exception as e:
    model = None
    print(f"❌ Whisperモデルのロード失敗: {e}")

# Geminiモデル
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
    """VOICEVOX APIを呼び出し、音声(WAV)データを生成する"""
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
            
            print("✅ VOICEVOXによる音声合成に成功しました。")
            return res_synth.content
            
        except httpx.RequestError as e:
            print(f"❌ VOICEVOX APIへの接続に失敗しました: {e} (VOICEVOXアプリは起動していますか？)")
            return None
        except httpx.HTTPStatusError as e:
            print(f"❌ VOICEVOX APIからエラーが返されました: {e.response.status_code}")
            return None

# --- WebSocketエンドポイント ---
@app.websocket("/ws/{mic_id}")
async def websocket_endpoint(websocket: WebSocket, mic_id: str):
    await websocket.accept()
    print(f"✅ マイク '{mic_id}' が接続しました。")

    try:
        while True:
            data = await websocket.receive()
            # クライアントからは音声(bytes)と制御(str)が送られてくる
            # 音声データでなければ処理をスキップ
            if not isinstance(data, bytes):
                print(f"ℹ️  マイク '{mic_id}' からテキストメッセージ（おそらく制御用）を受信: {data}")
                continue
            
            audio_data = data

            if not model or not gemini_model:
                print("モデルが準備できていないため処理をスキップします。")
                continue

            print(f"🎤 マイク '{mic_id}' から音声データ受信: {len(audio_data)} bytes")

            # 1. Whisperで文字起こし
            temp_path = f"temp_{mic_id}.wav"
            with open(temp_path, "wb") as f: f.write(audio_data)
            result = model.transcribe(temp_path, fp16=False)
            transcribed_text = result["text"].strip()
            os.remove(temp_path)
            if not transcribed_text:
                print("文字起こし結果が空でした。")
                continue
            print(f"✨ 文字起こし結果: {transcribed_text}")

            # 2. Geminiで応答生成
            prompt = f"ユーザーの発言「{transcribed_text}」に対して、親切で簡潔なアシスタントとして応答してください。"
            response = gemini_model.generate_content(prompt)
            ai_response_text = response.text
            print(f"💬 Geminiからの応答: {ai_response_text}")

            # 3. VOICEVOXで音声合成
            selected_speaker_id = random.choice(AVAILABLE_SPEAKER_IDS)
            print(f"🗣️  今回選択された話者ID: {selected_speaker_id}")
            voice_data = await generate_voicevox_audio(ai_response_text, speaker_id=selected_speaker_id)

            # 4. クライアントに音声データを送り返す
            if voice_data:
                print(f"🔊 クライアントに音声データ ({len(voice_data)} bytes) を送信します。")
                await websocket.send_bytes(voice_data)
                # ミュート解除のために完了通知を送信
                await websocket.send_json({"type": "tts_done"})
                print("ℹ️  ミュート解除のための完了通知を送信しました。")


    except WebSocketDisconnect:
        print(f"❌ マイク '{mic_id}' が切断しました。")
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)