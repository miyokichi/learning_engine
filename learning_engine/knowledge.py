"""Explicit, auditable concept/evidence adoption and scoped procedure candidates."""
import math
from .store import encode, decode

FEATURE_STATUSES = {"measured", "documented", "expert_confirmed", "derived_verified",
                    "unknown", "assumed", "conflicting"}


def validate_feature(entry):
    if not isinstance(entry, dict) or entry.get("status") not in FEATURE_STATUSES:
        raise ValueError("Feature requires a supported status")
    value = entry.get("value")
    if value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError("Feature value must be scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Feature number must be finite")
    if entry["status"] in {"measured", "documented", "expert_confirmed", "derived_verified"} and value is None:
        raise ValueError("Confirmed feature needs a value")


class KnowledgeMixin:
    def _state(self, object_id, case_id, state, body=None):
        from .engine import now
        self.store.conn.execute("INSERT OR REPLACE INTO workflow VALUES(?,?,?,?,?)",
                                (object_id, case_id, state, encode(body or {}), now()))

    def pending(self):
        return self.store.many("SELECT * FROM workflow WHERE state NOT IN ('completed','published','rejected','EXECUTE') ORDER BY available_at,object_id")

    def register_feature(self, feature_id, definition, aliases=(), unit=None,
                         measurement=None, version=1, expert_confirmed=False):
        from .engine import now
        if not expert_confirmed:
            raise PermissionError("Feature definition needs expert confirmation")
        if not isinstance(aliases, (list, tuple)):
            raise ValueError("aliases must be a sequence of names")
        if not feature_id.strip() or not definition.strip() or not isinstance(version, int) or version < 1:
            raise ValueError("Feature needs an id, definition and positive version")
        names = [feature_id, *aliases]
        if len(names) != len(set(names)) or any(not isinstance(n, str) or not n.strip() for n in names):
            raise ValueError("Invalid aliases")
        for row in self.store.many("SELECT * FROM feature_definitions"):
            other = decode(row["body"])
            if set(names) & {row["id"], *other["aliases"]}:
                raise ValueError("Definition/alias already exists; use a new canonical id for different semantics")
        body = dict(definition=definition, aliases=list(aliases), unit=unit,
                    measurement=measurement, version=version)
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO feature_definitions VALUES(?,?,?)", (feature_id, encode(body), now()))
            self.store.audit("feature_registered", feature_id, body, now())
        return body

    def normalize_features(self, features):
        definitions = self.store.many("SELECT * FROM feature_definitions")
        result = {}
        for key, original in features.items():
            entry = dict(original) if isinstance(original, dict) else original
            validate_feature(entry)
            canonical = key
            for row in definitions:
                body = decode(row["body"])
                if key == row["id"] or key in body["aliases"]:
                    canonical = row["id"]
                    if entry.get("unit", body["unit"]) != body["unit"]:
                        raise ValueError("Unit mismatch; automatic conversion is disabled")
                    if entry.get("definition_version", body["version"]) != body["version"]:
                        raise ValueError("Definition version mismatch")
                    entry.update(unit=body["unit"], definition_version=body["version"])
                    break
            if canonical in result:
                raise ValueError("Duplicate canonical feature")
            result[canonical] = entry
        return result

    def propose_knowledge(self, kind, body, case_id=None):
        from .engine import now, new_id
        if kind not in {"observation", "causal_hypothesis", "computational_dependency", "evidence", "concept", "suggestion"}:
            raise ValueError("Unsupported knowledge kind")
        if not isinstance(body, dict) or not isinstance(body.get("text"), str) or not body["text"].strip() or not isinstance(body.get("source"), str) or not body["source"].strip():
            raise ValueError("Knowledge needs text and source")
        if kind == "concept":
            if body.get("relation_type", "distinct") not in {"alias", "subconcept", "distinct"}:
                raise ValueError("Concept relation must be alias/subconcept/distinct")
            if not isinstance(body.get("scope", {}), dict):
                raise ValueError("Concept scope must be a feature/value mapping")
        if kind == "evidence" and body.get("polarity", "supports") not in {"supports", "refutes"}:
            raise ValueError("Evidence polarity must be supports/refutes")
        if body.get("claim_id") and not self.store.one("SELECT id FROM knowledge WHERE id=?", (body["claim_id"],)):
            raise ValueError("Unknown evidence claim")
        if case_id:
            self.case(case_id)
        kid, stamp = new_id("K"), now()
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO knowledge VALUES(?,?,?,?,?,?)", (kid, kind, case_id, encode(body), "draft", stamp))
            self.store.conn.execute("INSERT INTO knowledge_revisions(object_id,body,status,available_at) VALUES(?,?,?,?)",
                                    (kid, encode(body), "draft", stamp))
            self.store.audit("knowledge_proposed", kid, {"kind": kind, "body": body}, stamp)
            if case_id:
                self._state(kid, case_id, "knowledge_confirmation", body)
        return {"id": kid, "status": "draft"}

    def adopt_knowledge(self, knowledge_id, expert_confirmed=False, reject=False):
        from .engine import now
        if not expert_confirmed:
            raise PermissionError("Knowledge adoption needs explicit expert confirmation")
        row = self.store.one("SELECT * FROM knowledge WHERE id=?", (knowledge_id,))
        if not row or row["status"] != "draft":
            raise ValueError("Knowledge is not a draft")
        body = decode(row["body"])
        if row["kind"] == "suggestion" and not reject:
            raise ValueError("Suggestions must be edited into a specific knowledge proposal or correction workflow")
        if row["kind"] == "causal_hypothesis" and not reject and not body.get("validation_evidence"):
            raise ValueError("A hypothesis requires validation evidence before adoption")
        impacts = self.concept_impacts(knowledge_id) if row["kind"] == "concept" else {"cases": [], "rules": []}
        if row["kind"] == "concept" and not reject:
            for field in ("feature_id", "definition", "measurement"):
                if not body.get(field):
                    raise ValueError(f"Concept needs {field}")
            if "scope" not in body or not isinstance(body["scope"], dict):
                raise ValueError("Concept needs explicit scope ({} means all cases)")
        status, stamp = "rejected" if reject else "verified", now()
        with self.store.transaction():
            if row["kind"] == "concept" and not reject:
                self.register_feature(body["feature_id"], body["definition"], body.get("aliases", []),
                                      body.get("unit"), body["measurement"], expert_confirmed=True)
            self.store.conn.execute("UPDATE knowledge SET status=? WHERE id=?", (status, knowledge_id))
            self.store.conn.execute("INSERT INTO knowledge_revisions(object_id,body,status,available_at) VALUES(?,?,?,?)", (knowledge_id, row["body"], status, stamp))
            self.store.audit("knowledge_" + status, knowledge_id, {"body": body, "impacts": impacts}, stamp)
            if row["case_id"]:
                self._state(knowledge_id, row["case_id"], "rejected" if reject else "completed")
            if not reject and row["kind"] == "concept":
                for case_id in impacts["cases"]:
                    if body.get("important"):
                        self._state(knowledge_id + ":" + case_id, case_id, "needs_review", {"concept": knowledge_id, "feature": body["feature_id"]})
                        self.store.conn.execute("UPDATE decisions SET status='needs_review' WHERE case_id=?", (case_id,))
        return {"id": knowledge_id, "status": status, "impacts": impacts}

    def concept_impacts(self, knowledge_id):
        row = self.store.one("SELECT * FROM knowledge WHERE id=? AND kind='concept'", (knowledge_id,))
        if not row:
            raise ValueError("Not a concept proposal")
        body = decode(row["body"])
        scope = body.get("scope", {})
        if not isinstance(scope, dict):
            raise ValueError("scope must map feature ids to exact values")
        cases = [r["id"] for r in self.store.many("SELECT id FROM cases ORDER BY id")
                 if all(self.case(r["id"])["features"].get(k, {}).get("value") == v for k, v in scope.items())]
        rules = [r["id"] for r in self.rules() if r["cornerstone_case_id"] in cases or r["corrected_case_id"] in cases
                 or any(p["feature"] in body.get("related_features", []) for p in r["predicates"])]
        return {"cases": cases, "rules": rules, "questions": [f"{body.get('feature_id', 'new feature')} の定義・測り方・単位・適用範囲を確認してください"],
                "note": "potential impact only; historical values remain unknown"}

    def procedures(self, case_id):
        from .logic import all_predicates, Truth
        features = self.case(case_id)["features"]
        result = []
        for row in self.store.many("SELECT * FROM procedure_candidates WHERE status IN ('approved_candidate','validated_for_scope') ORDER BY id"):
            body = decode(row["body"])
            scope = body.get("scope", {})
            if all_predicates(features, [{"feature": k, "op": "eq", "value": v} for k, v in scope.items()]) == Truth.TRUE:
                result.append({"id": row["id"], "body": body, "status": row["status"], "executable": False,
                               "applicability": "scope_matched" if scope else "reference_only"})
        return result

    def compare_procedures(self, procedure_ids):
        if len(set(procedure_ids)) < 2:
            raise ValueError("Compare at least two different reports")
        bodies = []
        for pid in procedure_ids:
            row = self.store.one("SELECT body FROM procedure_candidates WHERE id=?", (pid,))
            if not row:
                raise KeyError(pid)
            bodies.append(decode(row["body"]))
        actions = [{s["action"] for s in b["steps"]} for b in bodies]
        return {"status": "draft", "sources": procedure_ids, "common_steps": sorted(set.intersection(*actions)),
                "branch_candidates": [{"trigger": b.get("trigger"), "steps": sorted(a - set.intersection(*actions))} for a, b in zip(actions, bodies)],
                "required_order": "unknown", "executable": False}

    def validate_procedure_scope(self, procedure_id, scope, evidence, expert_confirmed=False):
        from .engine import now
        if not expert_confirmed or not evidence or not isinstance(scope, dict) or not scope:
            raise PermissionError("Explicit scope, validation evidence and expert approval required")
        row = self.store.one("SELECT * FROM procedure_candidates WHERE id=?", (procedure_id,))
        if not row or row["status"] != "approved_candidate":
            raise ValueError("Approve candidate first")
        body = decode(row["body"])
        body.update(scope=scope, validation_evidence=evidence)
        with self.store.transaction():
            self.store.conn.execute("UPDATE procedure_candidates SET body=?,status='validated_for_scope' WHERE id=?", (encode(body), procedure_id))
            self.store.audit("procedure_scope_validated", procedure_id, body, now())
        return {"status": "validated_for_scope", "executable": False}
