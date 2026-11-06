# Learning Engine v0.3

人間との対話から判断基準・概念・作業手順を学ぶ、独立したPythonパッケージです。Python 3.11以上とSQLiteで動作し、実行時の追加依存はありません。外部ハーネス・常駐サーバー・Adapter・MCP連携は不要です。

「対話 → 理解の確認 → 判断 → 訂正 → 例外ルール学習 → 未経験案件への適用」を、CLIとPython APIで試せます。すべてのサンプルは架空のVia/Metal事例です。モデルや手順の物理的妥当性を保証する製品ではありません。

## 初めての方へ：3つのコマンド

リポジトリのルートで実行してください。仮想環境の有効化は不要です。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\learning-engine.exe --db tutorial.sqlite3 tutorial
```

画面を読み、進めるときだけ `確認` と入力します。架空の回答・承認を使った教材で、実データは不要です。

普段は `.env` を設定し、`.\.venv\Scripts\learning-engine.exe --db my_learning.sqlite3 chat` で日本語を話してください。案件や作業報告から始められ、IDやJSONを書く必要はありません。LLMなしでは自然言語の自動解釈は無効です。`tutorial` または従来の `interact` のフォームで利用できます。

- [日本語の使い方ガイド](docs/USER_GUIDE_ja.md)：cloneから起動、会話、訂正、再開まで。
- [チュートリアル](docs/TUTORIAL_ja.md)：架空教材と、LLM不要の固定会話練習。
- [用語と考え方](docs/CONCEPTS_ja.md)：Case、Unknown、承認、判断と実行の違い。
- [困ったとき](docs/TROUBLESHOOTING_ja.md)：設定・起動・質問・SQLiteの診断。
- [v0.3実装報告](IMPLEMENTATION_REPORT_v0_3.md)：互換性、検証結果、制約。

`--db` と `--env-file` はサブコマンドより前に指定します。教材DBと自分の案件DBを分けると、架空データと混ざりません。

## v0.3の新しい入口

```powershell
# 普通の日本語で依頼・回答・訂正・作業報告（.envのLLMを使用）
.\.venv\Scripts\learning-engine.exe --db my_learning.sqlite3 chat
# 前回の会話は通常起動時に番号で選択可能。chat --session に表示されたIDを指定しても再開できます
# 接続せずにDB・設定を確認。--check-llm を加えた時だけ疎通します
.\.venv\Scripts\learning-engine.exe --db my_learning.sqlite3 doctor
# 本番DBを開かず、一時DBで順次学習と比較実験
.\.venv\Scripts\learning-engine.exe replay-experiment
```

会話の `確認` は、画面に表示された一つの確認対象にだけ結び付きます。`OK` やLLM自身の「承認」は採用になりません。解釈確認とルール公開、手順候補の承認はそれぞれ別の確認です。原文・エラー・保留・候補を保存し、`/exit` で終了、`/status` で状態を確認できます。

画像対応LLMを設定すると、chat内の `/image "C:\work\断面 画像.png"` で画像を添付できます。続けて説明・判断したいことを入力し、不明点への質問に答え、解釈を `確認` してナレッジを蓄積します。PNG・JPEG・WebP、1枚5 MiB以下に対応し、現在の案件に関連する直近4枚をLLMへ送信します。画像本体と出典はSQLiteに保持するため、再開時に元ファイルは不要です。詳しくは[画像の使い方](docs/USER_GUIDE_ja.md#画像を見ながら質問ナレッジを蓄積する)を参照してください。

逐次リプレイは `case_revealed → system_decision → expert_answer_revealed / expert_review_revealed → correction_confirmed → rule_published` 等のイベントを `available_at` 順で適用します。`event_time` と利用可能時刻を分離し、未来の回答・正解を推論に渡しません。testの正解は採点だけに使い、学習DBに投入しません。`baseline_llm_plus_cases` は既定では固定の事例参照モデルで、実LLMの性能評価ではありません。fixtureは `examples/replay_sequence.json`、APIは `run_experiment` と `compare_experiment` です。

## 従来コマンドとAPI

プロジェクトのルートで実行してください。Windows PowerShellの例です。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
learning-engine --help
learning-engine --db learning.sqlite3 init
learning-engine --db learning.sqlite3 demo
learning-engine --db learning.sqlite3 interact
python -m unittest discover -s tests -v
```

