import copy
import unittest
from learning_engine.demo_experiment import sample_scenario
from learning_engine.replay_experiment import run_experiment, compare_experiment


class ReplayExperimentTests(unittest.TestCase):
    def test_repeatable_learning_and_isolated_test_labels(self):
        scenario = sample_scenario()
        a, b = run_experiment(scenario), run_experiment(scenario)
        self.assertEqual(a, b)
        self.assertEqual(a["metrics"]["test_correct"], 1)
        self.assertEqual(a["metrics"]["expert_answers"], 1)
        self.assertEqual(a["metrics"]["correct_questions"], 1)
        self.assertEqual(a["metrics"]["repeated_correction_errors"], 0)
        last = next(row["prediction"] for row in a["log"] if row["type"] == "system_decision" and row["prediction"]["case_id"] == "TEST-C")
        self.assertNotIn("TEST-C", last["visible_reviewed_cases"])
        self.assertEqual(last["rule_path"], ["ROOT", "EXCEPTION"])
        comparison = compare_experiment(scenario)
        self.assertEqual(comparison["learning_engine"]["metrics"]["test_decisions"], comparison["baseline_llm_plus_cases"]["metrics"]["test_decisions"])

    def test_future_label_cannot_change_earlier_decisions_or_retrieval(self):
        a = sample_scenario()
        b = copy.deepcopy(a)
        b["events"][-1]["payload"]["decision"] = "TOTALLY_DIFFERENT_FUTURE_LABEL"
        result_a, result_b = run_experiment(a), run_experiment(b)
        self.assertEqual(result_a["log"][:-1], result_b["log"][:-1])
        predictions = [r["prediction"] for r in result_a["log"] if r["type"] == "system_decision"]
        self.assertEqual(predictions[0]["visible_cases"], ["TRAIN-A"])

    def test_test_cases_cannot_train_or_answer_or_anchor_rules(self):
        scenario = sample_scenario()
        scenario["events"][5]["payload"]["split"] = "test"
        with self.assertRaises(ValueError):
            run_experiment(scenario)
        scenario = sample_scenario()
        scenario["events"][11]["payload"]["split"] = "test"
        with self.assertRaises(ValueError):
            run_experiment(scenario)

    def test_available_time_controls_visibility_not_event_time(self):
        scenario = sample_scenario()
        for event in scenario["events"]:
            event["event_time"] = "1999-01-01T00:00:00+00:00"
        result = run_experiment(scenario)
        self.assertEqual(result["log"][1]["prediction"]["visible_cases"], ["TRAIN-A"])
        scenario["events"][1]["available_at"] = "1900-01-01T00:00:00+00:00"
        with self.assertRaises(ValueError):
            run_experiment(scenario)

    def test_baseline_context_only_contains_revealed_information(self):
        class RecordingPolicy:
            def __init__(self):
                self.contexts = []
            def predict(self, context):
                self.contexts.append(context)
                return {"decision": "REVIEW", "gate": "REVIEW"}
        policy = RecordingPolicy()
        run_experiment(sample_scenario(), "baseline_llm_plus_cases", policy)
        self.assertEqual(policy.contexts[0]["reference_cases"], [])
        self.assertNotIn("TEST-C", str(policy.contexts[0]))
        self.assertIsNone(policy.contexts[-1]["case"]["expert_decision"])
