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

## WebSocketテスト (kokusimen-client)

実際のクライアントプロジェクト `kokusimen-client` を使用して、サーバーとの接続テストを行います。

**前提:**
- このサーバー (`kokushimen-server`) の環境構築と起動が完了していること。
- `kokusimen-client` プロジェクトの環境構築が完了していること。
- `kokusimen-client` のコードが、サーバーの仕様に合わせて修正されていること。（`client_final_modifications.md`を参照）

**テスト手順:**

1.  **サーバーの起動**
    まず、`実行方法` のセクションに従って、このサーバープロジェクトを起動しておきます。

2.  **クライアントの起動**
    サーバーとは**別の新しいターミナル**を開き、`kokusimen-client` プロジェクトのディレクトリに移動して、クライアントを起動します。

    ```shell
    # (例) kokusimen-client の client ディレクトリへ移動
    cd C:\Users\lamzn\Projects\kokushimen-server\kokusimen-client\client

    # kokusimen-client 側の仮想環境を有効化 (パスは適宜修正してください)
    .\.venv\Scripts\activate

    # クライアントのメインスクリプトを実行
    python run.py
    ```

3.  **動作確認**
    - **サーバー側**のターミナルに、`マイク 'self' が接続しました。` などの接続ログや、音声データ受信時のログが表示されることを確認します。
    - **クライアント側**のターミナルで、エラーなくサーバーへの接続が完了し、音声の送受信が開始されることを確認します。