コンソールエントリと同じ処理を `python -m learning_engine` でも起動できます。文字化けする環境では `$env:PYTHONIOENCODING = "utf-8"` を設定してください。仮想環境を有効化できない場合は `.venv\Scripts\python.exe` と `.venv\Scripts\learning-engine.exe` を直接指定できます。

インストール時だけビルド用のsetuptoolsが必要です。オフライン環境では事前に用意し、`python -m pip install --no-build-isolation -e .` を使えます。実行・テストでは外部ネットワークを使用しません。

## まずデモを試す

```powershell
learning-engine --db demo.sqlite3 init
learning-engine --db demo.sqlite3 demo
learning-engine --db demo.sqlite3 pending
learning-engine --db demo.sqlite3 replay --mode current_knowledge
learning-engine --db demo.sqlite3 replay --mode point_in_time
learning-engine --db demo.sqlite3 metrics
```

デモは回答・承認も架空のサンプルであることを表示します。同じDBで再実行すると投入済み結果を返し、重複を作りません。途中失敗したデモDBでは、新しいDBを指定するよう案内します。通常の対話による保留状態は再開できます。

1. `DEMO-OLD`：材料OXIDE_Aでモデル再利用が承認された過去ケース。
2. `DEMO-NEW`：材料がUnknownなのでASK。質問への原文回答と属性解釈を確定すると、既存ルールがモデル再利用を提案。
3. 専門家が「側壁形状が異なるので再検証」と訂正。過去ケースと差異を表示し、形状同等性の差を選択。
4. 解釈を確認し、`geometry_equivalence == false` の例外を草案化。回帰検査後に公開し、過去の正しい判断を維持。
5. 自然言語の作業報告からFake LLMで手順候補を抽出。解釈確認・候補承認を保存。
6. 未経験の `DEMO-NEXT` に再検証ルールを適用。`DEMO-UNKNOWN` は同等性が未確認なのでASKのまま保持。

`python examples/learning_cycle.py --db sample.sqlite3` は同じ流れをAPIから実行します。

## 対話CLI

`interact` は番号を選ぶ入力フォームです。JSONの手書きは不要です。

| 番号 | 操作 |
|---|---|
| 1 | 既存ケースの選択、新規ケースと属性の登録 |
| 2 / 11 | 確定判断の評価 / 安全なローカル探索 |
| 3 | 未解決質問への原文回答、型付き解釈の確認、再評価 |
| 4 | 訂正、承認済みcornerstoneとの比較、重要差異の選択、解釈確認、草案、回帰、公開 |
| 5 / 14 | 自然言語の作業報告、追加回答 |
| 6 / 7 | 未完了状態の一覧 / object_idからの再開 |
| 8 | 基本ルールを一致条件のフォームで登録・回帰・承認 |
| 9 | 新概念の定義・測定基準・単位・適用範囲を提案し、影響候補を確認 |
| 10 | 現在知識でのリプレイと集計 |
| 12 | 承認済み手順の参考提示、複数報告の共通手順・分岐候補の比較 |
| 13 | 明示的な専門家レビュー。意見相違はdisputedで保持 |
| 15 | AIへの自然言語相談。解釈・質問・仮説・ルール候補・概念候補を未確認で保存 |
| 0 | 終了 |

質問・回答確認待ち・訂正解釈待ち・ルール承認待ち・回帰衝突・作業報告・手順承認待ち・探索をSQLiteに保存します。IDは画面と `pending` に表示します。終了後に同じDBで起動し、7から再開してください。回帰衝突では条件を追加対比で見直し、古い草案を撤回して新しい草案を作成します。

LLM未設定時は「自動解釈は無効」と表示します。作業報告の原文を保存し、目的・実施条件・作業・理由・結果・判断基準をフォームに入力して確認できます。LLM/API失敗や不正JSONでも原文を失いません。

