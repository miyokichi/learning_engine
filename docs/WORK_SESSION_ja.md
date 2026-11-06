# Learning Engine v0.4 WorkSession利用ガイド

仕事の目的を入力して開始し、その途中の判断、質問、回答、作業報告から学習します。Caseを先に登録する必要はありません。Learning Engineは学習状態を管理し、外部ツールや計算プログラムは実行しません。

## 起動

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\learning-engine.exe --db work.sqlite3 work
```

最初に「Via抵抗を計算したい」のように目的を話してください。既存の仕事がある場合は番号で再開できます。LLMを設定すると、判断依頼、作業報告、回答、完了を自然言語で解釈します。表示された解釈は「確認」で採用し、違えば確認せず説明し直します。LLMなしでも目的と作業報告を保存し、`/complete`で手順候補を作成できます。

- `/status`：保存した仕事、回答、チェックポイントを確認。
- `/resume`：学習側の作業状態を再開。外部作業を実行する命令ではありません。
- `/complete`：仕事を完了し、学習候補をレビュー。
- `/exit`：保存して終了。

作業終了時の候補は、承認・修正・保留・却下を選べます。全候補を表示した後に「まとめて承認」も可能です。修正はLLM接続時に文章で入力し、修正案を「確認」して承認します。未承認の候補はルールや参考手順に反映されません。作業中の回答確認を正式なルール公開承認として使いません。

LLMは従来の `.env` 設定を利用します。`response_format` は既定で送信しません。実モデルの精度は、固定応答による自動テストとは別に評価してください。

## Python API

```python
from learning_engine import Engine, WorkSession

with Engine('work.sqlite3') as engine:
    work = WorkSession(engine)  # extractor=... で任意のLLMを渡す
    session = work.start_work('Via抵抗計算', {'material': '未確認'})
    wid = session['work_id']
    decision = work.request_decision(
        wid, '既存抵抗モデルの利用', {},
        checkpoint={'task': 'resistance', 'next_step': 'compare_cross_sections'},
        idempotency_key='decision-1')
    # decision['questions'] は最大2件。原文と確認済み解釈は別に保存する。
    qid = decision['questions'][0]['question_id']
    work.record_answer(wid, qid, '判断条件はまだ不明', idempotency_key='answer-1')
    work.confirm_answer(wid, qid, {'value': None}, expert_confirmed=True)
    context = work.get_resume_context(wid)
    # 外部エージェントは context を読み、再開の判断と実行を自分で行う。
```

公開操作は `start_work`、`record_step`、`request_decision`、`record_answer`、`confirm_answer`、`get_work_status`、`get_resume_context`、`pause_work`、`resume_work`、`complete_work`、`generate_reflection`、`list_learning_candidates`、`review_learning_candidate`、`get_reusable_knowledge` です。

`known_facts`の値を単なる文字列・真偽値で渡すと仮定として扱います。確認済み情報には `{'value': True, 'status': 'documented', 'source': '資料名'}` のように出典と状態を渡してください。外部エージェントには入力の真正性を保証する責任があります。

`record_step`にはaction、reason、result、evidenceを渡します。`state`はexecuted / planned / hypothesisで、後者2つは実施済み手順にしません。`source`、`occurred_at`も指定可能です。記録時刻はシステムが付与します。

`start_work`、`request_decision`、`record_step`、`record_answer`には冪等性キーを渡せます。同じキーを別内容に再利用すると拒否します。完了結果と確定回答は不変です。回答の修正は確認前に行ってください。

回答解釈の `rule_candidate` にconclusion、predicates、reasonを含めると、終了時にルール候補になります。回答確認だけでは公開されません。LLMがある場合は `reflect_work` による判断・手順・概念・観察・因果仮説の候補も得られます。候補の採用は `expert_confirmed=True` を伴う人間のレビューだけが行います。

状態READYは知識から判断可能、NEED_INPUTは質問待ち、REVIEWは人間の判断待ち、EXPLOREは調査候補、BLOCKは継続不可です。回答が確認され、未回答質問がなくなるとREADY_TO_RESUMEになります。いずれも外部ツール実行権限を付与しません。Unknownの回答を確認しても、業務上の不明点が解決したという意味にはなりません。

## 架空デモと検証

```powershell
.\.venv\Scripts\python.exe examples/work_agent.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

モックエージェントが架空Via抵抗計算を依頼し、側壁の質問・回答・確認・再開・作業報告・振り返り・候補承認を行い、次の仕事でREQUIRE_VALIDATIONを再利用します。専門家の回答と承認も架空です。実計算や実業務の正解を提供するものではありません。

## v0.3との差分と現時点の制約

WorkSessionは仕事全体、Caseは個別判断として分離しました。状態・イベント・Case関連・回答・振り返り・候補を追加型のDB移行5で保存します。既存のchat/APIと `PRAGMA user_version=2` は維持します。移行前にはアプリを終了し、DBをバックアップしてください。

振り返りは確認された回答と実施済みステップから決定的な候補を作り、LLMがあれば意味的な候補を補います。ルールは確認済みカテゴリ・真偽値の等値条件に限定し、例外では既存RDR比較を使い、公開時に回帰検査します。手順は必須順序をunknownとし、自動実行しません。知識検索は関連性と適用可能性を分け、参考手順と承認済み事例には関連候補であることを明記します。

残課題：実LLMの未知検出・概念抽出品質の独立評価、意味に基づく検索順位付け、複雑な数値境界の検証UI、完了後の追加質問から振り返り版を作り直す操作、候補修正の詳細な編集履歴、外部エージェントの実行状態との統合。LLM内部状態は復元しません。Adapter、MCP、NEXUS SEED、little_agent連携は含みません。
