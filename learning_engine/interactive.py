"""Resumable text forms; no JSON authoring required."""
import json
from .store import decode
from .engine import new_id


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def yes(prompt):
    return input(prompt + " [y/N]> ").strip().lower() == "y"


def scalar(prompt):
    text = input(prompt + " (true/false/数値/文字列)> ").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def manual_procedure(existing=None):
    body = existing or {}
    print("原文で説明した内容を入力してください。推定した理由は明記してください。")
    goal = input("目的> ") or body.get("goal")
    trigger = input("実施条件> ") or body.get("trigger")
    steps = []
    while True:
        action = input("作業（空欄で終了）> ")
        if not action:
            break
        steps.append({"action": action, "reason": input("理由（不明は空欄）> ") or None,
                      "result": input("結果（不明は空欄）> ") or None})
    return {"goal": goal, "trigger": trigger, "steps": steps or body.get("steps", []),
            "outcome": input("全体の結果> ") or body.get("outcome"),
            "decision_criteria": [s for s in input("判断基準（カンマ区切り）> ").split(",") if s],
            "unverified_inferences": [s for s in input("未確認の推定（カンマ区切り）> ").split(",") if s],
            "missing": [], "required_order": "unknown"}


def correct(e, case_id, feedback_id=None):
    if feedback_id is None:
        conclusion = input("訂正後の結論> ")
        latest = e.store.one("SELECT id FROM decisions WHERE case_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (case_id,))
        fid = e.feedback(case_id, input("訂正理由> "), conclusion, latest["id"] if latest else None)
    else:
        fid = feedback_id
    f = e.store.one("SELECT * FROM feedback WHERE id=?", (fid,))
    show({"feedback_id": fid, "cornerstone_candidates": e.feedback_context(fid)})
    cornerstone = input("比較する承認済みcase_id> ")
    show(e.cornerstone_comparison(case_id, cornerstone))
    fields = [s.strip() for s in input("重要な差異の属性ID（カンマ区切り。候補外は先に概念/質問で登録）> ").split(",") if s.strip()]
    interpretation = f["interpretation"] or input("一文の解釈（編集可）> ")
    show({"interpretation": interpretation, "selected_features": fields})
    if not yes("この解釈と訂正後の正解ラベルを専門家として確認しますか"):
        return
    if not f["confirmed"]:
        e.confirm_feedback(fid, interpretation)
    e.review_case(case_id, f["corrected_to"])
    parent = input("例外を追加する親rule_id> ")
    rid = new_id("R")
    preview = e.draft_exception_from_feedback(fid, cornerstone, fields, parent, rid)
    show(preview)
    if not preview["reviewed_mismatches"] and yes("差異・回帰結果を確認しルールを公開しますか"):
        show(e.publish_rule(rid, True, True))
        show(e.evaluate(case_id))


def answer(e, q):
    show(q)
    if q["status"] == "open":
        e.answer_question(q["id"], input("回答原文> "))
    value = scalar("確認する属性値")
    show({"field": q["field_id"], "interpreted_value": value})
    if yes("回答の構造化解釈を確認しますか"):
        show(e.confirm_question_feature(q["id"], value, expert_confirmed=True))
        show(e.evaluate(q["case_id"]))


def debrief(e, case_id, extractor=None, did=None):
    if not did:
        result = e.report_debrief(case_id, input("何を行い、なぜ選び、結果からどう判断しましたか？\n> "), extractor)
        did = result["id"]
    d = e.store.one("SELECT * FROM debriefs WHERE id=?", (did,))
    show(d)
    interpretation = decode(d["interpretation"])
    if interpretation is None or yes("候補を手動編集しますか"):
        interpretation = manual_procedure(interpretation)
    show(interpretation)
    if yes("原文と解釈を確認しますか"):
        proc = e.confirm_debrief(did, interpretation)
        show(proc)
        if yes("この手順を参考候補として承認しますか（自動実行不可）"):
            show(e.approve_procedure(proc["procedure_id"], True))


def concept(e, case_id):
    text = input("新しい概念・用語の説明> ")
    body = {"text": text, "source": "interactive_expert", "feature_id": input("正規属性ID> "),
            "definition": input("定義> "), "measurement": input("測定基準・角度定義等> "),
            "unit": input("単位（なしは空欄）> ") or None,
            "aliases": [s for s in input("別名（カンマ区切り）> ").split(",") if s],
            "relation_type": input("既存概念との関係（alias/subconcept/distinct）> "),
            "related_features": [s for s in input("関連属性ID（カンマ区切り）> ").split(",") if s]}
    scope_key = input("適用範囲の属性ID（空欄は全案件を影響候補にする）> ")
    body["scope"] = {scope_key: scalar("適用範囲の値")} if scope_key else {}
    body["important"] = yes("既存判断の再レビューが必要ですか")
    proposal = e.propose_knowledge("concept", body, case_id)
    show(proposal)
    show(e.concept_impacts(proposal["id"]))
    if yes("概念を独立した属性定義として承認しますか"):
        show(e.adopt_knowledge(proposal["id"], True))


MENU = """\n1 ケース選択/登録  2 評価  3 質問/回答  4 訂正と対比
5 作業報告  6 未完了一覧  7 IDから再開  8 基本ルール登録
9 概念提案  10 リプレイ  11 安全な探索  12 手順参照/比較
13 専門家レビュー  14 作業報告への追加回答
15 AIへの相談（提案のみ、未確認）
0 終了"""


def interact(e, extractor=None):
    print("Learning Engine 対話CLI。確認は y の明示入力で採用します。")
    if extractor is None:
        print("自動解釈は無効。回答文を保存して、手動で解釈を確認してください。")
    case_id = None
    show({"pending": e.pending()})
    while True:
        print(MENU)
        try:
            cmd = input(f"case={case_id or '-'} > ").strip()
            if cmd == "0":
                return
            if cmd == "1":
                show(e.store.many("SELECT id,review_status FROM cases ORDER BY created_at"))
                selected = input("case_id（新規IDで登録）> ")
                if not e.store.one("SELECT id FROM cases WHERE id=?", (selected,)):
                    metadata = {"target": input("対象> "), "goal": input("目的> "), "scope": input("スコープ> ")}
                    features = {}
                    while True:
                        field = input("属性ID（空欄で終了）> ")
                        if not field:
                            break
                        value = scalar("値（不明はnull）")
                        features[field] = {"value": value, "status": "unknown" if value is None else "assumed", "source": "interactive"}
                        if value is not None and yes("この値を専門家として確認しますか"):
                            features[field]["status"] = "expert_confirmed"
                    show(e.add_case(selected, features, metadata=metadata))
                case_id = selected
            elif cmd == "6":
                show(e.pending())
            elif cmd == "7":
                oid = input("再開するobject_id> ")
                w = e.store.one("SELECT * FROM workflow WHERE object_id=?", (oid,))
                if not w:
                    raise ValueError("Unknown workflow id")
                case_id = w["case_id"]
                if w["state"] in {"question_open", "answer_confirmation"}:
                    answer(e, e.store.one("SELECT * FROM questions WHERE id=?", (oid,)))
                elif w["state"] in {"correction_interpretation", "rule_draft_pending"}:
                    correct(e, case_id, oid)
                elif w["state"] == "debrief_confirmation":
                    debrief(e, case_id, extractor, oid)
                elif w["state"] in {"approval_pending", "regression_conflict"}:
                    show(e.rule_preview(oid))
                    if yes("回帰結果を確認し公開しますか"):
                        show(e.publish_rule(oid, True, True))
                elif w["state"] == "procedure_approval":
                    show(e.store.one("SELECT * FROM procedure_candidates WHERE id=?", (oid,)))
                    if yes("手順候補を承認しますか"):
                        show(e.approve_procedure(oid, True))
                elif w["state"] == "knowledge_confirmation":
                    show(e.store.one("SELECT * FROM knowledge WHERE id=?", (oid,)))
                    if yes("概念・証拠の内容を承認しますか"):
                        show(e.adopt_knowledge(oid, True))
                else:
                    show(w)
            elif cmd == "10":
                show({"replay": e.replay(), "metrics": e.metrics()})
            elif not case_id:
                print("まず1でケースを選択してください。")
            elif cmd in {"2", "11"}:
                show(e.evaluate(case_id, intent="explore" if cmd == "11" else "commit"))
                show({"procedure_references": e.procedures(case_id)})
            elif cmd == "3":
                questions = e.store.many("SELECT * FROM questions WHERE case_id=? AND status IN ('open','answered_unverified') ORDER BY created_at", (case_id,))
                show(questions)
                qid = input("回答するquestion_id（空欄で戻る）> ")
                if qid:
                    q = next(q for q in questions if q["id"] == qid)
                    answer(e, q)
            elif cmd == "4":
                correct(e, case_id)
            elif cmd == "5":
                debrief(e, case_id, extractor)
            elif cmd == "8":
                conclusion = input("ルール結論> ")
                field = input("条件の属性ID> ")
                value = scalar("一致する値")
                rid = new_id("R")
                if yes("現在ケースの正解ラベルとして結論を確認しますか"):
                    e.review_case(case_id, conclusion)
                    show(e.add_rule(rid, conclusion, [{"feature": field, "op": "eq", "value": value}], cornerstone_case_id=case_id))
                    if yes("回帰結果と適用条件を確認して公開しますか"):
                        show(e.publish_rule(rid, True, True))
            elif cmd == "9":
                concept(e, case_id)
            elif cmd == "12":
                show(e.procedures(case_id))
                ids = input("比較するprocedure_id（2件以上、カンマ区切り。空欄で戻る）> ")
                if ids:
                    show(e.compare_procedures([s.strip() for s in ids.split(",")]))
            elif cmd == "13":
                conclusion = input("確認した正解ラベル> ")
                status = input("expert_approved / expert_rejected / disputed> ")
                if yes("この専門家レビューを記録しますか"):
                    e.review_case(case_id, conclusion, status)
                    show(e.evaluate(case_id))
            elif cmd == "14":
                did = input("debrief_id> ")
                show(e.extend_debrief(did, input("追加回答原文> "), extractor))
            elif cmd == "15":
                show(e.suggest(case_id, input("相談・回答・訂正理由> "), extractor))
        except (EOFError, KeyboardInterrupt):
            print("\n保存済みの状態から次回再開できます。")
            return
        except (ValueError, KeyError, PermissionError, StopIteration) as exc:
            print(f"操作を完了できませんでした: {exc}")
