"""Deterministic, intentionally small, three-valued evaluator."""
from enum import Enum
import math

class Truth(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"

ACCEPTED_STATUSES = {"measured", "documented", "expert_confirmed", "derived_verified"}


def evaluate_predicate(features, predicate):
    feature = features.get(predicate["feature"])
    if not feature or feature.get("status") not in ACCEPTED_STATUSES:
        return Truth.UNKNOWN
    actual = feature.get("value")
    if actual is None:
        return Truth.UNKNOWN
    expected = predicate["value"]
    op = predicate["op"]
    if any(isinstance(v, float) and not math.isfinite(v) for v in (actual, expected)):
        return Truth.UNKNOWN
    if isinstance(actual, bool) != isinstance(expected, bool):
        return Truth.UNKNOWN
    if isinstance(actual, str) != isinstance(expected, str):
        return Truth.UNKNOWN
    if predicate.get("unit") is not None and feature.get("unit") != predicate["unit"]:
        return Truth.UNKNOWN
    if predicate.get("definition_version") is not None and feature.get("definition_version") != predicate["definition_version"]:
        return Truth.UNKNOWN
    if op in {"lt", "lte", "gt", "gte"}:
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return Truth.UNKNOWN
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            return Truth.UNKNOWN
    if op == "eq":
        result = actual == expected
    elif op == "ne":
        result = actual != expected
    elif op == "lt":
        result = actual < expected
    elif op == "lte":
        result = actual <= expected
    elif op == "gt":
        result = actual > expected
    elif op == "gte":
        result = actual >= expected
    else:
        raise ValueError(f"Unsupported operator: {op}")
    return Truth.TRUE if result else Truth.FALSE


def all_predicates(features, predicates):
    results = [evaluate_predicate(features, p) for p in predicates]
    if Truth.FALSE in results:
        return Truth.FALSE
    if Truth.UNKNOWN in results:
        return Truth.UNKNOWN
    return Truth.TRUE


def unknown_fields(features, predicates):
    return sorted({p["feature"] for p in predicates if evaluate_predicate(features, p) == Truth.UNKNOWN})


def decide(features, rules, decision_type):
    """MVP RDR tree: a matching child overrides its parent.

    UNKNOWN on any otherwise applicable root/exception blocks positive conclusions;
    does not mean FALSE. Multiple matching roots -> human review. Rule order is stable.
    """
    relevant = {r["id"]: r for r in rules if r["decision_type"] == decision_type and r["status"] == "published"}
    children = {}
    for rule in relevant.values():
        children.setdefault(rule["parent_id"], []).append(rule)
    for group in children.values():
        group.sort(key=lambda r: r["id"])
    roots = children.get(None, [])
    matches, unknown = [], set()
    for root in roots:
        res = all_predicates(features, root["predicates"])
        if res == Truth.TRUE:
            matches.append(root)
        elif res == Truth.UNKNOWN:
            unknown.update(unknown_fields(features, root["predicates"]))
    if unknown:
        return {"decision": "ASK", "rule_id": None, "unknown_fields": sorted(unknown), "reason": "unresolved_root"}
    if len(matches) != 1:
        return {"decision": "REVIEW", "rule_id": None, "unknown_fields": [], "reason": "no_root_or_ambiguous"}
    current = matches[0]
    while True:
        applicable = []
        pending = set()
        for child in children.get(current["id"], []):
            res = all_predicates(features, child["predicates"])
            if res == Truth.TRUE:
                applicable.append(child)
            elif res == Truth.UNKNOWN:
                pending.update(unknown_fields(features, child["predicates"]))
        if pending:
            return {"decision": "ASK", "rule_id": current["id"], "unknown_fields": sorted(pending), "reason": "possible_exception"}
        if len(applicable) > 1:
            return {"decision": "REVIEW", "rule_id": current["id"], "unknown_fields": [], "reason": "ambiguous_exceptions"}
        if not applicable:
            return {"decision": current["conclusion"], "rule_id": current["id"], "unknown_fields": [], "reason": "rule_matched"}
        current = applicable[0]


def feature_differences(first, second):
    diffs = []
    for key in sorted(set(first) | set(second)):
        a, b = first.get(key, {"status": "missing"}), second.get(key, {"status": "missing"})
        if a.get("value") != b.get("value") or a.get("status") != b.get("status"):
            diffs.append({"feature": key, "previous": a, "current": b})
    return diffs
