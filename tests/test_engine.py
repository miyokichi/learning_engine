import tempfile
import unittest
from pathlib import Path
from learning_engine import Engine


def features(equivalence, material="OXIDE_A"):
    return {
        "via.geometry_equivalence": {"value": equivalence, "status": "expert_confirmed" if equivalence is not None else "unknown", "source": "demo"},
        "via.material": {"value": material, "status": "documented", "source": "demo"},
    }

class FakeExtractor:
    def extract(self, text):
        return {"trigger": "Viaの評価", "goal": "成立性の判断",
                "steps": [{"action": "断面図を確認", "reason": "モデルの妥当性を見るため", "result": "モデルを保留"}],
                "outcome": "再検証が必要", "decision_criteria": ["形状の同等性"],
                "unverified_inferences": [], "missing": []}

class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.e = Engine(str(Path(self.tmp.name) / "test.db"))
        self.addCleanup(self.e.close)
        self.e.add_case("OLD", features(True), occurred_at="2026-01-01T00:00:00+00:00")
        self.e.add_case("NEW", features(False), occurred_at="2026-02-01T00:00:00+00:00")
        self.e.review_case("OLD", "REUSE_MODEL")

    def root(self):
        self.e.add_rule("ROOT", "REUSE_MODEL", [{"feature": "via.material", "op": "eq", "value": "OXIDE_A"}], cornerstone_case_id="OLD")
        return self.e.publish_rule("ROOT", True, True)

    def test_rule_correction_and_cornerstone_replay(self):
        self.root()
        self.assertEqual(self.e.evaluate("NEW", persist=False)["decision"], "REUSE_MODEL")
        self.e.review_case("NEW", "REQUIRE_VALIDATION")
        fb = self.e.feedback("NEW", "側壁が異なるのでM1は要再検証", "REQUIRE_VALIDATION")
        with self.assertRaises(ValueError):
            self.e.add_rule("CHILD", "REQUIRE_VALIDATION", [{"feature": "via.geometry_equivalence", "op": "eq", "value": False}],
                            parent_id="ROOT", cornerstone_case_id="OLD", corrected_case_id="NEW", feedback_id=fb)
        self.e.confirm_feedback(fb, "形状同等性が否定されたケースはモデル再検証")
        preview = self.e.add_rule("CHILD", "REQUIRE_VALIDATION", [{"feature": "via.geometry_equivalence", "op": "eq", "value": False}],
                                  parent_id="ROOT", cornerstone_case_id="OLD", corrected_case_id="NEW", feedback_id=fb)
        self.assertEqual(preview["regressions"], [])
        self.assertIn("NEW", [x["case_id"] for x in preview["affected_cases"]])
        self.e.publish_rule("CHILD", True, True)
        self.assertEqual(self.e.evaluate("OLD", persist=False)["decision"], "REUSE_MODEL")
        self.assertEqual(self.e.evaluate("NEW", persist=False)["decision"], "REQUIRE_VALIDATION")
        self.assertTrue(all(r["matches_review"] for r in self.e.replay("current")))

    def test_feedback_to_rule_without_manual_rule_json(self):
        self.root()
        self.e.review_case("NEW", "REQUIRE_VALIDATION")
        fid = self.e.feedback("NEW", "形状が違う", "REQUIRE_VALIDATION")
        self.e.confirm_feedback(fid, "形状非同等の時は再検証")
        drafted = self.e.draft_exception_from_feedback(fid, "OLD", ["via.geometry_equivalence"], "ROOT", "R-AUTO")
        self.assertEqual(drafted["draft_predicates"][0]["value"], False)
        self.e.publish_rule("R-AUTO", True, True)
        self.assertEqual(self.e.evaluate("NEW", persist=False)["decision"], "REQUIRE_VALIDATION")

    def test_unknown_is_not_false(self):
        self.root()
        self.e.add_case("MISSING", features(None))
        self.e.review_case("NEW", "REQUIRE_VALIDATION")
        self.e.add_rule("CHILD", "REQUIRE_VALIDATION", [{"feature": "via.geometry_equivalence", "op": "eq", "value": False}],
                        parent_id="ROOT", cornerstone_case_id="OLD", corrected_case_id="NEW")
        self.e.publish_rule("CHILD", True, True)
        result = self.e.evaluate("MISSING")
        self.assertEqual(result["decision"], "ASK")
        self.assertEqual(result["unknown_fields"], ["via.geometry_equivalence"])
        self.assertEqual(len(self.e.store.many("SELECT * FROM questions")), 1)

    def test_regression_requires_refinement(self):
        self.root()
        self.e.review_case("NEW", "REQUIRE_VALIDATION")
        self.e.add_rule("BAD", "REQUIRE_VALIDATION", [{"feature": "via.material", "op": "eq", "value": "OXIDE_A"}],
                        parent_id="ROOT", cornerstone_case_id="OLD", corrected_case_id="NEW")
        preview = self.e.rule_preview("BAD")
        self.assertTrue(preview["regressions"])
        with self.assertRaises(ValueError):
            self.e.publish_rule("BAD", True, True)

    def test_narrative_needs_expert_confirmation(self):
        draft = self.e.report_debrief("NEW", "断面を見て、形状が違うので計算を見直した", extractor=FakeExtractor())
        self.assertEqual(draft["status"], "draft")
        p = self.e.confirm_debrief(draft["id"])
        self.assertEqual(p["status"], "confirmed_unvalidated")
        with self.assertRaises(PermissionError):
            self.e.approve_procedure(p["procedure_id"])
        approval = self.e.approve_procedure(p["procedure_id"], True)
        self.assertFalse(approval["executable"])
        self.assertEqual(self.e.store.one("SELECT source_type FROM debriefs WHERE id=?", (draft["id"],))["source_type"], "human_self_report")

    def test_question_answer_requires_confirmation_then_reevaluates(self):
        self.root()
        self.e.add_case("MISSING", features(None))
        self.e.review_case("NEW", "REQUIRE_VALIDATION")
        self.e.add_rule("CHILD", "REQUIRE_VALIDATION", [{"feature": "via.geometry_equivalence", "op": "eq", "value": False}],
                        parent_id="ROOT", cornerstone_case_id="OLD", corrected_case_id="NEW")
        self.e.publish_rule("CHILD", True, True)
        decision = self.e.evaluate("MISSING")
        self.assertEqual(decision["decision"], "ASK")
        q_id = self.e.store.one("SELECT id FROM questions WHERE case_id='MISSING'")["id"]
        self.e.answer_question(q_id, "過去形状と同等であることを断面で確認")
        with self.assertRaises(PermissionError):
            self.e.confirm_question_feature(q_id, True)
        self.e.confirm_question_feature(q_id, True, evidence="expert_review_12", expert_confirmed=True)
        self.assertEqual(self.e.evaluate("MISSING", persist=False)["decision"], "REUSE_MODEL")
        self.assertEqual(self.e.store.one("SELECT count(*) AS n FROM case_feature_updates")["n"], 1)
        self.assertEqual(self.e.store.one("SELECT count(*) AS n FROM decisions WHERE case_id='MISSING' AND status='stale'")["n"], 1)
        self.assertIsNone(self.e.store.one("SELECT * FROM questions WHERE id=? AND status='open'", (q_id,)))

    def test_cornerstone_is_shown_at_feedback_time(self):
        fb = self.e.feedback("NEW", "前と違って同じモデルは危険", "REQUIRE_VALIDATION")
        candidates = self.e.feedback_context(fb)
        self.assertEqual(candidates[0]["case_id"], "OLD")
        self.assertIn("via.geometry_equivalence", [x["feature"] for x in candidates[0]["differences"]])

    def test_debrief_followup_can_be_added_and_interpreted(self):
        d = self.e.report_debrief("OLD", "図を見た")
        expanded = self.e.extend_debrief(d["id"], "再利用可能か確認し、保留した", FakeExtractor())
        self.assertTrue(expanded["interpretation"])
        self.assertEqual(self.e.confirm_debrief(d["id"])["status"], "confirmed_unvalidated")

    def test_narrative_without_model_stays_unparsed(self):
        draft = self.e.report_debrief("OLD", "過去の計算結果を調べて比較した")
        self.assertEqual(draft["status"], "needs_extraction")
        self.assertTrue(draft["followups"])
        with self.assertRaises(ValueError):
            self.e.confirm_debrief(draft["id"])

    def test_rule_cannot_invalidate_any_reviewed_case(self):
        self.e.review_case("NEW", "REQUIRE_VALIDATION")
        self.e.add_rule("ROOT", "REUSE_MODEL", [{"feature": "via.material", "op": "eq", "value": "OXIDE_A"}], cornerstone_case_id="OLD")
        with self.assertRaises(ValueError):
            self.e.publish_rule("ROOT", True, True)

    def test_no_unreviewed_case_as_gold(self):
        self.e.add_case("UNREVIEWED", features(True))
        self.assertIsNone([x for x in self.e.replay() if x["case_id"] == "UNREVIEWED"][0]["matches_review"])

    def test_temporal_no_future_rules(self):
        self.root()
        # Historical case happened before today's rule publication
        result = [x for x in self.e.replay("point_in_time") if x["case_id"] == "OLD"][0]
        self.assertEqual(result["decision"], "REVIEW")

if __name__ == "__main__":
    unittest.main()
