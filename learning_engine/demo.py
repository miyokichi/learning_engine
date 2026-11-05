"""Fictional, deterministic offline demonstration; approvals are sample data."""
from .llm import FakeExtractor

REPORT = "断面画像を確認した。側壁形状が違うため形状を考慮した計算を試し、最終判断を保留した。"
PROCEDURE = {"goal": "Viaモデルの評価", "trigger": "側壁形状の差異",
             "steps": [{"action": "断面画像を確認", "reason": "側壁形状を比較", "result": "形状差あり"},
                       {"action": "形状を考慮した計算", "reason": "形状が異なるため", "result": "最終判断保留"}],
             "outcome": "保留", "decision_criteria": ["形状同等性"],
             "unverified_inferences": [], "missing": ["validation"]}


def run_demo(e):
    note = "架空サンプルです。回答・専門家承認もデモ用データです。"
    if e.store.one("SELECT id FROM cases WHERE id='DEMO-OLD'"):
        if not e.store.one("SELECT object_id FROM audit WHERE event_type='demo_completed'"):
            raise ValueError("Incomplete demo exists; use a new database")
        return {"notice": note, "status": "already_completed", "replay": e.replay()}
    def features(geometry, material="OXIDE_A"):
        return {"via.geometry_equivalence": {"value": geometry, "status": "expert_confirmed" if geometry is not None else "unknown", "source": "fictional_demo"},
                "via.material": {"value": material, "status": "documented" if material else "unknown", "source": "fictional_demo"}}
    e.add_case("DEMO-OLD", features(True))
    e.review_case("DEMO-OLD", "REUSE_MODEL")
    e.add_rule("DEMO-ROOT", "REUSE_MODEL", [{"feature": "via.material", "op": "eq", "value": "OXIDE_A"}], cornerstone_case_id="DEMO-OLD")
    e.publish_rule("DEMO-ROOT", True, True)
    e.add_case("DEMO-NEW", features(False, None))
    first = e.evaluate("DEMO-NEW")
    q = first["questions"][0]
    e.answer_question(q["id"], "架空の資料で材料OXIDE_Aを確認した")
    e.confirm_question_feature(q["id"], "OXIDE_A", expert_confirmed=True)
    before = e.evaluate("DEMO-NEW")
    fid = e.feedback("DEMO-NEW", "側壁形状が異なるため再検証が必要", "REQUIRE_VALIDATION", before["decision_id"])
    context = e.feedback_context(fid)
    e.confirm_feedback(fid, "形状同等性が否定された場合はモデルを再検証する")
    e.review_case("DEMO-NEW", "REQUIRE_VALIDATION")
    preview = e.draft_exception_from_feedback(fid, "DEMO-OLD", ["via.geometry_equivalence"], "DEMO-ROOT", "DEMO-EXCEPTION")
    e.publish_rule("DEMO-EXCEPTION", True, True)
    after = e.evaluate("DEMO-NEW")
    draft = e.report_debrief("DEMO-NEW", REPORT, FakeExtractor(PROCEDURE))
    proc = e.confirm_debrief(draft["id"])
    e.approve_procedure(proc["procedure_id"], True)
    e.add_case("DEMO-NEXT", features(False))
    applied = e.evaluate("DEMO-NEXT")
    e.add_case("DEMO-UNKNOWN", features(None))
    unknown = e.evaluate("DEMO-UNKNOWN")
    with e.store.transaction():
        from .engine import now
        e.store.audit("demo_completed", "demo", {"notice": note}, now())
    return {"notice": note, "question": first, "before_correction": before, "feedback_id": fid,
            "cornerstones": context, "regression": preview, "after_learning": after,
            "debrief": draft, "procedure": proc, "next_case": applied, "unknown": unknown,
            "procedure_references": e.procedures("DEMO-NEXT")}
