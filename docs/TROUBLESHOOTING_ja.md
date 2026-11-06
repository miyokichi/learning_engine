# Learning Engine v0.3 困ったとき

## まず通信せずに診断する

```powershell
.\.venv\Scripts\learning-engine.exe --db my_learning.sqlite3 doctor
```

`db_read_write: true`、`integrity: ok` がDBの確認結果です。`schema_migration: 4` が画像対応版の追加テーブルです。`llm_configured` は設定の有無で、疎通成功を意味しません。`llm_response_format` で `json_object` / `none` の設定を確認できます。APIキーは表示しません。

## コマンドが見つからない／venvを有効化できない

原因はインストール先の違いか、PowerShellのスクリプト実行制限です。有効化せず直接実行できます。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\learning-engine.exe --help
.\.venv\Scripts\python.exe -m learning_engine --help
```

`.venv` 自体がなければ、プロジェクトルートで `python -m venv .venv` を実行します。Pythonの起動がWindowsエイリアスに阻まれる環境では、利用できるPython 3.11以上の実行ファイルでvenvを作成してください。

## .envが反映されない

原因は作業フォルダ、環境変数の上書き、URLとモデル名の片方の欠落などです。PowerShellで `Get-Location` を確認し、プロジェクトルートに移動してdoctorを実行してください。別フォルダの設定は `--env-file` で明示します。

すでに設定した `LE_LLM_URL` や `LE_LLM_MODEL` の環境変数は `.env` より優先されます。不要ならそのPowerShell内で次を実行します。APIキーそのものを画面に貼り付ける必要はありません。

```powershell
Remove-Item Env:LE_LLM_URL -ErrorAction SilentlyContinue
Remove-Item Env:LE_LLM_MODEL -ErrorAction SilentlyContinue
.\.venv\Scripts\learning-engine.exe --db my_learning.sqlite3 doctor
```

`.env` はUTF-8で保存し、URLとモデル名を両方設定します。JSONやPowerShell構文ではなく `KEY=value` 形式です。

## LLMが応答しない／URL関連のエラー

まずサーバーが起動済みで、モデルが読み込まれているか、URLのホスト・ポート・`/v1`・モデル名を確認してください。localhostはこのPCです。別PCのLLMにはそのPCのIPを指定します。LANの明示URLも利用できます。

接続テストは、次のように明示したときだけ行います。

```powershell
.\.venv\Scripts\learning-engine.exe --db my_learning.sqlite3 doctor --check-llm
```

`failed:HTTPError` はURL・認証・モデル指定等、`failed:URLError` や `TimeoutError` は接続先・サーバー・通信等の確認が必要です。APIはOpenAI互換のchat/completionsとJSON応答に対応している必要があります。接続先を設定しても、モデルの出力品質を保証するものではありません。

`response_format` は既定では送信しません。非対応のサーバーでエラーになる場合は、既存の `.env` や環境変数にある `LE_LLM_RESPONSE_FORMAT=json_object` を削除するか `none` に変更してください。`response_format: null` ではなく、項目そのものを省略します。一時的には `doctor --check-llm --response-format none`、`chat --response-format none` でも指定できます。対応サーバーへ明示的に送信する場合は `json_object` を使えます。指定を省略した場合も、モデルにはJSON応答を求め、返答の検証を続けます。

以前の `Only explicit loopback ...` エラーはlocalhost限定検査によるもので、現在はLANの明示URLにも対応しています。依然表示されるならインストール先を確認し、上のvenvで再インストールしてください。別ホストへのリダイレクトは許可しません。

## 不正JSON・誤解釈・ネットワーク失敗

chatは原文を先に保存し、失敗した更新を取り消して保留します。`/status` で確認対象を見て、修正内容を説明するか再入力します。`確認` で採用できるのは、正常に保存された確認対象だけです。接続失敗のまま再起動しても原文と保留は残ります。

LLMが利用できなければtutorial、またはinteractで手動入力してください。固定会話教材 `chat --sample` は、表示された練習文だけに対応し、自由な発言の代わりにはなりません。

## 空のDBでREVIEW／no_root_or_ambiguousになる

承認済みルールがない、条件が一致しない、複数のルールが競合している状態です。AIが勝手にルールを作って正解にすることはありません。判断を分ける条件と結論を説明し、解釈・草案・回帰結果を確認してください。初期ルール作成を試すなら固定会話チュートリアルが利用できます。

## Unknownで止まる／同じ過去事例を転用しない

材料・形状・根拠・定義・単位など、適用条件の確認が不足している可能性があります。質問に回答し、その解釈も確認してください。「未確認」はfalseではないので、回答だけで確定できない場合もあります。新しい根拠が出たら、訂正として伝えてください。安全な内部調査案は「仮定を置いた探索をしたい」と相談できます。

## 候補を作ったのに判断に反映されない

候補と採用済み知識は別です。`OK` は承認になりません。`/status` で、何を採用する確認なのかを読んで `確認` します。解釈確認後も、ルール公開や手順候補承認には別の確認が必要です。回帰違反があれば公開できません。定義の足りない概念は、定義・測定・単位・範囲を補ってください。

## 訂正で親ルールや比較事例のエラー

無関係な事例や異なる判断対象を使うと拒否します。同じ親ルールにつながる過去事例との違いを説明してください。後で変えた属性だけを使い、当時の判断経路をすり替えることもできません。数値2点から境界を自動生成する機能はありません。

## 日本語が文字化けする

```powershell
$env:PYTHONIOENCODING = "utf-8"
.\.venv\Scripts\learning-engine.exe --db tutorial.sqlite3 tutorial
```

ファイル入力には `Get-Content -Encoding UTF8` を使ってください。Windows PowerShellでパイプの日本語が崩れる場合は `$OutputEncoding = [System.Text.UTF8Encoding]::new()` も設定してください。

## SQLiteのロック・バックアップ・旧DB

同じDBを使うchatやinteract、DB閲覧ツールを閉じてから再試行してください。同じDBの多人数同時編集は対象外です。ファイルを移動・上書き・共有する前は、すべてのプロセスを終了します。

```powershell
Copy-Item -LiteralPath my_learning.sqlite3 -Destination my_learning.backup.sqlite3
```

この例はファイルが存在し、アプリ終了後であることが前提です。移行は追加型で、旧Case・Rule・Feedback・Auditを消しません。画像対応版の台帳は `schema_migrations` の4、互換性のため `PRAGMA user_version` は2です。破損が疑われる場合は原本を保管し、コピーで診断してください。自動修復は実装していません。