デモ後の具体的な練習：1で `DEMO-UNKNOWN` を選び、2で評価、3で表示されたquestion_idに回答します。同等性の値を `true` と入力して確認すると再利用、`false` なら再検証になります。5では自分の作業報告を入力して手順候補を確認できます。

## Python API

```python
from learning_engine import Engine

with Engine("work.sqlite3") as engine:
    engine.add_case("A", {
        "material": {"value": "oxide", "status": "documented", "source": "架空資料"}
    }, metadata={"target": "Via", "goal": "モデル選択", "scope": "架空工程"})
    # 実際に専門家が確認したときだけ実行する明示承認API。
    engine.review_case("A", "REUSE_MODEL")
    engine.add_rule("ROOT", "REUSE_MODEL", [
        {"feature": "material", "op": "eq", "value": "oxide"}
    ], cornerstone_case_id="A")
    engine.publish_rule("ROOT", expert_confirmed=True, impacts_reviewed=True)
    engine.add_case("B", {})
    result = engine.evaluate("B")  # ASK、question_idと判断への影響を返す
    question_id = result["questions"][0]["id"]
    engine.answer_question(question_id, "資料でoxideと確認した")
    engine.confirm_question_feature(question_id, "oxide", expert_confirmed=True)
    assert engine.evaluate("B")["decision"] == "REUSE_MODEL"
```

型付き属性の状態は `measured / documented / expert_confirmed / derived_verified / assumed / conflicting / unknown`。後半3種類は確定ルールの根拠にしません。属性値は文字列・真偽値・有限数値・不明値で、複雑な条件は複数の固定属性に分解します。

`register_feature(id, definition, aliases=..., unit=..., measurement=..., expert_confirmed=True)` で属性を定義すると、承認された別名を正規化し、単位・定義版を照合します。単位の自動変換や角度基準の自動統合は行いません。既存の未定義属性はv0.1互換で使用できます。異なる意味や新版は新しい正規IDにしてください。

`feedback → feedback_context → confirm_feedback → draft_exception_from_feedback → rule_preview → publish_rule` が訂正学習APIです。差異からの自動草案化は真偽値・カテゴリ値の一致条件に限定します。数値境界は専門家が別途根拠を確認した上で `add_rule` に明示します。`add_rule` は手動作成したルールを扱う従来APIでもあり、公開には明示承認と回帰確認が必要です。

## 手順・概念・証拠

手順は原文報告から開始し、ツール操作ログを必要としません。

```powershell
learning-engine --db demo.sqlite3 debrief --case DEMO-NEW --text "断面を比較した。側壁が異なるため形状を考慮した計算を試し、最終判断は保留した。"
```

出力されたIDで、対話メニュー7から解釈を入力できます。ファイルで編集する場合は `confirm-debrief --id <debrief_id> --edited examples/procedure_manual.json` を使います。このサンプルは自分の原文と一致するよう編集してください。確認後は `approve-procedure --id <procedure_id> --expert-approved` で参考候補として承認します。

保存状態は `draft/needs_extraction → interpretation_confirmed → approved_candidate → validated_for_scope`。v0.1互換のため `confirm_debrief` の戻り値 `status` は `confirmed_unvalidated` で、正式な状態は `lifecycle_status` にも返します。既存DBの同名旧状態も読み込めます。

観測順序と必須順序を分け、抽出・手動確認時の `required_order` は常に `unknown`。2件以上を `compare-procedures --id <id1> --id <id2>` で比較しても共通手順と分岐は候補のままです。`validate-procedure --id <id> --scope '<JSONまたはファイル>' --evidence <検証資料> --expert-confirmed` で適用範囲を明示できますが、どの状態でも `executable=false` です。

概念提案の例（架空サンプル）：

```powershell
learning-engine --db demo.sqlite3 propose-knowledge --kind concept --case DEMO-NEW --body examples/concept_bottom_radius.json
```

