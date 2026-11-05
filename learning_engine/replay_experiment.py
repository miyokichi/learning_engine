"""Isolated, event-by-event learning experiments; separate from historical replay."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from .engine import Engine, timestamp
from .store import encode

EVENTS = {"case_revealed", "system_decision", "expert_answer_revealed", "expert_review_revealed",
          "rule_proposed", "correction_confirmed", "rule_published"}


def validate_scenario(scenario):
    if not isinstance(scenario, dict) or set(scenario) != {"name", "events"} or not isinstance(scenario["name"], str) or not isinstance(scenario["events"], list):
        raise ValueError("実験はnameとeventsを持つオブジェクトにしてください")
    result, previous = [], None
    for event in scenario["events"]:
        if not isinstance(event, dict) or set(event) != {"type", "event_time", "available_at", "payload"} or event["type"] not in EVENTS or not isinstance(event["payload"], dict):
            raise ValueError("実験イベントの形式が不正です")
        event = copy.deepcopy(event)
        event["event_time"] = timestamp(event["event_time"])
        event["available_at"] = timestamp(event["available_at"])
        if previous and event["available_at"] < previous:
            raise ValueError("イベントはavailable_atの順に並べてください")
        previous = event["available_at"]
        result.append(event)
    return {"name": scenario["name"], "events": result}


class CaseReferenceBaseline:
    """Offline stand-in for an LLM given only the current case and revealed cases.

    A recorded/custom policy may implement predict(context) with this same input.
    This deliberately does not claim to measure the quality of a real LLM.
    """
    def predict(self, context):
        references = context["reference_cases"]
        if not references:
            return {"decision": "REVIEW", "gate": "REVIEW", "unknown_fields": [], "rule_path": []}
        return {"decision": references[0]["expert_decision"], "gate": "EXECUTE", "unknown_fields": [], "rule_path": []}


def run_experiment(scenario, strategy="learning_engine", baseline=None):
    """Replay explicit sample approvals only. Never accepts a production DB path."""
    if strategy not in {"learning_engine", "baseline_llm_plus_cases"}:
        raise ValueError("Unknown experiment strategy")
    scenario = validate_scenario(scenario)
    baseline = baseline or CaseReferenceBaseline()
    with tempfile.TemporaryDirectory(prefix="learning-experiment-") as directory:
        clock = ["2000-01-01T00:00:00.000000+00:00"]
        sequence = [0]
        def make_id(prefix):
            sequence[0] += 1
            return f"{prefix}-{sequence[0]:06d}"
        with Engine(Path(directory) / "experiment.sqlite3", clock=lambda: clock[0], id_factory=make_id) as e:
            splits, labels, needed, predictions, log = {}, {}, {}, [], []
            answers, correction_seen = 0, set()
            for index, event in enumerate(scenario["events"]):
                clock[0] = event["available_at"]
                p, kind = event["payload"], event["type"]
                entry = {"step": index, "type": kind, "event_time": event["event_time"], "available_at": clock[0]}
                if kind == "case_revealed":
                    if set(p) != {"case_id", "features", "split"} or p["split"] not in {"train", "test"}:
                        raise ValueError("Case公開に正解を含めず、train/testを明示してください")
                    e.add_case(p["case_id"], p["features"], occurred_at=event["event_time"], metadata={"source": "fictional_replay_fixture"})
                    splits[p["case_id"]] = p["split"]
                    entry.update(case_id=p["case_id"], split=p["split"])
                elif kind == "system_decision":
                    if set(p) - {"case_id", "decision_type"} or "case_id" not in p:
                        raise ValueError("推論イベントに未来の正解や回答を含められません")
                    cid, dtype = p["case_id"], p.get("decision_type", "model_reuse")
                    if cid not in splits:
                        raise ValueError("推論より先にCaseを公開してください")
                    if strategy == "learning_engine":
                        result = e.evaluate(cid, dtype)
                    else:
                        # The policy receives a fresh copy of only currently revealed information.
                        result = baseline.predict(copy.deepcopy({"case": e.case(cid), "decision_type": dtype,
                            "reference_cases": e.retrieve_cases(cid)}))
                        if not isinstance(result, dict) or not isinstance(result.get("decision"), str) or result.get("gate") not in {"EXECUTE", "ASK", "REVIEW", "EXPLORE", "BLOCK"}:
                            raise ValueError("Invalid baseline response")
                    record = {"case_id": cid, "split": splits[cid], "decision": result["decision"], "gate": result["gate"],
                              "after_correction_for_case": cid in correction_seen,
                              "questions": list(result.get("unknown_fields", [])), "rule_path": result.get("rule_path", []),
                              "visible_cases": [r["id"] for r in e.store.many("SELECT id FROM cases ORDER BY id")],
                              "visible_reviewed_cases": [r["id"] for r in e.store.many("SELECT id FROM cases WHERE review_status='expert_approved' ORDER BY id")],
                              "visible_rules": [r["id"] for r in e.rules() if r["status"] == "published"]}
                    predictions.append(record)
                    entry["prediction"] = copy.deepcopy(record)
                elif kind == "expert_review_revealed":
                    if set(p) - {"case_id", "decision", "status", "needed_fields"} or not {"case_id", "decision"} <= p.keys():
                        raise ValueError("Invalid expert review fixture")
                    cid = p["case_id"]
                    if cid not in splits or not any(r["case_id"] == cid for r in predictions):
                        raise ValueError("正解はCase公開・推論の後で開示してください")
                    status = p.get("status", "expert_approved")
                    if status == "expert_approved":
                        labels[cid] = p["decision"]
                        needed[cid] = p.get("needed_fields", [])
                    else:
                        labels.pop(cid, None)
                        needed.pop(cid, None)
                    if splits[cid] == "train":
                        e.review_case(cid, p["decision"], status)
                    entry.update(case_id=cid, used_for_training=splits[cid] == "train", review_status=status)
                elif kind == "expert_answer_revealed":
                    if set(p) != {"case_id", "field_id", "raw_text", "value", "expert_confirmed"} or p["expert_confirmed"] is not True:
                        raise ValueError("回答fixtureに明示確認が必要です")
                    cid = p["case_id"]
                    if splits.get(cid) != "train":
                        raise ValueError("testの専門家回答を学習に利用できません")
                    if not any(r["case_id"] == cid for r in predictions):
                        raise ValueError("専門家回答は最初の推論後に開示してください")
                    qid = e.ask(cid, "model_reuse", p["field_id"])
                    e.answer_question(qid, p["raw_text"])
                    e.confirm_question_feature(qid, p["value"], status="unknown" if p["value"] is None else "expert_confirmed", evidence=f"fixture_event:{index}", expert_confirmed=True)
                    answers += 1
                    entry.update(case_id=cid, field=p["field_id"], answer=p["raw_text"])
                elif kind == "rule_proposed":
                    for key in ("cornerstone_case_id", "corrected_case_id"):
                        if p.get(key) and splits.get(p[key]) != "train":
                            raise ValueError("testケースをルールの根拠にできません")
                    preview = e.add_rule(**p)
                    entry.update(rule_id=preview["rule_id"], regressions=preview["regressions"])
                elif kind == "correction_confirmed":
                    keys = {"case_id", "conclusion", "interpretation", "cornerstone_case_id", "parent_rule_id", "selected_features", "rule_id"}
                    if set(p) != keys or splits.get(p["case_id"]) != "train" or splits.get(p["cornerstone_case_id"]) != "train":
                        raise ValueError("訂正はtrain内の比較事例で行ってください")
                    latest = e.store.one("SELECT id FROM decisions WHERE case_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (p["case_id"],))
                    # Baseline still sees the same revealed cases/answers/approvals; its prediction ignores rules.
                    fid = e.feedback(p["case_id"], p["interpretation"], p["conclusion"], latest["id"] if latest else None)
                    e.confirm_feedback(fid, p["interpretation"])
                    preview = e.draft_exception_from_feedback(fid, p["cornerstone_case_id"], p["selected_features"], p["parent_rule_id"], p["rule_id"])
                    correction_seen.add(p["case_id"])
                    entry.update(rule_id=preview["rule_id"], regressions=preview["regressions"])
                elif kind == "rule_published":
                    if set(p) != {"rule_id", "expert_confirmed", "impacts_reviewed"} or p["expert_confirmed"] is not True or p["impacts_reviewed"] is not True:
                        raise ValueError("ルール公開fixtureに明示承認が必要です")
                    e.publish_rule(p["rule_id"], True, True)
                    entry["rule_id"] = p["rule_id"]
                log.append(entry)
            scored = [p for p in predictions if p["case_id"] in labels]
            held = [p for p in scored if p["gate"] in {"ASK", "REVIEW", "EXPLORE"}]
            completed = [p for p in scored if p["gate"] in {"EXECUTE", "BLOCK"}]
            test = [p for p in scored if p["split"] == "test"]
            metrics = {"evaluated_decisions": len(scored), "excluded_unreviewed_decisions": len(predictions) - len(scored),
                "wrong_confirmations": sum(p["decision"] != labels[p["case_id"]] for p in completed),
                "held_decisions": len(held), "correct_questions": sum(len(set(p["questions"]) & set(needed.get(p["case_id"], []))) for p in scored),
                "unnecessary_questions": sum(len(set(p["questions"]) - set(needed.get(p["case_id"], []))) for p in scored),
                "expert_answers": answers, "test_decisions": len(test),
                "test_correct": sum(p["decision"] == labels[p["case_id"]] and p["gate"] in {"EXECUTE", "BLOCK"} for p in test),
                "completion_rate": len(completed) / len(scored) if scored else None,
                "repeated_correction_errors": sum(p["after_correction_for_case"] and p["decision"] != labels[p["case_id"]] and p["gate"] in {"EXECUTE", "BLOCK"} for p in scored)}
            return {"scenario": scenario["name"], "fixture_sha256": hashlib.sha256(encode(scenario).encode()).hexdigest(), "strategy": strategy,
                    "notice": "架空の回答・承認のオフライン実験です。実LLMや実業務の精度評価ではありません。",
                    "metric_basis": "推論イベントを分母とし、後でexpert_approvedの正解が開示されたものだけ採点。test正解は学習DBへ投入しない。needed_fieldsは採点用fixture。",
                    "metrics": metrics, "log": log}


def compare_experiment(scenario):
    return {strategy: run_experiment(scenario, strategy) for strategy in ("baseline_llm_plus_cases", "learning_engine")}
