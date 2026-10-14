# 調合AIロボット 解説アニメーション

HTML・Canvas・JavaScriptによる4章の解説動画です。日本語ナレーションはPythonの `edge-tts` で事前生成し、`audio/` に保存したMP3を再生します。**APIキーは不要です。HTML再生中にMicrosoftやOpenAIへ通信することはありません。**

## 再生する

Python 3を用意し、リポジトリのルートで静的サーバーを起動します。

```sh
python -m http.server 8000 --bind 127.0.0.1
```

ブラウザでサーバーの `index.html` を開きます。各章のHTMLを単体で開くこともできます。iframeの共通時計を使うため、ファイルを直接開くのではなくHTTPサーバー経由で再生してください。

映像は自動再生します。**最初に「音声ON」を押すとナレーションが有効になります。** ブラウザの自動音声再生制限に対応するため、音声は最初はOFFです。冒頭から聞く場合は「最初から」を押してください。

| 操作 | 動作 |
|---|---|
| 再生／一時停止、Space | 映像と音声を同時に再生・停止 |
| 最初から | 通し再生ではOPへ、単体ではその章の先頭へ戻って再生 |
| Home | 先頭へ移動。再生／停止状態を維持 |
| 音声ON／OFF | 映像の現在位置に合わせて音声を有効化・ミュート |
| 再生位置スライダー、←→ | 共通タイムラインで移動。←→は基準時刻を1秒移動 |
| 章選択、1〜4 | 通し再生で選んだ章の先頭から再生 |
| ↑↓ | シーンの表示時間の倍率を0.05ずつ変更 |

基準タイムラインは185秒、初期の表示倍率は2なので、実再生は **6分10秒** です。スライダーと時刻表示は基準時刻を使います。章やシーンを移動すると古い音声を停止して対応するMP3に切り替えます。最終章の後はOPへループします。

初期倍率2で聞き取りやすい速さになるよう生成しています。倍率を変更すると、音程を保ちつつMP3の再生速度も変更し、映像との同期を維持します。大幅な速度変更では聞き取りやすさが変わるため、通常は倍率2を推奨します。単体ページの `#t=85` のような指定は、動画全体の基準時刻で静止表示します。

配布に必要なのはHTML、`assets/`、`audio/scene-*.mp3`です。Pythonやネット接続は音声付きHTMLの再生には不要です（ローカルHTTPサーバーは別途必要）。原稿や再生成スクリプトも編集用に一緒に保存することを推奨します。

## 原稿を編集し、変更した音声だけ再生成する

音声生成にはPython 3.10以降、FFmpeg（`ffmpeg`と`ffprobe`）、Microsoftの音声サービスへのインターネット接続が必要です。APIキーや `OPENAI_API_KEY` は設定しません。

FFmpegは使用中のOSのパッケージマネージャー等でインストールしてください。Ubuntuでは `sudo apt-get install ffmpeg`、macOSでは `brew install ffmpeg` で導入できます。

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-tts.txt
.venv/bin/python scripts/generate_narration.py
```

Windowsでは `.venv/bin/python` を `.venv\Scripts\python.exe` に読み替えてください。このクラウド環境では準備済みの `/workspace/.venvs/physicalai-tts/bin/python` も利用できます。

編集元は [narration/narration.ja.json](narration/narration.ja.json) です。

- `narration_text`: シーン全体の原稿。
- `narration_cues`: 発話ごとの原稿と基準開始時刻。字幕や映像の動作に合わせて区切ります。**cueのtextをつなげた文章がnarration_textと一致するよう両方を編集してください。**
- `generation.voice`: 初期値は日本語女性音声 `ja-JP-NanamiNeural`。男性音声は `ja-JP-KeitaNeural`。
- `generation.rate`: 初期値は `-5%`。落ち着いた話速に設定しています。

原稿変更後に同じ生成コマンドを実行すると、原稿・時刻・音声設定のハッシュが変わったシーンだけを再生成します。変更のないMP3には音声サービスへのリクエストを送りません。出力ファイルのハッシュも確認するので、欠落・破損したMP3は再作成対象になります。途中で失敗しても完成済みのシーンは保持し、再実行時に再利用します。

```sh
# 欠落・更新が必要な音声を確認するだけ。通信・音声生成はしない。
.venv/bin/python scripts/generate_narration.py --check

# 特定のシーンだけ確認・再生成する。
.venv/bin/python scripts/generate_narration.py --scene scene-03

# 原稿が同じでも、指定シーンを音声サービスから生成し直す。
.venv/bin/python scripts/generate_narration.py --scene scene-03 --force
```

開始時刻ごとに短い音声を生成し、FFmpegで間を埋めて1シーン1MP3にまとめます。各発話が枠に少しだけ収まらない場合は音程を保って最大1.2倍まで調整します。それでも長い場合は切り捨てずにエラーで止まるので、その発話を短くするか区切り時刻を調整してください。音声設定を変更すると全シーンが再生成対象になります。

生成結果と発話の実測値は `audio/manifest.json`、途中の音声キャッシュは `audio/.cache/` に保存します。キャッシュはGit管理対象外です。完成したMP3とmanifestは配布・保存対象です。`assets/narration-data.js` は生成スクリプトが原稿から更新するブラウザ用のローカル時刻・パス情報です。

`edge-tts` はMicrosoft Edgeのオンライン読み上げサービスを使用します。**原稿を送信するのは生成スクリプトの実行時だけです。** 接続制限がある環境では `speech.platform.bing.com` のHTTPS／WebSocket接続を許可してください。`HTTPS_PROXY` があれば自動利用します。TLS検証は維持し、`truststore` によりOSの信頼済み証明書を利用します。

## 検証

```sh
# ネットワーク不要の生成処理テスト。テスト音は一時ディレクトリだけに保存。
python -m unittest discover -s tests -p 'test_*.py' -v

# 実MP3を使うブラウザ検証。Python PlaywrightとChromiumが必要。
python tests/check_playback.py
```

ブラウザ検証は20シーンのMP3と映像の時刻、停止・再開、シーク、章変更、ループ、音声ON/OFF、単体再生を確認し、ローカルサーバー以外へのリクエストを拒否します。クラウド環境では `/usr/bin/chromium` を使用します。

初期の構成解析と原稿・時間の概算は [docs/narration-analysis.md](docs/narration-analysis.md) に保存しています。実装後の再生・生成手順はこのREADMEを参照してください。
