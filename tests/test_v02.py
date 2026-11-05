"""v0.2 phase acceptance tests, fictional data and offline LLM transport."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from learning_engine import Engine
from learning_engine.demo import run_demo, PROCEDURE, REPORT
from learning_engine.llm import FakeExtractor, LocalChatExtractor, validate_procedure, validate_suggestion
from learning_engine.logic import Truth, evaluate_predicate, all_predicates
from learning_engine.store import SCHEMA, encode, decode
from learning_engine.interactive import interact


def feature(value, status="expert_confirmed"):
    return {"value": value, "status": status, "source": "fictional_expert"}


def pred(key, value):
    return {"feature": key, "op": "eq", "value": value}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "engine.db")
        self.e = Engine(self.path)
        self.addCleanup(self.e.close)

    def seed(self):
        return run_demo(self.e)

    def reopen(self):
        self.e.close()
        self.e = Engine(self.path)
        self.addCleanup(self.e.close)


class Phase1Tests(EngineTest):
    def test_demo_e2e_and_restart_idempotence(self):
        result = self.seed()
        self.assertEqual(result["question"]["decision"], "ASK")
        self.assertEqual(result["before_correction"]["decision"], "REUSE_MODEL")
        self.assertEqual(result["after_learning"]["decision"], "REQUIRE_VALIDATION")
        self.assertEqual(result["next_case"]["decision"], "REQUIRE_VALIDATION")
        self.assertEqual(result["regression"]["regressions"], [])
        self.assertFalse(result["procedure_references"][0]["executable"])
        n = self.e.store.one("SELECT count(*) n FROM audit")["n"]
        self.reopen()
        self.assertEqual(run_demo(self.e)["status"], "already_completed")
        self.assertEqual(n, self.e.store.one("SELECT count(*) n FROM audit")["n"])
        self.assertTrue(self.e.pending())

    def test_answered_question_not_reasked_and_confirmation_survives_restart(self):
        result = self.seed()
        q = result["unknown"]["questions"][0]
        self.e.answer_question(q["id"], "断面で同等と確認")
        self.reopen()
        self.assertEqual(self.e.evaluate("DEMO-UNKNOWN")["decision"], "ASK")
        self.assertEqual(self.e.store.one("SELECT count(*) n FROM questions WHERE case_id='DEMO-UNKNOWN'")["n"], 1)
        self.e.confirm_question_feature(q["id"], True, expert_confirmed=True)
        self.assertEqual(self.e.evaluate("DEMO-UNKNOWN")["decision"], "REUSE_MODEL")

    def test_unknown_allows_safe_local_exploration(self):
        self.seed()
        result = self.e.evaluate("DEMO-UNKNOWN", intent="explore")
        self.assertEqual(result["gate"], "EXPLORE")
        self.assertEqual(result["decision"], "ASK")
        self.assertFalse(result["external_execution_allowed"])
        self.reopen()
        self.assertEqual(self.e.store.one("SELECT state FROM workflow WHERE object_id='DEMO-UNKNOWN'")["state"], "EXPLORE")

    def test_questions_limited_to_two_per_evaluation(self):
        self.e.add_case("A", {})
        self.e.add_rule("R", "OK", [pred(k, True) for k in ["x", "y", "z"]])
        self.e.publish_rule("R", True, True)
        r = self.e.evaluate("A")
        self.assertEqual(len(r["unknown_fields"]), 3)
        self.assertEqual(len(r["questions"]), 2)

    def test_interactive_manual_forms_e2e(self):
        answers = iter([
            "1", "CASE", "Via", "評価", "架空", "material", "OXIDE_A", "y", "",
            "8", "REUSE_MODEL", "material", "OXIDE_A", "y", "y", "2",
            "5", REPORT, "Via評価", "形状差", "断面を確認", "形状の比較", "差あり", "",
            "保留", "同等性", "", "y", "y", "0"
        ])
        with patch("builtins.input", side_effect=lambda _: next(answers)), contextlib.redirect_stdout(io.StringIO()) as out:
            interact(self.e)
        self.assertIn("自動解釈は無効", out.getvalue())
        self.assertIn("EXECUTE", out.getvalue())
        self.reopen()
        self.assertEqual(self.e.evaluate("CASE", persist=False)["decision"], "REUSE_MODEL")
        self.assertEqual(self.e.store.one("SELECT status FROM procedure_candidates")["status"], "approved_candidate")

    def test_interactive_question_resume_and_correction(self):
        result = self.seed()
        q = result["unknown"]["questions"][0]
        answers = iter(["7", q["id"], "断面を確認した", "true", "y", "0"])
        with patch("builtins.input", side_effect=lambda _: next(answers)), contextlib.redirect_stdout(io.StringIO()):
            interact(self.e)
        self.assertEqual(self.e.evaluate("DEMO-UNKNOWN", persist=False)["decision"], "REUSE_MODEL")

    def test_interactive_correction_contrast_approval_and_application(self):
        self.e.add_case("A", {"material": feature("oxide"), "equivalent": feature(True)})
        self.e.review_case("A", "REUSE_MODEL")
        self.e.add_rule("ROOT", "REUSE_MODEL", [pred("material", "oxide")], cornerstone_case_id="A")
        self.e.publish_rule("ROOT", True, True)
        self.e.add_case("B", {"material": feature("oxide"), "equivalent": feature(False)})
        answers = iter(["1", "B", "2", "4", "REQUIRE_VALIDATION", "形状が異なる", "A", "equivalent",
                        "同等性が否定されたら再検証", "y", "ROOT", "y", "0"])
        with patch("builtins.input", side_effect=lambda _: next(answers)), contextlib.redirect_stdout(io.StringIO()) as out:
            interact(self.e)
        self.assertIn("cornerstone_candidates", out.getvalue())
        self.assertEqual(self.e.evaluate("A", persist=False)["decision"], "REUSE_MODEL")
        self.reopen()
        self.e.add_case("C", {"material": feature("oxide"), "equivalent": feature(False)})
        self.assertEqual(self.e.evaluate("C", persist=False)["decision"], "REQUIRE_VALIDATION")

    def test_cli_independent_process_install_entrypoint_demo_replay(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        def cli(*args, input=None):
            run = subprocess.run([sys.executable, "-m", "learning_engine", "--db", self.path, *args],
                                 input=input, capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            return run.stdout
        self.assertEqual(json.loads(cli("init"))["status"], "initialized")
        self.assertEqual(json.loads(cli("demo"))["next_case"]["decision"], "REQUIRE_VALIDATION")
        self.assertTrue(json.loads(cli("pending")))
        self.assertTrue(json.loads(cli("replay", "--mode", "point_in_time")))
        self.assertIn("自動解釈は無効", cli("interact", input="6\n0\n"))


class Phase2Tests(EngineTest):
    def test_definition_alias_unit_and_angle_semantics(self):
        self.e.register_feature("via.angle_from_vertical", "垂直方向からの角度", aliases=["vertical_angle"], unit="deg", measurement="断面", expert_confirmed=True)
        self.e.add_case("A", {"vertical_angle": feature(10)})
        value = self.e.case("A")["features"]["via.angle_from_vertical"]
        self.assertEqual(value["unit"], "deg")
        self.assertEqual(value["definition_version"], 1)
        self.assertIn("recorded_at", value)
        with self.assertRaises(ValueError):
            self.e.add_case("B", {"vertical_angle": {**feature(10), "unit": "rad"}})
        with self.assertRaises(ValueError):
            self.e.register_feature("horizontal_angle", "水平基準", aliases=["vertical_angle"], expert_confirmed=True)
        self.e.add_rule("R", "OK", [pred("via.angle_from_vertical", 10)])
        self.e.publish_rule("R", True, True)
        self.assertEqual(self.e.evaluate("A", persist=False)["decision"], "OK")
        self.e.add_case("C", {"via.angle_from_horizontal": feature(10)})
        self.assertEqual(self.e.evaluate("C", persist=False)["decision"], "ASK")

    def test_three_valued_false_unknown_type_nonfinite(self):
        self.assertEqual(evaluate_predicate({"x": feature(False)}, pred("x", False)), Truth.TRUE)
        self.assertEqual(evaluate_predicate({"x": feature(False)}, pred("x", True)), Truth.FALSE)
        self.assertEqual(evaluate_predicate({"x": feature(False, "assumed")}, pred("x", False)), Truth.UNKNOWN)
        self.assertEqual(evaluate_predicate({"x": feature(0)}, pred("x", False)), Truth.UNKNOWN)
        self.assertEqual(evaluate_predicate({"x": feature("0")}, {"feature": "x", "op": "ne", "value": 0}), Truth.UNKNOWN)
        self.assertEqual(all_predicates({"x": feature(False)}, [pred("x", True), pred("y", True)]), Truth.FALSE)
        with self.assertRaises(ValueError):
            self.e.add_case("BAD", {"x": feature(float("nan"))})

    def test_claim_hypothesis_evidence_are_distinct_and_unverified(self):
        self.e.add_case("A", {})
        claim = self.e.propose_knowledge("causal_hypothesis", {"text": "形状が電界に影響", "source": "expert_hypothesis", "scope": {}}, "A")
        evidence = self.e.propose_knowledge("evidence", {"text": "計算結果", "source": "fictional_result", "claim_id": claim["id"], "polarity": "supports"}, "A")
        with self.assertRaises(ValueError):
            self.e.adopt_knowledge(claim["id"], True)
        self.assertEqual(self.e.adopt_knowledge(evidence["id"], True)["status"], "verified")
        self.assertEqual(self.e.store.one("SELECT status FROM knowledge WHERE id=?", (claim["id"],))["status"], "draft")

    def test_v01_database_migrates_once_without_history_loss(self):
        path = str(Path(self.tmp.name) / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?)", ("OLD", "2025-01-01T00:00:00+00:00", "{}", "expert_approved", "OK", 1, "2025-01-01T00:00:00+00:00"))
        conn.execute("INSERT INTO audit(event_type,object_id,payload,timestamp) VALUES(?,?,?,?)", ("case_reviewed", "OLD", encode({"status": "expert_approved", "decision": "OK"}), "2025-02-01T00:00:00+00:00"))
        conn.commit()
        conn.close()
        for _ in range(2):
            with Engine(path) as e:
                self.assertEqual(e.case("OLD")["review_status"], "expert_approved")
                self.assertEqual(e.store.one("SELECT count(*) n FROM review_history")["n"], 1)
                self.assertEqual(e.case("OLD", "2025-01-02T00:00:00+00:00")["review_status"], "unreviewed")
                self.assertEqual(e.store.one("PRAGMA user_version")["user_version"], 2)

    def test_conflicting_expert_review_retained_and_gated(self):
        self.seed()
        self.e.review_case("DEMO-OLD", "REQUIRE_VALIDATION", "disputed")
        self.assertEqual(self.e.evaluate("DEMO-OLD", persist=False)["gate"], "REVIEW")
        self.assertEqual(self.e.evaluate("DEMO-OLD", persist=False, intent="explore")["gate"], "REVIEW")
        self.assertEqual(len(self.e.store.many("SELECT * FROM review_history WHERE case_id='DEMO-OLD'")), 2)
        self.assertIsNone(next(r for r in self.e.replay() if r["case_id"] == "DEMO-OLD")["matches_review"])


class Phase3Tests(EngineTest):
    def test_regression_uses_confirmed_feature_updates_not_initial_snapshot(self):
        self.e.add_case("A", {"x": feature(None, "unknown")})
        qid = self.e.ask("A", "model_reuse", "x")
        self.e.answer_question(qid, "x is true")
        self.e.confirm_question_feature(qid, True, expert_confirmed=True)
        self.e.review_case("A", "OK")
        preview = self.e.add_rule("R", "OK", [pred("x", True)], cornerstone_case_id="A")
        self.assertEqual(preview["reviewed_mismatches"], [])
        self.e.publish_rule("R", True, True)
        self.assertEqual(self.e.evaluate("A", persist=False)["decision"], "OK")

    def test_unaffected_existing_review_mismatch_blocks_publication(self):
        self.e.add_case("A", {"x": feature(True)})
        self.e.review_case("A", "EXPECTED")
        preview = self.e.add_rule("R", "WRONG", [pred("x", False)])
        self.assertTrue(preview["reviewed_mismatches"])
        with self.assertRaises(ValueError):
            self.e.publish_rule("R", True, True)

    def test_numeric_differences_cannot_invent_threshold(self):
        self.e.add_case("A", {"angle": feature(1)})
        self.e.add_case("B", {"angle": feature(2)})
        self.e.review_case("A", "REUSE")
        self.e.add_rule("R", "REUSE", [{"feature": "angle", "op": "gte", "value": 0}], cornerstone_case_id="A")
        self.e.publish_rule("R", True, True)
        fid = self.e.feedback("B", "angle differs", "VALIDATE")
        self.e.confirm_feedback(fid, "angle difference requires validation")
        self.e.review_case("B", "VALIDATE")
        with self.assertRaises(ValueError):
            self.e.draft_exception_from_feedback(fid, "A", ["angle"], "R")

    def test_unconfirmed_correction_cannot_be_drafted(self):
        self.seed()
        f = self.e.feedback("DEMO-NEXT", "訂正", "BLOCK")
        with self.assertRaises(ValueError):
            self.e.draft_exception_from_feedback(f, "DEMO-OLD", ["via.geometry_equivalence"], "DEMO-ROOT")
        with self.assertRaises(PermissionError):
            self.e.publish_rule("DEMO-ROOT")


class Phase4Tests(EngineTest):
    def test_dialogue_suggestions_are_saved_but_never_activate_rules(self):
        self.seed()
        suggestions = {"interpretation": "形状差が重要かもしれない", "questions": ["底部の半径を確認できますか？適用条件の判断が変わります。"],
                       "hypotheses": ["底部形状が影響する可能性"], "concepts": ["底部半径"],
                       "rule_candidates": [{"conclusion": "REQUIRE_VALIDATION", "predicates": [pred("via.geometry_equivalence", False)], "reason": "形状差"}]}
        n = len(self.e.rules())
        result = self.e.suggest("DEMO-NEW", "底部の丸まりも確認したい", FakeExtractor(PROCEDURE, suggestions), "concept")
        self.assertFalse(result["knowledge_activated"])
        self.assertEqual(result["response"], suggestions)
        self.assertEqual(len(self.e.rules()), n)
        with self.assertRaises(ValueError):
            self.e.adopt_knowledge(result["id"], True)
        self.reopen()
        row = self.e.store.one("SELECT body FROM knowledge WHERE id=?", (result["id"],))
        self.assertEqual(decode(row["body"])["response"], suggestions)
        with self.assertRaises(ValueError):
            validate_suggestion({**suggestions, "questions": ["a", "b", "c"]})

    def test_dialogue_model_failure_and_no_model_preserve_raw_text(self):
        self.e.add_case("A", {})
        class Broken:
            def suggest(self, *args):
                raise TimeoutError("offline")
        result = self.e.suggest("A", "原文を保存", Broken())
        self.assertTrue(result["error"])
        self.assertEqual(decode(self.e.store.one("SELECT body FROM knowledge WHERE id=?", (result["id"],))["body"])["text"], "原文を保存")
        self.assertIn("自動解釈は無効", self.e.suggest("A", "相談")["response"]["interpretation"])

    def test_manual_narrative_and_multiple_report_comparison(self):
        self.e.add_case("A", {})
        ids = []
        for _ in range(2):
            d = self.e.report_debrief("A", REPORT)
            self.assertEqual(d["status"], "needs_extraction")
            p = self.e.confirm_debrief(d["id"], PROCEDURE)
            ids.append(p["procedure_id"])
            self.e.approve_procedure(p["procedure_id"], True)
        compared = self.e.compare_procedures(ids)
        self.assertEqual(len(compared["common_steps"]), 2)
        self.assertEqual(compared["required_order"], "unknown")
        self.assertEqual(compared["status"], "draft")
        self.assertFalse(compared["executable"])
        self.reopen()
        self.assertEqual(len(self.e.procedures("A")), 2)

    def test_extraction_errors_preserve_original_and_followup_history(self):
        self.e.add_case("A", {})
        class Broken:
            def extract(self, text):
                raise TimeoutError("unavailable")
        d = self.e.report_debrief("A", REPORT, Broken())
        self.assertEqual(d["status"], "needs_extraction")
        self.assertTrue(d["error"])
        self.e.extend_debrief(d["id"], "追加の原文", Broken())
        self.assertIn(REPORT, self.e.store.one("SELECT report_text FROM debriefs")["report_text"])
        self.assertTrue(self.e.store.one("SELECT payload FROM audit WHERE event_type='narrative_extended'"))
        p = self.e.confirm_debrief(d["id"], PROCEDURE)
        self.assertEqual(p["lifecycle_status"], "interpretation_confirmed")

    def test_llm_malformed_fields_not_adopted(self):
        self.e.add_case("A", {})
        class Malformed:
            def extract(self, text):
                return {**PROCEDURE, "steps": ["invented"]}
        d = self.e.report_debrief("A", REPORT, Malformed())
        self.assertIsNone(d["interpretation"])
        for change in [{"goal": 12}, {"missing": ["oops"]}, {"steps": []}, {"limitations": True}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_procedure({**PROCEDURE, **change})

    def test_scoped_validation_never_authorizes_execution(self):
        self.e.add_case("A", {"material": feature("oxide")})
        self.e.add_case("B", {"material": feature("metal")})
        self.e.add_case("C", {"material": feature("oxide", "assumed")})
        d = self.e.report_debrief("A", REPORT, FakeExtractor({**PROCEDURE, "required_order": ["must_do"]}))
        self.assertEqual(d["interpretation"]["required_order"], "unknown")
        p = self.e.confirm_debrief(d["id"])
        pid = p["procedure_id"]
        self.e.approve_procedure(pid, True)
        self.e.validate_procedure_scope(pid, {"material": "oxide"}, "fictional validation report", True)
        self.assertFalse(self.e.procedures("A")[0]["executable"])
        self.assertEqual(self.e.procedures("B"), [])
        self.assertEqual(self.e.procedures("C"), [])

    def test_openai_compatible_transport_and_invalid_json(self):
        extractor = LocalChatExtractor("http://localhost:1234/v1", "local-test", timeout=1)
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps(PROCEDURE)}}]}).encode()
        with patch("urllib.request.build_opener", return_value=opener):
            result = extractor.extract(REPORT)
            self.assertEqual(result["goal"], PROCEDURE["goal"])
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://localhost:1234/v1/chat/completions")
            self.assertEqual(json.loads(request.data)["messages"][1]["content"], REPORT)
            response.read.return_value = b'{"choices":[{"message":{"content":"not json"}}]}'
            with self.assertRaises(ValueError):
                extractor.extract(REPORT)
        with self.assertRaises(ValueError):
            LocalChatExtractor("https://external.example/v1", "model")


class Phase5Tests(EngineTest):
    def test_concept_impacts_and_adoption_do_not_fill_old_values(self):
        self.seed()
        body = {"text": "底部の丸まりも確認", "source": "fictional_expert",
                "feature_id": "via.bottom_radius", "definition": "底部の曲率半径", "measurement": "断面画像",
                "unit": "nm", "scope": {"via.material": "OXIDE_A"}, "important": True,
                "relation_type": "distinct", "related_features": ["via.geometry_equivalence"]}
        kid = self.e.propose_knowledge("concept", body, "DEMO-NEW")["id"]
        impact = self.e.concept_impacts(kid)
        self.assertIn("DEMO-OLD", impact["cases"])
        self.assertIn("DEMO-ROOT", impact["rules"])
        self.assertFalse(self.e.store.one("SELECT * FROM feature_definitions"))
        with self.assertRaises(PermissionError):
            self.e.adopt_knowledge(kid)
        self.e.adopt_knowledge(kid, True)
        self.reopen()
        self.assertNotIn("via.bottom_radius", self.e.case("DEMO-OLD")["features"])
        self.assertTrue(any(p["state"] == "needs_review" for p in self.e.pending()))
        self.assertEqual(self.e.store.one("SELECT count(*) n FROM knowledge_revisions WHERE object_id=?", (kid,))["n"], 2)

    def test_concept_rejection_and_incomplete_definition(self):
        self.e.add_case("A", {})
        p = self.e.propose_knowledge("concept", {"text": "未定義", "source": "expert"}, "A")
        with self.assertRaises(ValueError):
            self.e.adopt_knowledge(p["id"], True)
        self.assertEqual(self.e.adopt_knowledge(p["id"], True, reject=True)["status"], "rejected")

    def test_important_concept_gates_old_and_new_cases_until_value_and_review(self):
        self.seed()
        body = {"text": "底部の丸まり", "source": "fictional_expert", "feature_id": "via.radius",
                "definition": "底部半径", "measurement": "断面", "unit": "nm", "scope": {}, "important": True}
        k = self.e.propose_knowledge("concept", body, "DEMO-NEW")["id"]
        self.e.adopt_knowledge(k, True)
        result = self.e.evaluate("DEMO-OLD")
        self.assertEqual(result["decision"], "ASK")
        self.assertIn("via.radius", result["unknown_fields"])
        q = next(q for q in result["questions"] if q["field_id"] == "via.radius")
        self.e.answer_question(q["id"], "架空値5nm")
        self.e.confirm_question_feature(q["id"], 5, expert_confirmed=True)
        self.assertEqual(self.e.evaluate("DEMO-OLD", persist=False)["decision"], "REVIEW")
        self.e.review_case("DEMO-OLD", "REUSE_MODEL")
        self.assertEqual(self.e.evaluate("DEMO-OLD", persist=False)["decision"], "REUSE_MODEL")
        self.e.add_case("NEWER", {"via.material": feature("OXIDE_A"), "via.geometry_equivalence": feature(True)})
        self.assertEqual(self.e.evaluate("NEWER", persist=False)["decision"], "ASK")
        stamp = self.e.store.one("SELECT published_at FROM rules WHERE id='DEMO-EXCEPTION'")["published_at"]
        self.assertNotEqual(self.e.evaluate("DEMO-OLD", persist=False, as_of=stamp)["reason"], "important_new_concept")


class Phase6Tests(EngineTest):
    def test_future_features_reviews_and_rules_not_visible(self):
        self.e.add_case("A", {"x": feature(None, "unknown")}, occurred_at="2020-01-01T00:00:00+00:00")
        self.e.add_rule("R", "OK", [pred("x", True)])
        self.e.publish_rule("R", True, True)
        q = self.e.ask("A", "model_reuse", "x")
        self.e.answer_question(q, "now measured true")
        self.e.confirm_question_feature(q, True, expert_confirmed=True)
        self.e.review_case("A", "OK")
        historical = self.e.replay("point_in_time")[0]
        self.assertFalse(historical["knowledge_available"])
        self.assertEqual(historical["decision"], "REVIEW")
        self.assertEqual(historical["rule_version"], [])
        self.assertIsNone(historical["expected"])
        self.assertIsNone(historical["matches_review"])
        self.assertEqual(self.e.case("A", "2020-01-01T00:00:00+00:00")["features"], {})
        current = self.e.replay("current_knowledge")[0]
        self.assertEqual(current["decision"], "OK")
        self.assertTrue(current["matches_review"])

    def test_rule_withdrawal_preserves_past_published_state(self):
        self.seed()
        stamp = self.e.store.one("SELECT published_at FROM rules WHERE id='DEMO-EXCEPTION'")["published_at"]
        self.e.withdraw_rule("DEMO-EXCEPTION", True)
        self.assertEqual(self.e.evaluate("DEMO-NEW", persist=False)["decision"], "REUSE_MODEL")
        self.assertEqual(self.e.evaluate("DEMO-NEW", persist=False, as_of=stamp)["decision"], "REQUIRE_VALIDATION")
        self.assertTrue(self.e.store.one("SELECT * FROM audit WHERE event_type='rule_withdrawn'"))

    def test_frozen_evaluation_repeatability_and_metrics(self):
        self.seed()
        self.assertEqual(self.e.replay("current_knowledge"), self.e.replay("current_knowledge"))
        self.reopen()
        a = self.e.evaluate("DEMO-NEXT", persist=False)
        b = self.e.evaluate("DEMO-NEXT", persist=False)
        self.assertEqual(a, b)
        metrics = self.e.metrics()
        self.assertEqual(metrics["wrong_confirmations"], 0)
        self.assertEqual(metrics["regression_violations"], 0)
        self.assertGreater(metrics["held_cases"], 0)

    def test_explicit_block_not_overridden_by_exploration(self):
        self.e.add_case("A", {"x": feature(True), "y": feature(None, "unknown")})
        self.e.add_rule("R", "BLOCK", [pred("x", True)])
        self.e.publish_rule("R", True, True)
        self.assertEqual(self.e.evaluate("A", persist=False, intent="explore")["gate"], "BLOCK")

    def test_timezone_is_explicit_and_normalized(self):
        self.e.add_case("A", {}, occurred_at="2026-01-01T09:00:00+09:00")
        self.assertEqual(self.e.case("A")["occurred_at"], "2026-01-01T00:00:00.000000+00:00")
        with self.assertRaises(ValueError):
            self.e.add_case("B", {}, occurred_at="2026-01-01T09:00:00")

    def test_nested_transaction_rolls_back_all_changes(self):
        with self.assertRaises(ValueError):
            with self.e.store.transaction():
                self.e.add_case("ROLLBACK", {})
                raise ValueError("abort")
        self.assertIsNone(self.e.store.one("SELECT id FROM cases WHERE id='ROLLBACK'"))


if __name__ == "__main__":
    unittest.main()