戻り値のIDを `concept-impacts --id <id>` で確認します。専門家が確認後、`adopt-knowledge --id <id> --expert-confirmed` で独立した属性として採用、`--reject` を加えると棄却します。既存概念へ黙ってマージしません。対象範囲は固定属性と値のマッピングで指定し、空の `{}` は全案件を候補にします。

重要な新概念は影響する過去案件を `needs_review` にし、評価時に不足属性を質問します。過去値は補完せずUnknownのまま、回答を確定した後も専門家レビューまでREVIEWにします。新しい案件にも同じ確認条件を適用します。影響検出は指定範囲・関連属性・根拠ケースによる保守的な候補抽出です。

`propose_knowledge` は観測 `observation`、因果仮説 `causal_hypothesis`、計算依存 `computational_dependency`、証拠 `evidence`、概念 `concept` を分離して保存します。証拠は `claim_id` と `polarity=supports/refutes` で主張に結べます。因果仮説の採用には `validation_evidence` と専門家確認が必要です。AIの一般提案 `suggestion` はそのまま検証済み知識に昇格できません。

## 任意のローカルLLM

OpenAI互換のローカルチャットAPIを設定したときだけ通信します。

作業ディレクトリの `.env` を自動で読み込みます。プロジェクトルートに用意した `.env` のURLとモデル名を編集してください（初期状態は未設定です）。設定例は `.env.example` にあります。

```dotenv
LE_LLM_URL=http://localhost:11434/v1
LE_LLM_MODEL=使用中のローカルモデル名
LE_LLM_API_KEY=local
```

```powershell
learning-engine --db demo.sqlite3 interact
learning-engine --db demo.sqlite3 consult --case DEMO-NEW --task correction --text "底部の丸まりも確認が必要だと思う"
# 別の設定ファイルを指定する場合（グローバル引数はサブコマンドの前）
learning-engine --env-file path/to/local.env --db demo.sqlite3 interact
```

優先順位は **CLI引数 > 環境変数 > .env** です。`--llm-url` と `--model` でも指定できます。APIキーは任意の `LE_LLM_API_KEY`（未設定時は `local`）。`.env` はUTF-8で、空行・コメント・引用符・`export KEY=value`に対応します。変数展開やコード実行はしません。`.env` と `.env.*` はGit管理から除外し、`.env.example` だけを共有します。

temperatureパラメータは送信しません。サーバー・モデルの既定値を使用します。画像を利用する場合は画像入力対応モデルとOpenAI互換の `image_url` 入力に対応したサーバーを指定してください。

`response_format` が使えない社内LLMなどでは、`.env` に `LE_LLM_RESPONSE_FORMAT=none` を追加すると、この項目をリクエストから完全に省略します。既定の `json_object` は `response_format: {"type": "json_object"}` を送信します。`chat --response-format none` のようにCLIでも切り替えられ、優先順位はCLI > 環境変数 > .envです。どちらもプロンプトでJSON応答を依頼し、受信したJSONと型の検証を行います。

接続先は明示指定したHTTP(S) URLで、localhostだけでなくLAN内のIPアドレス・ホスト名も利用できます（例：`http://192.168.1.10:11434/v1`）。設定した接続先へ入力内容を送信するため、自分が利用するLLMサーバーを指定してください。別の接続先へのリダイレクトと環境プロキシは無効です。

LLMは作業報告の構造化と相談への提案を生成し、Pythonが型・enum・質問件数を検査します。相談の原文・応答・固定コンテキストと、作業報告の抽出結果はDBに保存されます。ルール判定・履歴・承認・実行ゲートはLLMを使いません。オフラインでは `FakeExtractor(fixed_procedure, suggestions=fixed_suggestions)` を利用できます。実モデルによる出力品質は環境ごとの検証が必要です。

## 判定・回帰・リプレイ

