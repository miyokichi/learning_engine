import contextlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest

from learning_engine import Engine
from learning_engine.conversation import Conversation
from learning_engine.demo_conversation import sample_model, SAMPLE_TEXTS
from learning_engine.llm import FakeChatExtractor
from learning_engine.store import decode
from learning_engine.tutorial import tutorial


class V03Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "test.db")
        self.e = Engine(self.path)
        self.addCleanup(self.e.close)


class RDRIntegrityTests(V03Test):
    def test_unrelated_cornerstone_and_wrong_decision_type_rejected(self):
        for name, material, equivalent in [("OLD", "A", True), ("NEW", "A", False), ("OTHER", "B", True)]:
            self.e.add_case(name, {"material": {"value": material, "status": "documented"}, "equivalent": {"value": equivalent, "status": "expert_confirmed"}})
        self.e.review_case("OLD", "REUSE")
        self.e.add_rule("ROOT", "REUSE", [{"feature": "material", "op": "eq", "value": "A"}], cornerstone_case_id="OLD")
        self.e.publish_rule("ROOT", True, True)
        self.e.review_case("OTHER", "REUSE")
        decision = self.e.evaluate("NEW")
        self.assertEqual(decision["rule_path"], ["ROOT"])
        self.assertTrue(decision["evidence_refs"])
        fid = self.e.feedback("NEW", "different", "VALIDATE", decision["decision_id"])
        self.e.confirm_feedback(fid, "different geometry requires validation")
        self.e.review_case("NEW", "VALIDATE")
        with self.assertRaises(ValueError):
            self.e.draft_exception_from_feedback(fid, "OTHER", ["equivalent"], "ROOT")
        self.assertFalse(self.e.draft_exception_from_feedback(fid, "OLD", ["equivalent"], "ROOT")["regressions"])
        wrong = self.e.evaluate("NEW", "unrelated_type")
        bad = self.e.feedback("NEW", "wrong type", "VALIDATE", wrong["decision_id"])
        self.e.confirm_feedback(bad, "wrong type")
        with self.assertRaises(ValueError):
            self.e.draft_exception_from_feedback(bad, "OLD", ["equivalent"], "ROOT")


