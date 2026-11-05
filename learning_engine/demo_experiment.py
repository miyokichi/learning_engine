"""Fictional event fixture. Only demo/test values, never domain logic."""
def sample_scenario():
    events = []
    def event(kind, **payload):
        events.append({"type": kind, "event_time": "2025-01-01T00:00:00+00:00",
                       "available_at": f"2026-01-01T00:00:{len(events):02d}+00:00", "payload": payload})
    def features(equivalent, process):
        return {"material": {"value": "A", "status": "documented", "source": "fixture:material"},
                "equivalent": {"value": equivalent, "status": "unknown" if equivalent is None else "expert_confirmed", "source": "fixture:comparison"},
                "process": {"value": process, "status": "documented", "source": "fixture:process"},
                "document": {"value": process, "status": "documented", "source": "fixture:document"}}
    event("case_revealed", case_id="TRAIN-A", features=features(True, "P1"), split="train")
    event("system_decision", case_id="TRAIN-A")
    event("expert_review_revealed", case_id="TRAIN-A", decision="REUSE_MODEL", needed_fields=[])
    event("rule_proposed", rule_id="ROOT", conclusion="REUSE_MODEL", predicates=[{"feature": "material", "op": "eq", "value": "A"}], cornerstone_case_id="TRAIN-A")
    event("rule_published", rule_id="ROOT", expert_confirmed=True, impacts_reviewed=True)
    event("case_revealed", case_id="TRAIN-B", features=features(False, "P2"), split="train")
    event("system_decision", case_id="TRAIN-B")
    event("expert_review_revealed", case_id="TRAIN-B", decision="REQUIRE_VALIDATION", needed_fields=[])
    event("correction_confirmed", case_id="TRAIN-B", conclusion="REQUIRE_VALIDATION", interpretation="同等性が否定されたら再検証", cornerstone_case_id="TRAIN-A", parent_rule_id="ROOT", selected_features=["equivalent"], rule_id="EXCEPTION")
    event("rule_published", rule_id="EXCEPTION", expert_confirmed=True, impacts_reviewed=True)
    event("system_decision", case_id="TRAIN-B")
    event("case_revealed", case_id="TRAIN-D", features=features(None, "P3"), split="train")
    event("system_decision", case_id="TRAIN-D")
    event("expert_answer_revealed", case_id="TRAIN-D", field_id="equivalent", raw_text="教材の回答：同等ではないと確認", value=False, expert_confirmed=True)
    event("system_decision", case_id="TRAIN-D")
    event("expert_review_revealed", case_id="TRAIN-D", decision="REQUIRE_VALIDATION", needed_fields=["equivalent"])
    event("case_revealed", case_id="TEST-C", features=features(False, "P1"), split="test")
    event("system_decision", case_id="TEST-C")
    event("expert_review_revealed", case_id="TEST-C", decision="REQUIRE_VALIDATION", needed_fields=[])
    return {"name": "fictional-sequential-learning-v1", "events": events}
