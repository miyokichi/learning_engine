# Learning Engine v0.5 stdio MCP接続ガイド

v0.4の `codex/work-session-v04` を土台にした独立Pythonパッケージです。Learning Engineは仕事の実行や外部ツール操作を行いません。little_agentをimportせず、MCP通信にLLM設定も不要です。既定のMCPサーバはLLMを接続せず、既存WorkSession APIを呼び出します。

## 起動とDB

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\learning-engine.exe --db C:\work\learning.sqlite3 serve-mcp
# 同じ入口
.\.venv\Scripts\python.exe -m learning_engine --db C:\work\learning.sqlite3 serve-mcp
```

クライアントにはサーバ用Pythonの絶対パスと、同じDBの絶対パスを指定してください。相対DBパスは起動ディレクトリ基準です。`--db` はサブコマンドの前です。DBをコピー・バックアップするときはサーバを終了してください。追加型の移行6で承認トークン使用履歴を追加し、既存データと `PRAGMA user_version=2` は維持します。

stdin/stdoutはUTF-8の改行区切りJSON-RPC 2.0です。stdoutに通常メッセージを出しません。内部例外の詳細はstderrへ出力します。HTTPポートを開きません。接続終了はstdinのEOFで行います。1メッセージの上限は1 MiBです。

`initialize` → `notifications/initialized` → `tools/list` / `tools/call` を使います。`ping`も対応します。2024-11-05、2025-03-26、2025-06-18のprotocolVersionを受け付け、それ以外には2025-06-18を提案します。JSON-RPCのbatch要求は扱いません。ツール結果はtextとstructuredContentの両方を返し、配列や文字列は `{"result": ...}` に包みます。ツール処理の失敗は `isError: true`、不正JSONや不正JSON-RPCはerrorレスポンスです。

## little_agent向け接続例

ホストが一般的な `mcpServers` 形式を使う場合の例です。little_agent側の実装でキー名が異なる場合は、その設定形式にcommandとargsを対応付けてください。Learning Engineにlittle_agent固有の依存はありません。

```json
{
  "mcpServers": {
    "learning-engine": {
      "command": "C:\\work\\learning_engine\\.venv\\Scripts\\python.exe",
      "args": ["-m", "learning_engine", "--db", "C:\\work\\learning.sqlite3", "serve-mcp"]
    }
  }
}
```

Linuxならcommandを `/absolute/learning_engine/.venv/bin/python` に置き換えます。設定やAPIキーの環境変数は不要です。

## 通常のエージェント接続に公開するツール

| Tool | 用途 |
|---|---|
| le_start_work | goalとcontextで仕事を開始 |
| le_get_work_status | 現在状態取得 |
| le_get_reusable_knowledge | 公開ルール・関連事例・参考手順の取得 |
| le_request_decision | targetとknown_factsによる判断要求 |
| le_record_step | action・reason・result・evidenceの記録 |
| le_record_answer | 回答原文の記録。人間確認ではない |
| le_get_resume_context | チェックポイント・回答・履歴の取得 |
| le_pause_work | 学習側の状態を中断 |
| le_resume_work | 学習側の状態を再開 |
| le_complete_work | 完了と未承認の振り返り候補作成 |
| le_generate_reflection | 振り返り取得・生成 |
| le_list_learning_candidates | 候補とレビュー状態取得 |

JSON Schemaは `tools/list` が返します。[全ツールのSchema](mcp_tools.json)も同梱します。入力の型、必須項目、enum、余分な引数を検証します。`expert_confirmed` を通常ツールに渡しても拒否します。

### 判断と再開

`le_request_decision` の引数例：

```json
{
  "work_id": "WORK-xxx",
  "target": "既存Via抵抗モデルの適用可否",
  "decision_type": "model_reuse",
  "known_facts": {
    "sidewall_changed": {"value": true, "status": "documented", "source": "断面比較結果"}
  },
  "checkpoint": {"task": "resistance_calculation", "next_step": "choose_model"},
  "idempotency_key": "request-1"
}
```

仕事は複数の判断Caseを持てます。応答のquestion_idで原文を記録し、信頼されたUIが人間確認を終えるまで未確認として扱います。通常接続から伝えた回答は `agent_reported_human` という出典で保存し、認証済みの人間入力とは区別します。構造化factsの確実性と出典は呼び出し側の報告であり、サーバが資料の真正性を認証したものではありません。

| 状態 | 意味 |
|---|---|
| READY | 確認済みの条件から判断可能 |
| NEED_INPUT | 回答または解釈確認が必要 |
| REVIEW | 知識不足・競合など人間の判断が必要 |
| EXPLORE | 不確かさを残した調査候補 |
| BLOCK | 継続不可の条件 |
| READY_TO_RESUME | 未回答質問がなく、学習側の再開情報を取得可能 |

どの状態も外部ツール実行許可ではありません。`external_execution_authorized` は常にfalseです。Unknownの確認は不明点を解決したという意味ではなく、不明のまま保持します。

ルールがない場合はreasonが `no_rules` になります。`judgment_basis` は不足属性値とは異なり、判断基準・専門家判断を求める質問です。人間の判断は下記の専用UIから `le_record_expert_judgment` で、conclusionとreasonをcase_idに結び付けて記録できます。今回の仕事だけに使い、ルールを即時公開しません。属性質問が未解決ならNEED_INPUT、BLOCKがある場合はBLOCKを維持します。

## 信頼された人間UI専用接続

通常のAgent Tool Callには以下のツールを公開しません。ホストの信頼されたUIバックエンドは、同じDBで別のMCPプロセスを起動します。

```text
/absolute/server/python -m learning_engine --db /absolute/learning.sqlite3 serve-mcp --human-ui-key-file /private/learning-ui.key
```

この接続は次の3ツールだけを公開します。モデルのツールレジストリにこの接続を登録しないでください。

| Tool | 必須情報 |
|---|---|
| le_confirm_answer | work_id、question_id、interpretation、approval_token |
| le_review_learning_candidate | candidate_id、action（approve/reject/defer）、approval_token。修正時はcorrection |
| le_record_expert_judgment | work_id、case_id、conclusion、reason、approval_token |

鍵はホスト管理者が32バイト以上の乱数で生成し、信頼されたUIとサーバだけが読める場所に置きます。例：ホスト管理用Pythonで `os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)` を使い、`secrets.token_bytes(32)` を書き込みます。WindowsではファイルACLも設定してください。DBごとに別の鍵を使います。

UIは対象の回答解釈または候補全文を人間に表示し、明示的な承認を受けた後だけトークンを生成します。

```python
from pathlib import Path
from learning_engine.mcp_server import create_approval_token

