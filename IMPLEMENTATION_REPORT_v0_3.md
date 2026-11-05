# Learning Engine v0.3 実装報告

検証日：2026-11-06（日本時間）。独立したPythonパッケージ `learning-engine==0.3.0` として実装・インストールしました。

## 最初に実行する3つのコマンド

このワークスペースでは `.venv` を準備済みです。プロジェクトルートで実行してください。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\learning-engine.exe --db tutorial.sqlite3 tutorial
.\.venv\Scripts\learning-engine.exe --db my_learning.sqlite3 chat
```

新規cloneからのvenv作成は [使い方ガイド](docs/USER_GUIDE_ja.md) に記載しました。tutorialはLLM不要で、画面ごとに `確認` を入力します。chatの自由な日本語の自動解釈には `.env` のLLM設定が必要です。教材DBと自分のDBを分けてください。

## Phase 0：実装前の確認

実際の作業ベースはローカルリポジトリの `b075cc3` でした。指示書のv0.2に加え、既に `.env` 読み込みと、明示したLAN内IP・ホスト名への接続対応が入っていました。指示書全体、既存コード、README、テストを確認し、変更前に **52テストすべて成功**を確認しました。

会話の意図抽出と操作の連結、セッション再開、既知ルール外の質問計画、実際の逐次学習実験、初心者向けガイドが不足していました。RDRは比較Caseと親ルールの判断経路の関連を十分検査していませんでした。既存機能を再利用し、追加型で改善しました。

## 追加・変更したファイル

| ファイル | 主な内容 |
|---|---|
| `conversation.py` | 自然言語chat、セッション・原文・解釈・確認対象、訂正と対比、Engine APIの呼び出し、再開 |
| `conversation_schema.py` | 限定された意図enumと引数検証、会話用LLMプロンプト |
| `question_planner.py` | 根拠・理由・影響・優先度付き質問、重複抑制、新概念候補、複数仮説の区別 |
| `tutorial.py` | 日本語7段階の教材、途中再開・冪等性、架空承認の明示 |
| `replay_experiment.py` | 隔離SQLiteでの逐次実験、時刻・train/test分離、固定baseline比較と指標 |
| `demo_conversation.py` / `demo_experiment.py` | ドメイン固有の架空教材・固定応答。コア判定から分離 |
| `engine.py` / `logic.py` | 判断経路、RDR参照整合性、証拠参照、時刻/ID注入、明示された判断対象の分離 |
| `knowledge.py` / `llm.py` | 条件分岐・観察・終了条件・来歴、意味的比較候補、会話・比較LLMインターフェース |
| `migrations.py` / `store.py` | 会話等の追加テーブル、初期化失敗時の接続解放 |
| `cli.py` | chat / tutorial / doctor / replay-experiment、既存コマンド維持 |
| `pyproject.toml` / `__init__.py` | バージョン0.3.0、READMEメタデータ |
| `tests/test_v03_*.py` | 27件の追加検証。既存52件も維持 |
| `docs/*.md` / `README.md` | 日本語の導入、教材、概念、トラブル対応 |
| `examples/chat_sample.txt` / `replay_sequence.json` | 会話の実入力と逐次イベントfixture |
| `examples/*_output_ja.txt` | PowerShellで実行した教材の出力記録 |

元の `.gitignore` はexamples全体を除外していました。新規cloneでも文書とテストが成立するよう、確認済みの架空サンプルだけを共有対象にする例外を追加しました。その他のローカルexamplesは引き続き除外します。

## Phase 1：RDR・DB・Evidence

判断の `rule_path` を決定的に取得し、新しいDecisionの詳細を `decision_details` に保存します。例外草案作成と公開時に、親の判断対象・公開状態、比較Caseと訂正Caseの経路を照合します。Feedbackに元Decisionがある場合は、保存済みの経路を使用します。旧Decisionに詳細がなければ、そのスナップショットと当時のルールから再構成します。元Decisionが指定されない従来APIでは、現在の固定条件による経路照合を行います。

無関係なコーナーストーン、異なる判断対象、同一Caseとの例外対比を拒否します。更新済み属性を使う全承認済みCaseの回帰・既存不一致検査を維持し、矛盾する草案は公開しません。

新しい属性の登録・回答確認で `evidence_refs` を追加します。出典、属性状態、取得時点、利用可能時点を保存し、Decisionにも参照を残します。`reported_status_only` / `authenticity_verified=false` とし、状態ラベルから資料の真正性を保証しません。既存属性のsourceは維持し、古いデータに偽の検証時刻を作りません。

## Phase 2：chatとtutorial

chatは薄いコーディネータです。LLMが返せるのは限定された意図と候補だけで、SQLや任意関数名を実行しません。原文はモデル呼び出し前にコミットします。失敗した更新は取り消し、原文・エラー・現在の確認対象を残します。

`new_case / evaluate / answer / correction / debrief / concept / ask_about_knowledge / clarify / cancel` を扱います。ユーザーは通常、Case ID・Rule ID・JSONを入力しません。会話を案件からでも作業報告からでも始められます。訂正の比較には、更新前の判断経路と、まだ未確認の観察候補を区別して表示します。

`確認` または専用の `y` だけが、現在の一つのPendingActionへの人間の確認です。`OK`・「そうですね」・モデル出力は承認になりません。解釈確認、ルール公開、手順候補承認を分け、確認・取消・差し替えを記録します。処理が失敗した場合は、承認と状態更新も一緒にロールバックします。

`chat --session` と、通常起動時の前回会話選択を実装しました。確認対象・質問を保存状態から復元し、二重承認・二重公開・同じ未確認回答への質問の繰り返しを抑制します。LLM未設定時は、自動解釈が無効であることを明示し、tutorialとinteractへ案内します。

### tutorialの実際の出力概要

7つの画面で、初期ルール、ASK、回答確認、訂正対比、回帰と公開、次案件、自然言語報告を順に体験できます。

```text
判断：ASK / 理由：材料が未確認です。資料で材料を確認できますか？
判断：REUSE_MODEL / 状態：EXECUTE（内部の判断を進められる、という意味です）。
比較：過去は形状同等性=true、今回はfalse。
例外草案：再検証が必要。回帰違反：0件。公開前です。
未経験案件の判断：REQUIRE_VALIDATION。過去の正しい再利用判断も維持されました。
保存状態：ルール=公開済み、手順=approved_candidate、必須順序=unknown、実行不可。
```

実出力を [tutorial_output_ja.txt](examples/tutorial_output_ja.txt) に保存しました。画面ごとに架空教材の操作であることを示し、通常は人間の `確認` で進みます。`--auto` で自動化する場合も架空の回答・承認だと表示します。再実行してもデータは重複しません。

### chatの代表的な会話と判断変化

固定教材で実行した流れです。一般的な自然言語理解の品質を実証したものではありません。

1. 「モデルの再利用を判断したい。材料はAで形状は同じです。」→解釈確認後、ルール不足でREVIEW。
2. 「この材料Aなら再利用できる。材料を条件として覚えて。」→解釈確認→草案→公開確認→REUSE_MODEL。
3. 「別の案件です。材料はA、形状の同等性は未確認です。」→未確認の重要条件候補を質問し、会話の状態はASK。
4. 「断面を見たら同じでした。」→回答の解釈確認→EXECUTE。
5. 「見直すと形状が違いました。再利用を保留して再検証してください。」→過去のtrueと今回のfalse候補を比較→重要差異・解釈確認→草案→回帰→公開。
6. 「次の案件も材料はAで形状が異なります。」→REQUIRE_VALIDATIONを適用。
7. 作業報告→解釈確認→参考手順承認。自動実行は不可。

入力は [chat_sample.txt](examples/chat_sample.txt)、実出力は [chat_sample_output_ja.txt](examples/chat_sample_output_ja.txt) です。

## Phase 3：未知探索と質問計画

既知ルールのUnknownだけでなく、過去Caseとの差、判断説明の不足、複数の未検証仮説、新概念から候補を作ります。質問には `target_decision / missing_information / why_needed / possible_answers / how_answer_changes_next_step / source_case_or_rule / status / priority` を保持します。

critical / useful / optional順に最大2件を選び、回答済み・確認済み情報を抑制します。既に公開ルールで扱う差異の重要性を再質問しないよう、古い質問をsupersededにします。LLMが未知属性を提案した場合は、概念候補をdraftで保存し、正式な属性辞書へ追加しません。重要な未確認候補がある場合、会話では確定を保留します。

Safeな内部探索ではEXPLOREを提示できます。未知をfalseにせず、根拠のない数値閾値・正式条件は採用しません。

## Phase 4：逐次リプレイ

従来の `point_in_time` / `current_knowledge` を維持し、別の `replay-experiment` を追加しました。本番DBを引数で受け取らず、CLIの `--db` も開きません。実験ごとに一時SQLiteを作り、終了時に閉じて削除します。

Case公開、推論、専門家回答、正解開示、草案、訂正確認、公開のイベントを `available_at` 順で適用します。`event_time` は別に保存し、過去の出来事を後から登録しても先の知識に逆流させません。trainの回答・訂正だけを学習に使い、testの正解は推論後の採点用辞書にのみ保持します。

baselineへ渡すのも、その時点で公開済みのCaseと承認済み事例だけです。既定のbaselineはLLMを模した固定の事例参照モデルで、実LLMではありません。Pythonの `predict(context)` を実装した固定/録画ポリシーに差し替えられます。

Engineに時刻とID生成器の任意注入を追加し、同じfixtureを再実行すると同じ判断ログ・スコアになります。fixtureのSHA-256も出力します。生産会話ログから実験イベントを自動生成する機能は今回実装していません。

架空fixtureの実測結果：

| 指標 | 事例参照の固定baseline | Learning Engine |
|---|---:|---:|
| 採点対象の推論イベント | 6 | 6 |
| 誤確定（学習前を含む） | 4 | 1 |
| 保留 | 1 | 2 |
| fixture上で正しい質問 | 0 | 1 |
| 不要質問 | 0 | 0 |
| 開示した専門家回答 | 1 | 1 |
| 未経験testの正答 / 対象 | 0 / 1 | 1 / 1 |
| 同一訂正Caseでの訂正後の誤確定再発 | 1 | 0 |
| 完了率 | 5 / 6 | 4 / 6 |

分母はexpert_approvedの正解が後で開示された推論イベントです。未レビュー・disputedを正解にしません。質問の正誤はfixtureの `needed_fields`、再発は同一Caseの訂正後の推論として定義しています。保留を増やすことと、根拠不足の誤確定を減らすことを区別して評価できます。少数の人工ケースの結果であり、実LLM・実業務の優位性の主張ではありません。

## Phase 5：自然言語の手順学習

従来の項目を維持し、`steps[].input / observation`、`decision_points / branches / stop_conditions / unknowns / provenance` を追加しました。報告原文・追加回答・候補・解釈確認を残します。

例えば「断面画像を比較し、形状が異なるので形状計算を選び、検証不足なら保留した」から、目的・観察・計算選択条件・保留条件を未検証候補として整理できます。条件不明なら「どの条件でその計算を選んだか」「どんな結果なら省略・終了できるか」を最大2問で尋ねます。

複数報告の「断面を確認」「断面画像を見た」を同義候補とする機能を、LLMまたは手動の比較候補で提供しました。比較対象のID・元の作業名を検査し、目的の一致・条件の違いも候補として保存します。少数報告から必須順序は決めず、分岐はunverified、Procedureは常に実行不能です。

## Phase 6：日本語ドキュメント

READMEの最初に3つのコマンドを置き、[USER_GUIDE_ja.md](docs/USER_GUIDE_ja.md)、[TUTORIAL_ja.md](docs/TUTORIAL_ja.md)、[CONCEPTS_ja.md](docs/CONCEPTS_ja.md)、[TROUBLESHOOTING_ja.md](docs/TROUBLESHOOTING_ja.md) を追加しました。

入口の違い、DBと再開、状態と業務結論、作業の教え方、訂正・承認、新概念、`.env`、LLMなしの制限、実出力、次に試すことを記載しました。doctorはDBと設定を通信なしで確認し、`--check-llm` を明示した場合だけ架空メッセージで疎通します。

## Phase 7：実行した検証

環境はWindows PowerShell / CPython 3.13.13です。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q learning_engine examples tests
git diff --check
```

標準のeditable install成功。ソース版とインストール済みメタデータの両方が0.3.0で一致することも検査しました。**全79件成功、失敗0件、スキップ0件**です。

| テスト群 | 件数 | 結果 |
|---|---:|---|
| 既存v0.2、設定、LAN接続形式、旧CLI E2E | 52 | 成功 |
| v0.3 会話、RDR整合性、tutorial | 10 | 成功 |
| v0.3 インストール済みCLI・版・DB移行 | 7 | 成功 |
| v0.3 質問計画・手順の意味的比較候補 | 5 | 成功 |
| v0.3 逐次実験・未来リーク・train/test分離 | 5 | 成功 |

実際のPowerShellパイプでもUTF-8の確認入力でtutorialを完走し、会話教材の入力ファイルからchatを完走しました。CLI E2Eはソース外の一時ディレクトリでインストール済み実行ファイルを使い、再起動・データ保持・二重公開防止まで確認しています。

## 互換性と注意点

- 既存 `Engine`、demo、interact、個別コマンド、従来replayを維持。Engineのclock/id_factoryは任意のキーワード引数です。
- v0.3のDB移行は追加型です。旧Case・Rule・Feedback・Debrief・Procedure・Auditの全行が移行前後で一致するテストを実行しました。
- 移行台帳 `schema_migrations` に3を追加します。既存互換テスト・ツールとの整合のため `PRAGMA user_version` は2を維持し、台帳を正式な移行版とします。
- 不正なRDR比較や未公開の親への例外作成は、従来より厳しく拒否します。既存の公開ルールを書き換えたり、無断撤回したりはしません。
- chatのCaseには判断対象を保存し、明示的に別対象の正解を回帰へ混ぜません。旧Caseの判断対象は推測せず、従来の扱いを維持します。
- `.env` と既存のユーザーDBを検証用に変更していません。DBは利用開始前にバックアップを推奨します。
- 指示書にはloopback制限維持の記述がありますが、以前のユーザー指示による明示LAN接続対応を優先して維持しました。無設定通信・リダイレクトは行いません。

## 未実装・未検証・今後の課題

- 実ローカルLLMへの自由会話・日本語抽出品質は**未検証**です。Fake/固定会話と模擬HTTPを使用しました。doctorの明示疎通と、使い方ガイドの実モデル手動確認手順を提供しています。
- Python 3.11そのもの、Linux/macOSでの実行は未検証です。Python 3.11以上を対象とした標準ライブラリ中心の実装です。
- 未知の発見は、ルール、差異、保存された仮説、LLM提案に依存します。未知の未知を漏れなく発見する保証はありません。
- 意味的な作業統合は候補生成までです。専門家の合議、一般性の証明、必須依存順序の自動確定は未実装です。
- 会話ログ全体からの学習実験の自動生成、実LLM同士の大規模比較、数値境界の自動学習は未実装です。
- 保存済み正解はCase単位です。複数対象の厳密な独立レビューは別Caseに分けてください。
- 単一ローカルユーザーを前提とし、多人数・同じ会話への同時操作、認証、暗号化、監査の耐改ざん性は対象外です。
- 外部システム接続、Adapter、MCP/A2A、GUI、常駐サーバー、外部ツール実行は指定どおり実装していません。

## 再現方法

教材の自動再生と実験は次で確認できます。回答・承認はすべて架空サンプルです。

```powershell
.\.venv\Scripts\learning-engine.exe --db tutorial.sqlite3 tutorial --auto
$env:PYTHONIOENCODING = "utf-8"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
Get-Content -Encoding UTF8 examples/chat_sample.txt | .\.venv\Scripts\learning-engine.exe --db chat_practice_auto.sqlite3 chat --sample
.\.venv\Scripts\learning-engine.exe replay-experiment --scenario examples/replay_sequence.json
```

chatの固定教材を最初から再実行するときは空の別DBを指定してください。tutorialは同じDBで再実行・再開できます。
