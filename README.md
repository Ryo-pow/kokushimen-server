# リアルタイム音声対話AI サーバー

## 概要

マイクから入力された音声をリアルタイムで受け取り、AIが生成した応答を音声で返す、音声対話アプリケーションのバックエンドサーバーです。
クライアントからのWebSocket接続を待ち受け、以下の処理を実行します。

1.  **音声認識 (ASR):** [Whisper](https://github.com/openai/whisper) を使用して、クライアントからの音声データをテキストに変換します。
2.  **応答生成 (LLM):** [Google Gemini](https://ai.google/discover/gemini/) を使用して、認識されたテキストに対する応答文を生成します。
3.  **音声合成 (TTS):** [VOICEVOX](https://voicevox.hiroshiba.jp/) APIを呼び出し、生成されたテキストを音声データに変換してクライアントに返します。

## 主な使用技術

- **フレームワーク:** FastAPI
- **音声認識 (ASR):** OpenAI Whisper
- **言語モデル (LLM):** Google Gemini
- **音声合成 (TTS):** VOICEVOX
- **非同期通信:** WebSocket

## ファイル構成

- `main.py`:
  - FastAPIアプリケーションの本体。
  - WebSocket (`/ws/{mic_id}`) エンドポイントを定義し、クライアントとのリアルタイム音声通信を処理します。
- `converter.py`:
  - 音声ファイル（m4a, mp3など）をWhisperが処理しやすいWAV形式に自動変換する補助スクリプトです。
  - フォルダを監視し、新しいファイルが追加されると自動で`ffmpeg`を実行します。
- `test_client.py`:
  - サーバーの動作確認用テストクライアント。
  - `test_audio.wav`をサーバーに送信し、返ってきた音声を再生します。
- `requirements.txt`:
  - プロジェクトに必要なPythonライブラリのリストです。

## 環境構築

1.  **リポジトリのクローン**
    ```shell
    git clone <repository_url>
    cd kokushimen-server
    ```

2.  **Python仮想環境の作成と有効化**
    プロジェクト用に独立したPython環境を用意します。
    ```shell
    # 仮想環境の作成
    python -m venv .venv

    # 仮想環境の有効化 (Windowsの場合)
    .\.venv\Scripts\activate
    ```

3.  **FFmpegのインストール**
    Whisperが音声を処理するために必要な`FFmpeg`をシステムにインストールします。
    - [公式サイト](https://www.gyan.dev/ffmpeg/builds/)から最新版をダウンロードします。
    - 解凍後、`bin`フォルダへのパスをシステムの環境変数 `Path` に追加してください。

4.  **Pythonライブラリのインストール**
    `requirements.txt`を使用して、必要なライブラリを一括でインストールします。
    ```shell
    pip install -r requirements.txt
    ```

## 実行方法

1.  **環境変数ファイルの設定**
    プロジェクトルートにある`.env.example`をコピーし、`.env`という名前のファイルを作成します。
    ```shell
    copy .env.example .env
    ```
    作成した`.env`ファイルを開き、ご自身のGemini APIキーを設定してください。
    ```env
    GEMINI_API_KEY="YOUR_GEMINI_API_KEY_HERE"
    ```

2.  **サーバーの起動**
    以下のコマンドでFastAPIサーバーを起動します。
    ```shell
    python main.py
    ```
    コンソールに`Uvicorn running on http://0.0.0.0:8000`と表示されれば、サーバーはクライアントからの接続を待機している状態です。

## WebSocketテスト

`test_client.py` を使用して、サーバーとの基本的な音声送受信をテストできます。

1.  **テスト用音声ファイルの準備**
    プロジェクトのルートディレクトリ（`kokushimen-server`）に、`test_audio.wav` という名前の音声ファイルを配置してください。音声はWAV形式（16kHz, 16-bit, モノラル）を推奨します。

2.  **サーバーの起動**
    まず、`実行方法` のセクションに従って、サーバーを起動しておきます。

3.  **テストクライアントの実行**
    サーバーとは**別の新しいターミナル**を開き、以下のコマンドを実行します。

    ```shell
    # 仮想環境の有効化 (Windowsの場合)
    .\.venv\Scripts\activate

    # テストクライアントの起動
    python test_client.py
    ```

4.  **動作確認**
    - **サーバー側**のターミナルに、音声データが受信され、処理が進むログが表示されます。
    - **クライアント側**のターミナルに、音声の送信・受信ログが表示され、サーバーから返ってきた音声がPCのスピーカーから再生されます。（`pyaudio`ライブラリが正しく動作している場合）