- `decision` は `REUSE_MODEL` 等の業務結論、`gate` は `EXECUTE / EXPLORE / ASK / REVIEW / BLOCK`。EXECUTEは内部の判断継続だけを許可し、外部操作は許可しません。
- Unknownの例外条件をFalseとして親ルールへ戻しません。競合する根・例外はREVIEW、明示BLOCKは探索でも維持します。
- `evaluate --intent explore` はUnknown下でも安全なローカル調査・試算をEXPLOREにします。外部ツールを実行する機能はありません。
- 公開前に、更新済み属性で全承認済みケースを照合します。既存判断の回帰、変更されない既存不一致のどちらも公開を拒否します。未レビュー・disputedを正解に使いません。
- `point_in_time` は各Caseの `occurred_at` を基準に、登録時刻・属性更新時刻・ルール公開/撤回時刻・レビュー時刻を制限します。後から取り込んだCaseは `knowledge_available=false` となり、当時の属性や正解ラベルを捏造しません。
- `current_knowledge`（互換名 `current`）は現在の固定属性と公開ルールで再評価。使用ルール版・証拠・理由を返します。保存済み抽出を再生成しません。
- `evaluate(..., as_of=..., persist=False)` で任意時点を確認できます。API時刻はタイムゾーン付きISO 8601、内部はUTCです。
- `metrics` は誤確定、質問漏れ、不要質問、訂正案件の不一致再発、回帰違反、保留を集計します。不要質問/質問漏れは構造的な代理指標で、未知の業務上の正解を推定した評価ではありません。

## 永続化・互換性

初期Case属性と当時のDecisionスナップショットは保持し、属性更新・専門家レビュー・ルール公開/撤回を追記します。重要操作には監査イベント、ローカル操作主体、システム利用可能時刻が付きます。原文の追加回答・解釈確認・採用前後も保存します。

`learning_engine/migrations.py` が起動時に旧DBへ追加テーブルを作ります。元テーブルの削除はありません。v0.3は会話・確認対象・質問候補・判断経路・証拠参照を追加し、画像対応では画像本体・出典を保持する `conversation_attachments` を追加します。既存監査からレビュー時刻を復元し、ルール公開イベントを取り込みます。監査がない古いレビューの利用可能時刻は推測せず、時点評価では未レビューにします。移行前にはアプリを終了しDBファイルをバックアップしてください。移行台帳 `schema_migrations` の最新は4です。既存ツールとの互換性のため `PRAGMA user_version` は2のまま維持しています。

`learning_engine.Engine` と既存サブコマンドを維持しています。DB接続は `with Engine(...)` または `close()` で閉じてください。

## 実装範囲と検証

| Phase | 実装 |
|---|---|
| 0 | 既存コード監査、名称整合性、既存12テスト、Windows接続終了修正 |
| 1 | demo/interact、フォーム、質問回答、訂正、承認待ち・衝突・探索の永続化と再開 |
| 2 | 固定属性定義・別名・単位・版、証拠と仮説、3値判定、追加型マイグレーション |
| 3 | cornerstone比較、差異選択、解釈確認、RDR草案、回帰検査、公開/撤回履歴 |
| 4 | 自然言語Debrief、手動/LLM抽出、追加回答、確認・候補承認、複数報告比較、範囲検証 |
| 5 | 概念提案・比較・採用/棄却、過去への影響候補、Unknown保持と再レビュー |
| 6 | 2種類のリプレイ、Fake LLM・模擬HTTP、評価指標、ドキュメント |

上表は維持しているv0.2の基盤です。v0.3では自然言語chat、ガイド付きtutorial、理由付き質問候補、RDR参照整合性、逐次リプレイ、条件分岐と意味的な手順比較候補を追加しました。

テストは単体・API E2E・入力フォーム・別プロセスCLI・再起動・旧DB移行を含みます。実モデルの性能評価、複雑な数式条件、意味的な手順統合の一般性検証、因果推論の証明、専門家の認証/合議、暗号化、多人数同時編集、外部ツール実行は未実装です。レビュー正解ラベルは既存互換のCase単位のため、複数の判断対象を厳密に扱う場合は別Caseに分けてください。chatで作るCaseには判断対象も記録し、明示的に別の対象のルール・正解を混ぜないようにしています。

本実装は追跡可能な単体PoCです。今後は評価用の独立した正解データ、判断対象ごとのレビュー、概念の詳細な版移行、実モデルによる日本語抽出品質、専門家間の権限と合議を拡充する余地があります。
