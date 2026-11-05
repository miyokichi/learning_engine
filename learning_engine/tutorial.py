"""Japanese guided, resumable fictional learning cycle. No real expert labels."""
from .demo import PROCEDURE, REPORT
from .llm import FakeExtractor
from .store import encode, decode

NOTICE = "これは架空のVia教材です。回答・承認は教材のサンプルで、実際の専門家レビューではありません。"


def tutorial(engine, auto=False):
    e = engine
    print("Learning Engine はじめての学習体験（約5分）")
    print(NOTICE)
    row = e.store.one("SELECT * FROM tutorial_runs WHERE id='v0.3'")
    if not row:
        if e.store.one("SELECT id FROM cases WHERE id LIKE 'TUTORIAL-%'"):
            raise ValueError("教材用IDが使われています。--dbで別の教材DBを指定してください")
        with e.store.transaction():
            e.store.conn.execute("INSERT INTO tutorial_runs VALUES('v0.3',0,'{}')")
        row = {"stage": 0, "body": "{}"}
    stage, state = row["stage"], decode(row["body"])
    titles = [
        ("1/7 過去の判断を準備する", "材料OXIDE_Aの過去事例ではモデルを再利用できた、という架空の正解と初期ルールを登録します。"),
        ("2/7 なぜ質問するのか", "新案件は側壁形状が異なり、材料は未確認です。分からない材料を推測せずASKにします。"),
        ("3/7 回答と解釈を確認する", "教材の回答は『資料で材料OXIDE_Aを確認した』。この解釈を確認すると、初期ルールは再利用を提案します。"),
        ("4/7 訂正から重要な違いを学ぶ", "今回は過去事例と側壁形状が同等ではありません。重要な差異を『形状同等性=false』と指定し、再検証が必要という解釈を確認します。まだ公開しません。"),
        ("5/7 回帰を確認して別案件に応用する", "過去の正しい再利用判断を壊さないことを検査して例外を公開します。別の新案件にも同じ条件を適用します。"),
        ("6/7 作業を言葉で教える", "『断面画像を確認し、形状が異なるので形状を考慮した計算を試し、判断を保留した』という原文を保存し、手順の候補を作ります。操作ログは不要です。"),
        ("7/7 候補と検証済みを区別する", "教材の手順解釈を確認して参考候補として承認します。必須順序・条件分岐の一般性は未検証で、自動実行はできません。"),
    ]
    def features(geometry, material="OXIDE_A"):
        return {"via.geometry_equivalence": {"value": geometry, "status": "expert_confirmed", "source": "fictional_tutorial"},
                "via.material": {"value": material, "status": "unknown" if material is None else "documented", "source": "fictional_tutorial"}}
    while stage < len(titles):
        title, explanation = titles[stage]
        print("\n" + title + "\n" + explanation)
        print("架空の教材データとして進めます。")
        if not auto:
            try:
                token = input("この教材操作を進める場合は『確認』、中断はEnter> ").strip()
            except (EOFError, KeyboardInterrupt):
                token = ""
            if token != "確認":
                print("中断しました。同じDBでtutorialを起動すると続きから再開します。")
                return
        else:
            print("--auto：架空の回答・承認を自動再生します。")
        with e.store.transaction():
            if stage == 0:
                e.add_case("TUTORIAL-OLD", features(True), metadata={"source": "fictional_tutorial"})
                e.review_case("TUTORIAL-OLD", "REUSE_MODEL")
                e.add_rule("TUTORIAL-ROOT", "REUSE_MODEL", [{"feature": "via.material", "op": "eq", "value": "OXIDE_A"}], cornerstone_case_id="TUTORIAL-OLD")
                e.publish_rule("TUTORIAL-ROOT", True, True)
                print("増えた知識：材料がOXIDE_Aなら再利用する初期ルール（教材内の承認済み）。")
            elif stage == 1:
                e.add_case("TUTORIAL-NEW", features(False, None), metadata={"source": "fictional_tutorial"})
                result = e.evaluate("TUTORIAL-NEW")
                state["question_id"] = result["questions"][0]["id"]
                print("判断：ASK / 理由：材料が未確認です。資料で材料を確認できますか？")
            elif stage == 2:
                e.answer_question(state["question_id"], "教材の架空回答：資料で材料OXIDE_Aを確認した")
                e.confirm_question_feature(state["question_id"], "OXIDE_A", expert_confirmed=True)
                result = e.evaluate("TUTORIAL-NEW")
                state["decision_id"] = result["decision_id"]
                print("判断：REUSE_MODEL / 状態：EXECUTE（内部の判断を進められる、という意味です）。")
            elif stage == 3:
                fid = e.feedback("TUTORIAL-NEW", "教材の訂正：側壁形状が異なるので再検証", "REQUIRE_VALIDATION", state["decision_id"])
                e.confirm_feedback(fid, "形状同等性が否定されたらモデルを再検証する")
                e.review_case("TUTORIAL-NEW", "REQUIRE_VALIDATION")
                preview = e.draft_exception_from_feedback(fid, "TUTORIAL-OLD", ["via.geometry_equivalence"], "TUTORIAL-ROOT", "TUTORIAL-EXCEPTION")
                print("比較：過去は形状同等性=true、今回はfalse。")
                print(f"例外草案：再検証が必要。回帰違反：{len(preview['regressions'])}件。公開前です。")
            elif stage == 4:
                e.publish_rule("TUTORIAL-EXCEPTION", True, True)
                e.add_case("TUTORIAL-NEXT", features(False), metadata={"source": "fictional_tutorial"})
                result = e.evaluate("TUTORIAL-NEXT")
                print(f"未経験案件の判断：{result['decision']}。過去の正しい再利用判断も維持されました。")
            elif stage == 5:
                draft = e.report_debrief("TUTORIAL-NEW", REPORT, FakeExtractor(PROCEDURE))
                state["debrief_id"] = draft["id"]
                print("保存：人間の原文と未確認の手順候補（draft）。")
                for question in draft["followups"]:
                    print("追加質問：" + question)
            elif stage == 6:
                proc = e.confirm_debrief(state["debrief_id"])
                e.approve_procedure(proc["procedure_id"], True)
                print("保存状態：ルール=公開済み、手順=approved_candidate、必須順序=unknown、実行不可。")
            stage += 1
            e.store.conn.execute("UPDATE tutorial_runs SET stage=?,body=? WHERE id='v0.3'", (stage, encode(state)))
    print("\n教材は完了済みです。再実行しても教材データは重複しません。")
    print(NOTICE)
    print("次は自分用の別DBでchatを起動し、判断したいことや作業報告を話してください。LLMなしの場合はinteractを使えます。")