class ConversationTests(V03Test):
    def test_work_report_can_start_a_new_conversation_without_a_case_id(self):
        c = Conversation(self.e, sample_model())
        self.assertIn("案件を作ります", c.handle(SAMPLE_TEXTS[6]))
        self.assertIn("作業の順序", c.handle("確認"))
        c.handle("確認")
        self.assertIn("参考候補", c.handle("確認"))
        self.assertEqual(self.e.store.one("SELECT status FROM procedure_candidates")["status"], "approved_candidate")

    def test_numeric_boundary_is_not_invented_from_chat(self):
        llm = FakeChatExtractor({"案件": {"intent": "new_case", "payload": {"goal": "数値評価", "features": {"angle": 2}}},
            "学習": {"intent": "correction", "payload": {"conclusion": "OK", "selected_features": ["angle"], "interpretation": "この2点から境界を推定"}}})
        c = Conversation(self.e, llm)
        c.handle("案件"); c.handle("確認")
        c.handle("学習")
        self.assertIn("数値境界", c.handle("確認"))
        self.assertFalse(self.e.rules())

    def cycle(self):
        c = Conversation(self.e, sample_model())
        c.handle(SAMPLE_TEXTS[0]); c.handle("確認")
        c.handle(SAMPLE_TEXTS[1]); c.handle("確認"); c.handle("確認")
        c.handle(SAMPLE_TEXTS[2]); message = c.handle("確認")
        self.assertIn("状態：ASK", message)
        c.handle(SAMPLE_TEXTS[3]); message = c.handle("確認")
        self.assertIn("判断：REUSE_MODEL", message)
        c.handle(SAMPLE_TEXTS[4]); message = c.handle("確認")
        self.assertIn("草案", message)
        self.assertIn("REQUIRE_VALIDATION", c.handle("確認"))
        c.handle(SAMPLE_TEXTS[5]); message = c.handle("確認")
        self.assertIn("判断：REQUIRE_VALIDATION", message)
        return c

    def test_natural_dialogue_learning_cycle_and_procedure(self):
        c = self.cycle()
        c.handle(SAMPLE_TEXTS[6])
        self.assertIn("作業報告", c.handle("確認"))
        self.assertIn("参考候補", c.handle("確認"))
        row = self.e.store.one("SELECT * FROM procedure_candidates")
        self.assertEqual(row["status"], "approved_candidate")
        self.assertEqual(decode(row["body"])["required_order"], "unknown")

    def test_approval_is_bound_to_pending_action_and_not_model_or_ok(self):
        c = Conversation(self.e, sample_model())
        c.handle(SAMPLE_TEXTS[0])
        self.assertIn("まだ採用", c.handle("OK"))
        self.assertEqual(self.e.store.many("SELECT * FROM cases"), [])
        c.handle("確認")
        self.assertEqual(len(self.e.store.many("SELECT * FROM cases")), 1)
        self.assertIn("二重採用", c.handle("確認"))
        bad = Conversation(self.e, FakeChatExtractor({"publish": {"intent": "publish_rule", "payload": {"rule_id": "R"}}}))
        self.assertIn("保留", bad.handle("publish"))
        self.assertFalse(self.e.store.many("SELECT * FROM rules"))

    def test_restart_pending_action_and_llm_failure_preserve_raw(self):
        c = Conversation(self.e, sample_model())
        c.handle(SAMPLE_TEXTS[0])
        sid, aid = c.id, c.pending["id"]
        self.e.close()
        self.e = Engine(self.path)
        self.addCleanup(self.e.close)
        resumed = Conversation(self.e, sample_model(), sid)
        self.assertEqual(resumed.pending["id"], aid)
        self.assertIn("確認して保存", resumed.handle("確認"))
        class Broken:
            def interpret(self, *args):
                raise TimeoutError("offline")
        resumed.llm = Broken()
        self.assertIn("原文", resumed.handle("失敗しても残す文章"))
        self.assertTrue(self.e.store.one("SELECT id FROM conversation_turns WHERE raw_text='失敗しても残す文章'"))

    def test_no_model_says_manual_mode_and_saves_narrative(self):
        c = Conversation(self.e)
        self.assertIn("自動解釈は無効", c.handle("作業をしました"))
        self.assertTrue(self.e.store.one("SELECT id FROM conversation_turns WHERE role='human'"))

    def test_unknown_answer_not_reasked(self):
        model = sample_model()
        model.interpretations["未確認です"] = {"intent": "answer", "payload": {"values": {"geometry_equivalent": None}}}
        c = Conversation(self.e, model)
        c.handle(SAMPLE_TEXTS[0]); c.handle("確認")
        c.handle(SAMPLE_TEXTS[1]); c.handle("確認"); c.handle("確認")
        c.handle(SAMPLE_TEXTS[2]); c.handle("確認")
        c.handle("未確認です"); c.handle("確認")
        n = self.e.store.one("SELECT count(*) n FROM questions")["n"]
        c._evaluate(); c._evaluate()
        self.assertEqual(n, self.e.store.one("SELECT count(*) n FROM questions")["n"])

    def test_new_unknown_hypothesis_allows_explicit_safe_exploration(self):
        model = sample_model()
        model.interpretations["安全な探索案を検討したい"] = {"intent": "evaluate", "payload": {"explore": True}}
        c = Conversation(self.e, model)
        c.handle(SAMPLE_TEXTS[0]); c.handle("確認")
        c.handle(SAMPLE_TEXTS[1]); c.handle("確認"); c.handle("確認")
        c.handle(SAMPLE_TEXTS[2]); c.handle("確認")
        self.assertIn("状態：EXPLORE", c.handle("安全な探索案を検討したい"))


class TutorialTests(V03Test):
    def test_offline_tutorial_complete_idempotent_and_labeled(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            tutorial(self.e, auto=True)
            n = self.e.store.one("SELECT count(*) n FROM audit")["n"]
            tutorial(self.e, auto=True)
        self.assertIn("実際の専門家レビューではありません", out.getvalue())
        self.assertIn("REQUIRE_VALIDATION", out.getvalue())
        self.assertEqual(n, self.e.store.one("SELECT count(*) n FROM audit")["n"])
        self.assertEqual(self.e.store.one("SELECT stage FROM tutorial_runs")["stage"], 7)
