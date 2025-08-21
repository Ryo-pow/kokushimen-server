import time
import subprocess
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 設定 ---
# 監視するフォルダ（'.' はスクリプトがある現在のフォルダを意味します）
WATCH_FOLDER = "." 
# 自動で変換したいファイルの拡張子
SUPPORTED_EXTENSIONS = {'.m4a', '.mp3', '.mov', '.mp4'}

def convert_to_wav(input_file: str):
    """
    指定されたファイルをWhisperが推奨するWAV形式に変換します。
    (16kHz, 16-bit, モノラル)
    """
    # 出力ファイル名 (.wav) を作成
    base_name = os.path.splitext(input_file)[0]
    output_file = f"{base_name}.wav"
    
    # 既に変換済みのファイルが存在する場合は処理しない
    if os.path.exists(output_file):
        print(f"✅ 変換済みファイルが既に存在するため、スキップします: {output_file}")
        return

    print(f"⏳ 新しい音声ファイルを検出！ '{input_file}' をWAVに変換します...")
    
    # ffmpegのコマンドを組み立て
    command = [
        'ffmpeg',
        '-i', input_file,
        '-ar', '16000',        # サンプリングレート: 16kHz
        '-ac', '1',            # チャンネル: モノラル
        '-c:a', 'pcm_s16le',   # コーデック: 16-bit PCM
        '-y',                  # 確認なしで上書き
        output_file
    ]
    
    try:
        # ffmpegを実行
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"✅ 変換完了！ -> '{output_file}'")
    except subprocess.CalledProcessError as e:
        print(f"❌ '{input_file}' の変換に失敗しました。")
        print(f"   エラー内容: {e.stderr.decode('utf-8', errors='ignore')}")
    except FileNotFoundError:
        print("❌ エラー: 'ffmpeg' コマンドが見つかりません。インストールされていますか？")

class AudioFileHandler(FileSystemEventHandler):
    """ファイルが作成されたときに呼び出されるイベントハンドラ"""
    def on_created(self, event):
        # ディレクトリの場合は無視
        if event.is_directory:
            return
        
        file_path = event.src_path
        _, extension = os.path.splitext(file_path)
        
        # サポート対象の拡張子かチェック
        if extension.lower() in SUPPORTED_EXTENSIONS:
            # ファイルの書き込みが完了するまで少し待つ
            time.sleep(1) 
            convert_to_wav(file_path)

if __name__ == "__main__":
    print("🤖 自動音声変換ロボットが起動しました。")
    # abspathでフルパスを表示して分かりやすくする
    print(f"📁 監視中のフォルダ: '{os.path.abspath(WATCH_FOLDER)}'")
    print(f"🎯 対象の拡張子: {list(SUPPORTED_EXTENSIONS)}")
    print("（このウィンドウを開いたままにしておいてください）")
    print("（停止するには Ctrl+C を押してください）")

    # 監視を開始
    event_handler = AudioFileHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCH_FOLDER, recursive=False) # recursive=Falseでサブフォルダは監視しない
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    print("\n👋 ロボットを停止しました。")