"""Case/RDR/QA/conversational-procedure learning MVP.

The store captures claims and reviewed decisions; LLM extraction is never used as
an authoritative feature or an automatically executable procedure.
"""
import uuid
from datetime import datetime, timezone
from .store import Store, encode, decode
from .logic import decide, feature_differences
from .knowledge import KnowledgeMixin, validate_feature
from .llm import validate_procedure


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp requires an explicit timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Engine(KnowledgeMixin):
    def __init__(self, db="learning.sqlite3", *, clock=None, id_factory=None):
        self.clock = clock or now
        self.make_id = id_factory or new_id
        self.store = Store(db)

    def close(self):
        self.store.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def case(self, case_id, as_of=None):
        if as_of is not None:
            as_of = timestamp(as_of)
        c = self.store.one("SELECT * FROM cases WHERE id=?", (case_id,))
        if not c:
            raise KeyError(f"Unknown case {case_id}")
        features = decode(c["features"])
        if as_of is not None and c["created_at"] > as_of:
            features = {}
        if as_of is None:
            updates = self.store.many("SELECT * FROM case_feature_updates WHERE case_id=? ORDER BY recorded_at, id", (case_id,))
        else:
            updates = self.store.many("SELECT * FROM case_feature_updates WHERE case_id=? AND recorded_at<=? ORDER BY recorded_at,id", (case_id, as_of))
        for u in updates:
            features[u["feature_id"]] = decode(u["feature_json"])
        c["features"] = features
        if as_of is not None:
            review = self.store.one("SELECT * FROM review_history WHERE case_id=? AND available_at<=? ORDER BY available_at DESC,id DESC LIMIT 1", (case_id, as_of))
            c["review_status"] = review["status"] if review else "unreviewed"
            c["expert_decision"] = review["decision"] if review else None
        metadata = self.store.one("SELECT body FROM case_metadata WHERE case_id=?", (case_id,))
        c["metadata"] = decode(metadata["body"]) if metadata and (as_of is None or c["created_at"] <= as_of) else {}
        return c

    def add_case(self, case_id, features, occurred_at=None, feature_schema_version=1, metadata=None):
        if not isinstance(features, dict):
            raise TypeError("features must be a dict")
        features = self.normalize_features(features)
        for name, entry in features.items():
            if not isinstance(entry, dict) or "status" not in entry:
                raise ValueError(f"Feature {name} needs an explicit status")
            if entry["status"] in ("measured", "documented", "expert_confirmed", "derived_verified") and entry.get("value") is None:
                raise ValueError(f"Feature {name} is verified but value is absent")
        stamp = self.clock()
        occurred_at = timestamp(occurred_at) if occurred_at else stamp
        for entry in features.values():
            entry.setdefault("source", "user_input")
            entry.setdefault("observed_at", occurred_at)
            entry["recorded_at"] = stamp
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO cases(id, occurred_at, features, feature_schema_version, created_at) VALUES(?,?,?,?,?)",
                                    (case_id, occurred_at or stamp, encode(features), feature_schema_version, stamp))
            self.store.audit("case_created", case_id, {"features": features}, stamp)
            for field, entry in features.items():
                self._evidence_ref(case_id, field, entry, stamp)
            self.store.conn.execute("INSERT INTO case_metadata VALUES(?,?)", (case_id, encode(metadata or {})))
            self._state(case_id, case_id, "registered")
        return self.case(case_id)

    def review_case(self, case_id, decision, status="expert_approved"):
        if status not in {"expert_approved", "expert_rejected", "disputed", "unreviewed"}:
            raise ValueError("unsupported review status")
        self.case(case_id)
        with self.store.transaction():
            self.store.conn.execute("UPDATE cases SET review_status=?,expert_decision=? WHERE id=?",
                                    (status, decision, case_id))
            self.store.audit("case_reviewed", case_id, {"status": status, "decision": decision}, self.clock())
            self.store.conn.execute("INSERT INTO review_history(case_id,decision,status,available_at) VALUES(?,?,?,?)", (case_id, decision, status, self.clock()))
            if status == "expert_approved":
                from .logic import ACCEPTED_STATUSES
                features = self.case(case_id)["features"]
                for row in self.store.many("SELECT * FROM workflow WHERE case_id=? AND state='needs_review'", (case_id,)):
                    field = decode(row["body"]).get("feature")
                    if field and features.get(field, {}).get("status") in ACCEPTED_STATUSES:
                        self._state(row["object_id"], case_id, "completed")

    def rules(self, until=None):
        if until is not None:
            until = timestamp(until)
        rows = self.store.many("SELECT * FROM rules ORDER BY created_at, id")
        result = []
        for r in rows:
            if until is not None and (r["published_at"] is None or r["published_at"] > until):
                continue
            if until is not None:
                event = self.store.one("SELECT status FROM rule_events WHERE rule_id=? AND available_at<=? ORDER BY available_at DESC,id DESC LIMIT 1", (r["id"], until))
                r["status"] = event["status"] if event else "draft"
            r["predicates"] = decode(r["predicates"])
            result.append(r)
        return result

    def evaluate(self, case_id, decision_type="model_reuse", persist=True, as_of=None, intent="commit"):
        if as_of is not None:
            as_of = timestamp(as_of)
        if intent not in {"commit", "explore"}:
            raise ValueError("intent must be commit or explore")
        if as_of is not None and persist:
            raise ValueError("Historical evaluation is read-only")
        current = self.case(case_id, as_of=as_of)
        rules = self.rules(until=as_of)
        result = decide(current["features"], rules, decision_type)
        # "REUSE_MODEL" is deliberately a recommendation; actual high-impact
        # changes are NOT authorized by this judgment evaluator.
        result = {**result, "case_id": case_id, "decision_type": decision_type}
        concept_unknowns, concept_review = self._concept_requirements(current, as_of)
        if concept_unknowns and result["decision"] != "BLOCK":
            result.update(decision="ASK", unknown_fields=sorted(set(result["unknown_fields"]) | concept_unknowns), reason="important_new_concept")
        elif concept_review and result["decision"] not in {"BLOCK", "ASK"}:
            result.update(decision="REVIEW", reason="new_concept_requires_review")
        if current["review_status"] == "disputed":
            result.update(decision="REVIEW", reason="disputed_case")
        result["gate"] = result["decision"] if result["decision"] in {"ASK", "REVIEW", "BLOCK", "EXPLORE"} else "EXECUTE"
        if intent == "explore" and result["gate"] in {"ASK", "REVIEW"} and current["review_status"] != "disputed":
            result.update(gate="EXPLORE", reason="safe_local_exploration_only")
        result["rule_version"] = [r["id"] for r in rules if r["status"] == "published" and r["decision_type"] == decision_type]
        result["evidence"] = {k: v.get("source") for k, v in current["features"].items()}
        result["evidence_refs"] = self.store.many("SELECT * FROM evidence_refs WHERE case_id=? AND available_at<=? ORDER BY available_at,id", (case_id, as_of or self.clock()))
        result["reference_cases"] = self.retrieve_cases(case_id, as_of=as_of, decision_type=decision_type)
        result["external_execution_allowed"] = False
        if persist:
            stamp = self.clock()
            d_id = self.make_id("DEC")
            with self.store.transaction():
                self.store.conn.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?)", (
                    d_id, case_id, decision_type, result["decision"], result["rule_id"],
                    "provisional", encode(current["features"]), stamp))
                self.store.audit("decision_proposed", d_id, result, stamp)
                self.store.conn.execute("INSERT INTO decision_details VALUES(?,?)", (d_id, encode(result)))
                self._state(case_id, case_id, result["gate"], result)
            result["decision_id"] = d_id
            if result["decision"] == "ASK":
                for feature in result["unknown_fields"][:2]:
                    self.ask(case_id, decision_type, feature)
            result["questions"] = self.store.many("SELECT * FROM questions WHERE case_id=? AND status IN ('open','answered_unverified') ORDER BY created_at,id LIMIT 2", (case_id,))
        return result

    def _evidence_ref(self, case_id, field, entry, stamp):
        self.store.conn.execute("INSERT INTO evidence_refs VALUES(?,?,?,?,?,?,?)", (
            self.make_id("EV"), case_id, field, str(entry.get("source") or "unspecified"), entry["status"],
            encode({"feature": entry, "verification": "reported_status_only", "authenticity_verified": False}), stamp))

    def _concept_requirements(self, current, as_of):
        """New important concepts are explicit review obligations, not inferred values."""
        from .logic import all_predicates, Truth, ACCEPTED_STATUSES
        missing, needs_review = set(), False
        for row in self.store.many("SELECT * FROM audit WHERE event_type='knowledge_verified' ORDER BY timestamp,id"):
            if as_of is not None and row["timestamp"] > as_of:
                continue
            knowledge = self.store.one("SELECT kind FROM knowledge WHERE id=?", (row["object_id"],))
            if not knowledge or knowledge["kind"] != "concept":
                continue
            body = decode(row["payload"])["body"]
            if not body.get("important"):
                continue
            scope = [{"feature": k, "op": "eq", "value": v} for k, v in body["scope"].items()]
            if all_predicates(current["features"], scope) == Truth.FALSE:
                continue
            field = body["feature_id"]
            value = current["features"].get(field, {})
            if value.get("status") not in ACCEPTED_STATUSES or value.get("value") is None:
                missing.add(field)
            else:
                review = self.store.one("SELECT * FROM review_history WHERE case_id=? AND available_at>=? AND available_at<=? ORDER BY available_at DESC,id DESC LIMIT 1",
                                        (current["id"], row["timestamp"], as_of or self.clock()))
                if not review or review["status"] != "expert_approved":
                    needs_review = True
        return missing, needs_review

    def ask(self, case_id, target_decision, field_id):
        case = self.case(case_id)
        existing = self.store.one("SELECT * FROM questions WHERE case_id=? AND field_id=? AND target_decision=? AND status IN ('open','answered_unverified')",
                                  (case_id, field_id, target_decision))
        if existing:
            return existing["id"]
        previous = self.store.one("SELECT * FROM questions WHERE case_id=? AND field_id=? AND target_decision=? ORDER BY created_at DESC LIMIT 1", (case_id, field_id, target_decision))
        if previous and previous["status"] == "confirmed" and case["features"].get(field_id, {}).get("status") in {"unknown", "conflicting"}:
            return previous["id"]
        q_id, stamp = self.make_id("Q"), self.clock()
        question = f"{field_id} は今回どのように確認しましたか？判断根拠と適用範囲も教えてください。回答の確定で {target_decision} の適用可否を再評価します。"
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?)",
                                    (q_id, case_id, target_decision, field_id, question, "open", None, stamp))
            self.store.audit("question_created", q_id, {"case_id": case_id, "feature": field_id}, stamp)
            self._state(q_id, case_id, "question_open")
        return q_id

    def answer_question(self, question_id, free_text):
        q = self.store.one("SELECT * FROM questions WHERE id=?", (question_id,))
        if not q:
            raise KeyError(question_id)
        if q["status"] != "open":
            raise ValueError("Question already answered")
        with self.store.transaction():
            self.store.conn.execute("UPDATE questions SET answer=?, status='answered_unverified' WHERE id=?",
                                    (free_text, question_id))
            self.store.audit("question_answered", question_id, {"text": free_text, "feature": q["field_id"]}, self.clock())
            self._state(question_id, q["case_id"], "answer_confirmation")
        # Do NOT silently update case features from free text.
        return {"status": "answered_unverified", "next": "confirm_and_version_case_feature"}

    def confirm_question_feature(self, question_id, value, status="expert_confirmed", evidence=None, expert_confirmed=False):
        """An explicit human action turns a free-text answer into a typed case feature.

        Never treats an unreviewed LLM extraction as ground truth. Previous
        values are retained in case_feature_updates; base case is immutable.
        """
        if not expert_confirmed:
            raise PermissionError("An expert must confirm the structured interpretation")
        if status not in {"expert_confirmed", "documented", "measured", "derived_verified", "assumed", "conflicting", "unknown"}:
            raise ValueError("Unsupported feature status")
        q = self.store.one("SELECT * FROM questions WHERE id=?", (question_id,))
        if not q:
            raise KeyError(question_id)
        if q["status"] != "answered_unverified":
            raise ValueError("Question must have an answered, unverified narrative")
        if not evidence:
            evidence = f"question:{question_id}"
        f = {"value": value, "status": status, "source": evidence}
        if status in {"unknown", "conflicting"}:
            f["value"] = None
        validate_feature(f)
        f = self.normalize_features({q["field_id"]: f})[q["field_id"]]
        uid, stamp = self.make_id("FEATURE"), self.clock()
        f.update(recorded_at=stamp, observed_at=stamp)
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO case_feature_updates VALUES(?,?,?,?,?,?)",
                                    (uid, q["case_id"], q["field_id"], encode(f), question_id, stamp))
            self._evidence_ref(q["case_id"], q["field_id"], f, stamp)
            self.store.conn.execute("UPDATE questions SET status='confirmed' WHERE id=?", (question_id,))
            self.store.conn.execute("UPDATE decisions SET status='stale' WHERE case_id=?", (q["case_id"],))
            self.store.audit("case_feature_confirmed", uid, {"question": question_id, "feature": q["field_id"], "entry": f}, stamp)
            self._state(question_id, q["case_id"], "completed")
        return {"status": "confirmed", "feature_update_id": uid, "next": "reevaluate", "case_id": q["case_id"]}

    def feedback_context(self, feedback_id, limit=3):
        """Show historical explicitly reviewed cases immediately at correction time."""
        f = self.store.one("SELECT * FROM feedback WHERE id=?", (feedback_id,))
        if not f:
            raise KeyError(feedback_id)
        return self.retrieve_cases(f["case_id"], limit=limit)

    def retrieve_cases(self, case_id, limit=3, as_of=None, decision_type=None):
        """Rank approved comparisons separately from deterministic applicability."""
        current = self.case(case_id, as_of=as_of)
        rows = self.store.many("SELECT id FROM cases WHERE occurred_at<=? AND id<>? ORDER BY occurred_at DESC,id",
                               (current["occurred_at"], case_id))
        candidates = []
        for row in rows:
            older = self.case(row["id"], as_of=as_of)
            if decision_type and older["metadata"].get("decision_type") not in {None, decision_type}:
                continue
            if older["review_status"] != "expert_approved":
                continue
            shared = set(current["features"]) & set(older["features"])
            agreement = sum(current["features"][k].get("value") == older["features"][k].get("value") for k in shared)
            candidates.append({"case_id": row["id"], "expert_decision": older["expert_decision"],
                               "common_feature_count": len(shared), "matching_feature_count": agreement,
                               "differences": feature_differences(older["features"], current["features"]),
                               "selection_note": "similarity candidate, not a verified causal comparison"})
        return sorted(candidates, key=lambda r: (-r["matching_feature_count"], -r["common_feature_count"], r["case_id"]))[:limit]

    def feedback(self, case_id, text, corrected_to, decision_id=None):
        self.case(case_id)
        if decision_id and not self.store.one("SELECT id FROM decisions WHERE id=? AND case_id=?", (decision_id, case_id)):
            raise ValueError("decision_id does not belong to case")
        fid, stamp = self.make_id("FB"), self.clock()
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?)",
                                    (fid, case_id, decision_id, text, corrected_to, None, 0, stamp))
            self.store.audit("correction_reported", fid, {"case": case_id, "corrected_to": corrected_to}, stamp)
            self._state(fid, case_id, "correction_interpretation")
        return fid

    def confirm_feedback(self, feedback_id, interpretation):
        if not interpretation.strip():
            raise ValueError("Need an explicit expert-confirmed interpretation")
        if not self.store.one("SELECT id FROM feedback WHERE id=?", (feedback_id,)):
            raise KeyError(feedback_id)
        with self.store.transaction():
            old = self.store.one("SELECT * FROM feedback WHERE id=?", (feedback_id,))
            if old["confirmed"]:
                raise ValueError("Confirmed correction is immutable; create another correction")
            self.store.conn.execute("UPDATE feedback SET interpretation=?, confirmed=1 WHERE id=?",
                                    (interpretation, feedback_id))
            self.store.audit("correction_interpretation_confirmed", feedback_id, {"interpretation": interpretation}, self.clock())
            self._state(feedback_id, old["case_id"], "rule_draft_pending")

    def draft_exception_from_feedback(self, feedback_id, cornerstone_case_id, selected_features,
                                      parent_rule_id, rule_id=None):
        """RDR acquisition: expert selects which displayed differences matter.

        For categorical/boolean features the current value becomes a draft
        equality predicate. A numeric difference is NOT converted into an
        invented threshold: it requires a separately justified condition.
        """
        f = self.store.one("SELECT * FROM feedback WHERE id=?", (feedback_id,))
        if not f or not f["confirmed"]:
            raise ValueError("The expert must confirm the correction interpretation")
        current = self.case(f["case_id"])
        prior = self.case(cornerstone_case_id)
        if current["review_status"] != "expert_approved" or current["expert_decision"] != f["corrected_to"]:
            raise ValueError("Corrected case needs explicit approved target decision")
        if prior["review_status"] != "expert_approved":
            raise ValueError("Cornerstone case must be explicitly reviewed")
        if not selected_features:
            raise ValueError("Expert must select at least one contrasting feature")
        predicates = []
        for fid in selected_features:
            before = prior["features"].get(fid)
            after = current["features"].get(fid)
            if not before or not after or before.get("value") == after.get("value"):
                raise ValueError(f"{fid} does not separate the two cases")
            if after.get("status") not in {"measured", "documented", "expert_confirmed", "derived_verified"}:
                raise ValueError(f"{fid} is not sufficiently supported for a rule")
            value = after["value"]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                raise ValueError(f"{fid} is numeric: expert must supply a justified boundary separately")
            if not isinstance(value, (str, bool)):
                raise ValueError(f"{fid} needs a typed rule predicate")
            predicates.append({"feature": fid, "op": "eq", "value": value})
        parent = self.store.one("SELECT * FROM rules WHERE id=?", (parent_rule_id,))
        if not parent or parent["status"] != "published":
            raise ValueError("Parent rule must be published")
        self.validate_contrast(parent_rule_id, cornerstone_case_id, f["case_id"], feedback_id)
        result = self.add_rule(rule_id or self.make_id("R"), f["corrected_to"], predicates,
                               decision_type=parent["decision_type"], parent_id=parent_rule_id,
                               cornerstone_case_id=cornerstone_case_id,
                               corrected_case_id=f["case_id"], explanation=f["interpretation"], feedback_id=feedback_id)
        return {**result, "draft_predicates": predicates, "human_confirmation_required": True}

    def validate_contrast(self, parent_id, cornerstone_case_id, corrected_case_id, feedback_id=None):
        parent = self.store.one("SELECT * FROM rules WHERE id=?", (parent_id,))
        if not parent or parent["status"] != "published":
            raise ValueError("Parent rule must be published")
        if cornerstone_case_id == corrected_case_id:
            raise ValueError("Contrasting cases must be different")
        for case_id in (cornerstone_case_id, corrected_case_id):
            case = self.case(case_id)
            if case["metadata"].get("decision_type") not in {None, parent["decision_type"]}:
                raise ValueError("Case judgment subject does not match parent decision type")
            result = decide(case["features"], self.rules(), parent["decision_type"])
            if case_id == corrected_case_id and feedback_id:
                feedback = self.store.one("SELECT * FROM feedback WHERE id=?", (feedback_id,))
                if feedback and feedback["decision_id"]:
                    decision = self.store.one("SELECT * FROM decisions WHERE id=?", (feedback["decision_id"],))
                    if not decision or decision["case_id"] != case_id or decision["decision_type"] != parent["decision_type"]:
                        raise ValueError("Correction decision type/case does not match the parent")
                    details = self.store.one("SELECT body FROM decision_details WHERE decision_id=?", (decision["id"],))
                    result = decode(details["body"]) if details else decide(decode(decision["snapshot"]), self.rules(until=decision["created_at"]), decision["decision_type"])
            if parent_id not in result.get("rule_path", []):
                raise ValueError(f"Case {case_id} does not belong to the parent rule judgment path")
        return True

    def add_rule(self, rule_id, conclusion, predicates, decision_type="model_reuse",
                 parent_id=None, cornerstone_case_id=None, corrected_case_id=None,
                 explanation="", feedback_id=None):
        if not predicates or not isinstance(predicates, list):
            raise ValueError("Need at least one explicit predicate: no implicit broad rules")
        for p in predicates:
            if not {"feature", "op", "value"} <= set(p) or set(p) - {"feature", "op", "value", "unit", "definition_version"}:
                raise ValueError("Predicate keys must be feature/op/value")
            if p["op"] not in {"eq", "ne", "gt", "gte", "lt", "lte"}:
                raise ValueError("unsupported predicate op")
            validate_feature({"value": p["value"], "status": "expert_confirmed"})
            if p["op"] in {"gt", "gte", "lt", "lte"} and (isinstance(p["value"], bool) or not isinstance(p["value"], (int, float))):
                raise ValueError("Numeric operator requires a numeric boundary")
            definition = self.store.one("SELECT body FROM feature_definitions WHERE id=?", (p["feature"],))
            if definition:
                body = decode(definition["body"])
                if p.get("unit", body["unit"]) != body["unit"] or p.get("definition_version", body["version"]) != body["version"]:
                    raise ValueError("Predicate definition mismatch")
                p = dict(p)
                p.update(unit=body["unit"], definition_version=body["version"])
                predicates = [p if v["feature"] == p["feature"] else v for v in predicates]
        if parent_id:
            parent = self.store.one("SELECT * FROM rules WHERE id=?", (parent_id,))
            if not parent or parent["decision_type"] != decision_type:
                raise ValueError("No parent with same decision type")
        if not (cornerstone_case_id and corrected_case_id) and parent_id:
            raise ValueError("Exception requires both contrasting cases")
        if parent_id:
            self.validate_contrast(parent_id, cornerstone_case_id, corrected_case_id, feedback_id)
        if cornerstone_case_id:
            self.case(cornerstone_case_id)
        if corrected_case_id:
            self.case(corrected_case_id)
        if feedback_id:
            f = self.store.one("SELECT * FROM feedback WHERE id=?", (feedback_id,))
            if not f or not f["confirmed"] or f["case_id"] != corrected_case_id:
                raise ValueError("Expert must confirm correction interpretation before rule proposal")
        stamp = self.clock()
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                rule_id, parent_id, decision_type, encode(predicates), conclusion,
                cornerstone_case_id, corrected_case_id, explanation, feedback_id, "draft", stamp, None))
            self.store.audit("rule_proposed", rule_id, {"predicates": predicates, "feedback_id": feedback_id}, stamp)
            case_id = corrected_case_id or cornerstone_case_id
            if case_id:
                self._state(rule_id, case_id, "approval_pending")
        preview = self.rule_preview(rule_id)
        if case_id and preview["reviewed_mismatches"]:
            with self.store.transaction():
                self._state(rule_id, case_id, "regression_conflict", preview)
        return preview

    def rule_preview(self, rule_id):
        original_rules = self.rules()
        target = next((r for r in original_rules if r["id"] == rule_id), None)
        if target is None:
            raise KeyError(rule_id)
        if target["status"] != "draft":
            raise ValueError("rule is not a draft")
        candidate_rules = [*original_rules]
        modified = {**target, "status": "published"}
        candidate_rules = [modified if r["id"] == rule_id else r for r in candidate_rules]
        rows = []
        for c in self.store.many("SELECT * FROM cases ORDER BY occurred_at, id"):
            case = self.case(c["id"])
            if case["metadata"].get("decision_type") not in {None, target["decision_type"]}:
                continue
            features = case["features"]
            old = decide(features, original_rules, target["decision_type"])
            new = decide(features, candidate_rules, target["decision_type"])
            if old["decision"] != new["decision"] or old["rule_id"] != new["rule_id"] or c["review_status"] == "expert_approved":
                rows.append({"case_id": c["id"], "before": old["decision"], "after": new["decision"],
                             "review_status": c["review_status"], "expected": c["expert_decision"],
                             "regression": (c["review_status"] == "expert_approved"
                                            and old["decision"] == c["expert_decision"]
                                            and new["decision"] != c["expert_decision"]),
                             "violates_reviewed_case": (c["review_status"] == "expert_approved"
                                                         and new["decision"] != c["expert_decision"])})
        return {"rule_id": rule_id, "affected_cases": rows,
                "regressions": [r for r in rows if r["regression"]],
                "reviewed_mismatches": [r for r in rows if r["violates_reviewed_case"]],
                "cornerstone": target["cornerstone_case_id"], "corrected": target["corrected_case_id"]}

    def publish_rule(self, rule_id, expert_confirmed=False, impacts_reviewed=False):
        if not expert_confirmed or not impacts_reviewed:
            raise PermissionError("Explicit human confirmation AND impact review required")
        r = self.store.one("SELECT * FROM rules WHERE id=?", (rule_id,))
        if not r:
            raise KeyError(rule_id)
        if r["parent_id"]:
            parent = self.store.one("SELECT * FROM rules WHERE id=?", (r["parent_id"],))
            if parent["status"] != "published":
                raise ValueError("Parent must be published")
            self.validate_contrast(r["parent_id"], r["cornerstone_case_id"], r["corrected_case_id"], r["feedback_id"])
        preview = self.rule_preview(rule_id)
        if preview["reviewed_mismatches"]:
            raise ValueError(f"Proposed rule contradicts expert-reviewed cases: {preview['reviewed_mismatches']}")
        for id_ in [r["cornerstone_case_id"], r["corrected_case_id"]]:
            if id_ and self.case(id_)["review_status"] != "expert_approved":
                raise ValueError(f"Case {id_} lacks explicit expert approval")
        if r["corrected_case_id"]:
            after = next((c["after"] for c in preview["affected_cases"] if c["case_id"] == r["corrected_case_id"]), None)
            if after != self.case(r["corrected_case_id"])["expert_decision"]:
                raise ValueError("Proposed rule does not match corrected case expert decision")
        stamp = self.clock()
        with self.store.transaction():
            self.store.conn.execute("UPDATE rules SET status='published',published_at=? WHERE id=?", (stamp, rule_id))
            self.store.audit("rule_published", rule_id, preview, stamp)
            self.store.conn.execute("INSERT INTO rule_events(rule_id,status,available_at) VALUES(?,?,?)", (rule_id, "published", stamp))
            self.store.conn.execute("UPDATE decisions SET status='stale' WHERE decision_type=?", (r["decision_type"],))
            case_id = r["corrected_case_id"] or r["cornerstone_case_id"]
            if case_id:
                self._state(rule_id, case_id, "published")
            if r["feedback_id"]:
                self._state(r["feedback_id"], case_id, "completed")
        return preview

    def withdraw_rule(self, rule_id, expert_confirmed=False):
        if not expert_confirmed:
            raise PermissionError("Withdrawal needs expert approval")
        rule = self.store.one("SELECT * FROM rules WHERE id=?", (rule_id,))
        if not rule or rule["status"] not in {"draft", "published"}:
            raise ValueError("No active rule")
        if self.store.one("SELECT id FROM rules WHERE parent_id=? AND status IN ('published','draft')", (rule_id,)):
            raise ValueError("Withdraw descendants first")
        stamp = self.clock()
        with self.store.transaction():
            self.store.conn.execute("UPDATE rules SET status='withdrawn' WHERE id=?", (rule_id,))
            self.store.conn.execute("INSERT INTO rule_events(rule_id,status,available_at) VALUES(?,?,?)", (rule_id, "withdrawn", stamp))
            self.store.audit("rule_withdrawn", rule_id, {"before": rule["status"]}, stamp)
            self.store.conn.execute("UPDATE workflow SET state='completed' WHERE object_id=?", (rule_id,))
            self.store.conn.execute("UPDATE decisions SET status='stale' WHERE decision_type=?", (rule["decision_type"],))

    def cornerstone_comparison(self, current_case_id, prior_case_id):
        return feature_differences(self.case(prior_case_id)["features"], self.case(current_case_id)["features"])

    def replay(self, mode="current", decision_type="model_reuse"):
        if mode == "current_knowledge":
            mode = "current"
        if mode not in {"current", "point_in_time"}:
            raise ValueError("mode must be current or point_in_time")
        output = []
        for row in self.store.many("SELECT id,occurred_at,expert_decision,review_status FROM cases ORDER BY occurred_at,id"):
            if mode == "point_in_time":
                historical = self.case(row["id"], row["occurred_at"])
                row.update(review_status=historical["review_status"], expert_decision=historical["expert_decision"])
            evaluated = self.evaluate(row["id"], decision_type, persist=False,
                                      as_of=row["occurred_at"] if mode == "point_in_time" else None)
            evaluated.update({"as_of": row["occurred_at"] if mode == "point_in_time" else "latest",
                              "review_status": row["review_status"], "expected": row["expert_decision"]})
            evaluated["knowledge_available"] = mode != "point_in_time" or self.case(row["id"])["created_at"] <= row["occurred_at"]
            if row["review_status"] == "expert_approved":
                evaluated["matches_review"] = evaluated["decision"] == row["expert_decision"]
            else:
                evaluated["matches_review"] = None
            output.append(evaluated)
        return output

    def metrics(self, mode="current_knowledge"):
        rows = self.replay(mode)
        gold = [r for r in rows if r["matches_review"] is not None]
        mismatches = [r for r in gold if not r["matches_review"]]
        return {"cases": len(rows), "reviewed": len(gold),
                "wrong_confirmations": sum(r["gate"] == "EXECUTE" for r in mismatches),
                "missing_questions": sum(r["gate"] == "EXECUTE" and bool(r["unknown_fields"]) for r in rows),
                "unnecessary_questions": sum(r["decision"] == "ASK" and not r["unknown_fields"] for r in rows),
                "review_mismatches": len(mismatches),
                "held_cases": sum(r["gate"] in {"ASK", "REVIEW"} for r in rows),
                "regression_violations": sum(len(self.rule_preview(r["id"])["regressions"]) for r in self.rules() if r["status"] == "draft"),
                "repeated_errors": sum(not r["matches_review"] and bool(self.store.one("SELECT id FROM feedback WHERE case_id=? AND confirmed=1", (r["case_id"],))) for r in gold),
                "note": "unnecessary_questions is a structural proxy; domain ground truth is not inferred"}

    def suggest(self, case_id, text, extractor=None, task="case"):
        """Persist human dialogue and model suggestions without activating knowledge."""
        from .llm import validate_suggestion
        if task not in {"case", "answer", "correction", "concept"} or not isinstance(text, str) or not text.strip():
            raise ValueError("Need a supported task and nonempty dialogue")
        context = {"case": self.case(case_id), "evaluation": self.evaluate(case_id, persist=False),
                   "rules": self.rules(), "feature_definitions": self.store.many("SELECT * FROM feature_definitions")}
        error = None
        if extractor:
            try:
                response = validate_suggestion(extractor.suggest(task, text, context))
            except Exception as exc:
                response, error = None, f"{type(exc).__name__}: suggestion failed; dialogue retained"
        else:
            fields = context["evaluation"]["unknown_fields"][:2]
            response = {"interpretation": "自動解釈は無効。手動で解釈を確認してください。", "hypotheses": [],
                        "rule_candidates": [], "concepts": [],
                        "questions": [f"{f} の値と根拠を確認できますか？回答でルール適用可否が変わります。" for f in fields]}
        proposal = self.propose_knowledge("suggestion", {"text": text, "source": "human_dialogue", "task": task,
                                            "response": response, "error": error, "context": context}, case_id)
        with self.store.transaction():
            self._state(proposal["id"], case_id, "suggestion_review")
        return {**proposal, "response": response, "error": error, "knowledge_activated": False}

    def report_debrief(self, case_id, narrative, extractor=None):
        """Ask for natural-language action description: no tool events required."""
        self.case(case_id)
        if not narrative.strip():
            raise ValueError("Natural-language narrative must not be blank")
        interpretation, error = self._extract_narrative(extractor, narrative)
        followups = self._debrief_followups(interpretation)
        did, stamp = self.make_id("D"), self.clock()
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO debriefs VALUES(?,?,?,?,?,?,?,?)", (
                did, case_id, narrative, encode(interpretation) if interpretation else None,
                "draft" if interpretation else "needs_extraction", encode(followups), "human_self_report", stamp))
            self.store.audit("human_narrative_collected", did, {"case_id": case_id}, stamp)
            self._state(did, case_id, "debrief_confirmation", {"error": error})
            if error:
                self.store.audit("extraction_failed", did, {"error": error}, stamp)
        return {"id": did, "interpretation": interpretation, "followups": followups,
                "status": "draft" if interpretation else "needs_extraction", "error": error}

    @staticmethod
    def _extract_narrative(extractor, narrative):
        if extractor is None:
            return None, None
        try:
            return validate_procedure(extractor.extract(narrative)), None
        except Exception as exc:
            return None, f"{type(exc).__name__}: extraction failed; narrative retained"

    @staticmethod
    def _debrief_followups(result):
        if not result:
            return ["どのような状況で、何を確認・計算しましたか？", "その作業を選んだ理由と結果は何でしたか？"]
        missing = set(result.get("missing", []))
        questions = []
        if "trigger" in missing or not result.get("trigger"):
            questions.append("どんな状況でその作業が必要になりましたか？")
        if "reason" in missing or any(not s.get("reason") for s in result.get("steps", [])):
            questions.append("その確認・計算を選んだ理由は何でしたか？")
        if "result" in missing or not result.get("outcome"):
            questions.append("結果はどうでしたか？何をもって確認しましたか？")
        if "validation" in missing:
            questions.append("結論を裏付ける資料や実測はありますか？")
        return questions[:2]

    def extend_debrief(self, debrief_id, free_text, extractor=None):
        d = self.store.one("SELECT * FROM debriefs WHERE id=?", (debrief_id,))
        if not d:
            raise KeyError(debrief_id)
        if d["interpretation_status"] == "confirmed":
            raise ValueError("Confirmed debrief is immutable; open a new one")
        narrative = d["report_text"] + "\n追加回答: " + free_text
        interpretation, error = self._extract_narrative(extractor, narrative)
        followups = self._debrief_followups(interpretation)
        with self.store.transaction():
            self.store.conn.execute("UPDATE debriefs SET report_text=?, interpretation=?, interpretation_status=?, followups=? WHERE id=?",
                                    (narrative, encode(interpretation) if interpretation else None,
                                     "draft" if interpretation else "needs_extraction", encode(followups), debrief_id))
            self.store.audit("narrative_extended", debrief_id, {"before": d, "text": free_text, "error": error}, self.clock())
        return {"id": debrief_id, "interpretation": interpretation, "followups": followups, "error": error}

    def confirm_debrief(self, debrief_id, edited=None):
        d = self.store.one("SELECT * FROM debriefs WHERE id=?", (debrief_id,))
        if not d:
            raise KeyError(debrief_id)
        if d["interpretation_status"] != "draft" and not (d["interpretation_status"] == "needs_extraction" and edited is not None):
            raise ValueError("Debrief requires an LLM draft, not already confirmed")
        interpreted = edited if edited is not None else decode(d["interpretation"])
        interpreted = validate_procedure(interpreted)
        interpreted["provenance"] = list(dict.fromkeys([*interpreted["provenance"], f"debrief:{debrief_id}"]))
        pid, stamp = self.make_id("PROC"), self.clock()
        with self.store.transaction():
            self.store.conn.execute("UPDATE debriefs SET interpretation_status='confirmed',interpretation=? WHERE id=?",
                                    (encode(interpreted), debrief_id))
            self.store.conn.execute("INSERT INTO procedure_candidates VALUES(?,?,?,?,?,?,?,?)", (
                pid, d["case_id"], debrief_id, encode(interpreted), "interpretation_confirmed", stamp, None, stamp))
            self.store.audit("narrative_interpretation_confirmed", debrief_id, {"procedure_candidate": pid}, stamp)
            self._state(debrief_id, d["case_id"], "completed")
            self._state(pid, d["case_id"], "procedure_approval")
        return {"procedure_id": pid, "status": "confirmed_unvalidated", "lifecycle_status": "interpretation_confirmed"}

    def approve_procedure(self, procedure_id, approved_by_expert=False):
        if not approved_by_expert:
            raise PermissionError("Procedure needs explicit expert approval")
        p = self.store.one("SELECT * FROM procedure_candidates WHERE id=?", (procedure_id,))
        if not p:
            raise KeyError(procedure_id)
        if p["status"] not in {"confirmed_unvalidated", "interpretation_confirmed"}:
            raise ValueError("Procedure requires confirmed interpretation first")
        stamp = self.clock()
        with self.store.transaction():
            self.store.conn.execute("UPDATE procedure_candidates SET status='approved_candidate',approved_at=? WHERE id=?", (stamp, procedure_id))
            self.store.audit("procedure_candidate_approved", procedure_id, {"note": "Not an executable autonomous workflow"}, stamp)
            self._state(procedure_id, p["case_id"], "completed")
        return {"procedure_id": procedure_id, "status": "approved_candidate", "executable": False}
