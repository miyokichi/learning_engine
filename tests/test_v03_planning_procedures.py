import unittest
from learning_engine import Engine
from learning_engine.demo import PROCEDURE, REPORT
from learning_engine.question_planner import QuestionPlanner, validate_candidate
from learning_engine.llm import FakeExtractor, validate_procedure
from learning_engine.store import decode


def candidate(field="new_property"):
    return {"field_id": field, "target_decision": "model_reuse", "missing_information": "新しい条件の意味と値",
            "why_needed": "過去の説明では区別できない要因があるという未検証の仮説", "possible_answers": ["確認できた", "未確認"],
            "how_answer_changes_next_step": "重要なら概念定義と条件を確認します", "source_case_or_rule": [], "priority": "critical"}


class QuestionPlanningTests(unittest.TestCase):
    def setUp(self):
        self.e = Engine(":memory:")
        self.addCleanup(self.e.close)
        self.e.add_case("A", {"known": {"value": True, "status": "expert_confirmed"}})

    def test_unknown_discovery_is_a_draft_concept_and_never_a_rule(self):
        planner = QuestionPlanner(self.e)
        questions = planner.ask_batch("A", "model_reuse", [candidate()])
        self.assertLessEqual(len(questions), 2)
        self.assertTrue(self.e.store.one("SELECT id FROM knowledge WHERE kind='concept' AND status='draft'"))
        self.assertFalse(self.e.store.many("SELECT * FROM feature_definitions"))
        self.assertFalse(self.e.rules())
        before = self.e.store.one("SELECT count(*) n FROM question_candidates")["n"]
        planner.ask_batch("A", "model_reuse", [candidate()])
        self.assertEqual(before, self.e.store.one("SELECT count(*) n FROM question_candidates")["n"])

    def test_already_confirmed_information_not_asked_again(self):
        planner = QuestionPlanner(self.e)
        questions = planner.ask_batch("A", "model_reuse", [candidate("known")])
        self.assertNotIn("known", [q["field_id"] for q in questions])
        with self.assertRaises(ValueError):
            validate_candidate({**candidate(), "priority": "urgent_and_publish"})

    def test_multiple_unverified_explanations_lead_to_discriminating_question(self):
        for text in ("原因は形状の可能性", "原因は材料の可能性"):
            self.e.propose_knowledge("causal_hypothesis", {"text": text, "source": "expert_hypothesis"}, "A")
        questions = QuestionPlanner(self.e).plan("A")
        self.assertIn("hypothesis_discrimination", [q["field_id"] for q in questions])
        self.assertTrue(all(row["status"] == "draft" for row in self.e.store.many("SELECT status FROM knowledge")))


class ProcedureSemanticsTests(unittest.TestCase):
    def test_report_conditions_synonyms_are_candidates_with_provenance(self):
        with Engine(":memory:") as e:
            e.add_case("A", {})
            ids = []
            for action in ("断面を確認", "断面画像を見た"):
                body = {**PROCEDURE, "steps": [{"action": action, "reason": "形状差を見る", "input": "断面", "observation": "差異", "result": "保留"}],
                        "decision_points": ["形状差の有無"], "branches": [{"condition": None, "action": "再検証", "status": "verified"}], "stop_conditions": []}
                d = e.report_debrief("A", REPORT, FakeExtractor(body))
                self.assertEqual(d["interpretation"]["branches"][0]["status"], "unverified")
                p = e.confirm_debrief(d["id"])
                e.approve_procedure(p["procedure_id"], True)
                ids.append(p["procedure_id"])
            semantic = {"action_synonyms": [{"actions": ["断面を確認", "断面画像を見た"], "reason": "同じ観察目的の可能性", "sources": ids}], "goal_matches": [], "condition_differences": []}
            result = e.compare_procedures(ids, semantic_candidates=semantic)
            self.assertEqual(result["semantic_candidates"]["action_synonyms"][0]["status"], "unverified")
            self.assertEqual(result["required_order"], "unknown")
            self.assertFalse(result["executable"])
            self.assertEqual(len(result["followups"]), 2)
            self.assertTrue(e.procedures("A")[0]["provenance"])
            stored = e.store.one("SELECT body FROM knowledge WHERE id=?", (result["comparison_id"],))
            self.assertEqual(decode(stored["body"])["source"], "procedure_comparison")

    def test_semantic_comparison_failure_retains_original_reports(self):
        with Engine(":memory:") as e:
            e.add_case("A", {})
            ids = []
            for _ in range(2):
                d = e.report_debrief("A", REPORT, FakeExtractor(PROCEDURE))
                ids.append(e.confirm_debrief(d["id"])["procedure_id"])
            class Broken:
                def compare_reports(self, reports):
                    raise TimeoutError("unavailable")
            result = e.compare_procedures(ids, extractor=Broken())
            self.assertTrue(result["error"])
            self.assertEqual(len(e.store.many("SELECT * FROM debriefs")), 2)