# 信頼されたUIバックエンドでのみ実行。モデルには鍵も生成関数も渡さない。
arguments = {
    'work_id': selected_work_id,
    'question_id': selected_question_id,
    'interpretation': {'value': True},
}
# この直前にホストUIが本人の確認を取得する。
token = create_approval_token(
    Path('/private/learning-ui.key').read_bytes(),
    'le_confirm_answer', arguments)
# UI専用MCP接続で tools/call。argumentsにapproval_tokenを追加する。
```

トークンは操作名・対象ID・全引数に結び付き、既定120秒（最大300秒）の有効期限と乱数nonceを持ちます。署名、期限、引数一致、使用履歴を検証します。成功時に使用済みnonceをSQLiteへ保存し、再起動後も再利用を拒否します。失敗操作はトランザクションで戻し、修正後に期限内で再試行できます。再試行で承認内容が変わる場合には、新たな人間確認とトークンが必要です。

回答解釈の確認と候補の公開は別ツール・別トークンです。`expert_confirmed=true` を渡すだけでは承認できません。鍵を知らない通常接続には正しいトークンでも承認ツールがありません。

同じOS権限で任意のファイルを読み書きできるエージェントから、秘密鍵やDBを保護する仕組みではありません。ホストはUI接続、鍵、DBへの直接アクセスをモデルから隔離してください。Python APIの `expert_confirmed` は信頼された組み込み用APIのまま維持します。

## 小さな別環境クライアント

```text
/client/python examples/mcp_client.py /absolute/server/python /absolute/learning.sqlite3
```

クライアントは標準ライブラリだけを使い、Learning Engineをimportしません。別venvから起動できます。サンプルはツール一覧、仕事開始、質問取得まで行います。人間確認は通常のクライアントサンプルに組み込んでいません。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

実プロセスE2Eは、通常接続とUI接続で、回答確認・再開・完了・候補承認・次回仕事への再利用を検証します。空の別venvのクライアントによる接続、JSON-RPC不正入力、EOF、再起動、冪等性、対象変更・再利用・期限切れトークンの拒否も検証します。

## 残課題

実little_agentホストへの組み込みと実モデル精度は未検証です。信頼されたUIそのものの画面実装・利用者認証はホスト側の責務で、本パッケージはUI用接続とトークンAPIを提供します。キャンセル通知による実行中操作の割り込み、進捗通知、リソース/プロンプト機能、HTTP transportは対象外です。v0.4の振り返り改版・意味検索などの残課題は維持します。
