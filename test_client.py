import asyncio
import websockets
import pyaudio
import wave
import io

SERVER_IP = "localhost"

async def send_audio(websocket):
    """ローカルの音声ファイルを読み込み、一度だけサーバーに送信する"""
    try:
        with open("test_audio.wav", "rb") as f:
            audio_data = f.read()
        print(f"📤 音声ファイル ({len(audio_data)} bytes) をサーバーに送信します...")
        await websocket.send(audio_data)
        print("✅ 送信完了。サーバーからの応答を待ちます。")
    except FileNotFoundError:
        print("❌ エラー: 'test_audio.wav' が見つかりません。先に音声ファイルを作成してください。")
        raise

async def receive_and_play_audio(websocket):
    """サーバーから音声データを受信し、再生する"""
    p = pyaudio.PyAudio()
    stream = None
    
    print("🎧 サーバーからの音声応答を待機中...")
    try:
        audio_data = await websocket.recv()
        print(f"✅ サーバーから音声データ ({len(audio_data)} bytes) を受信しました。再生します...")
        
        with io.BytesIO(audio_data) as buffer:
            with wave.open(buffer, 'rb') as wf:
                stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                                channels=wf.getnchannels(),
                                rate=wf.getframerate(),
                                output=True)
                
                data = wf.readframes(1024)
                while data:
                    stream.write(data)
                    data = wf.readframes(1024)

    except websockets.exceptions.ConnectionClosed:
        print("🔌 サーバーとの接続が切れました。")
    finally:
        if stream:
            stream.stop_stream()
            stream.close()
        p.terminate()
        print("🔊 再生を終了しました。")

async def main():
    uri = f"ws://{SERVER_IP}:8000/ws/mic_a"
    try:
        async with websockets.connect(uri) as websocket:
            send_task = asyncio.create_task(send_audio(websocket))
            receive_task = asyncio.create_task(receive_and_play_audio(websocket))
            await asyncio.gather(send_task, receive_task)
    except (ConnectionRefusedError, OSError):
        print("❌ サーバーに接続できませんでした。サーバーが起動しているか確認してください。")
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}")

if __name__ == "__main__":
    print("クライアントを起動します...")
    asyncio.run(main())