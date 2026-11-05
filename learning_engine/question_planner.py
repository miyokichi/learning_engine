"""Persisted, reasoned question candidates. Model importance is a hypothesis."""
from .store import encode, decode
from .logic import ACCEPTED_STATUSES

KEYS = {"field_id", "target_decision", "missing_information", "why_needed", "possible_answers",
        "how_answer_changes_next_step", "source_case_or_rule", "priority"}


def validate_candidate(value):
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError("質問候補の形式が不正です")
    for key in KEYS - {"possible_answers", "source_case_or_rule"}:
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("質問候補には判断への影響と理由が必要です")
    for key in ("possible_answers", "source_case_or_rule"):
        if not isinstance(value[key], list) or any(not isinstance(s, str) for s in value[key]):
            raise ValueError("質問候補の選択肢・出典が不正です")
    if value["priority"] not in {"critical", "useful", "optional"}:
        raise ValueError("質問の優先度が不正です")
    return value


class QuestionPlanner:
    def __init__(self, engine):
        self.e = engine

    def plan(self, case_id, decision_type="model_reuse", proposed=()):
        current = self.e.case(case_id)
        evaluation = self.e.evaluate(case_id, decision_type, persist=False)
        basis_needed = evaluation["reason"] in {"no_root_or_ambiguous", "ambiguous_exceptions"}
        known_rule_fields = {p["feature"] for r in self.e.rules() if r["status"] == "published" and r["decision_type"] == decision_type for p in r["predicates"]}
        candidates = []
        for field in evaluation["unknown_fields"]:
            candidates.append(dict(field_id=field, target_decision=decision_type,
                missing_information=f"{field} の値と確認根拠", why_needed="ルールの適用可否を左右する条件が未確認です",
                possible_answers=["確認済みの値", "未確認"], how_answer_changes_next_step="確認後にルールを再評価します",
                source_case_or_rule=evaluation["rule_path"], priority="critical"))
        if basis_needed:
            candidates.append(dict(field_id="judgment_basis", target_decision=decision_type,
                missing_information="結論を分ける条件と根拠", why_needed="承認済みの判断条件が不足または競合しています",
                possible_answers=["条件と判断理由の説明", "まだ分からない"], how_answer_changes_next_step="訂正・ルール候補または調査案を整理します",
                source_case_or_rule=[], priority="critical"))
        hypotheses = self.e.store.many("SELECT id,body FROM knowledge WHERE case_id=? AND kind='causal_hypothesis' AND status='draft' ORDER BY created_at,id", (case_id,))
        if len(hypotheses) >= 2:
            candidates.append(dict(field_id="hypothesis_discrimination", target_decision=decision_type,
                missing_information="複数の未検証仮説を区別する観測と反証", why_needed="複数の説明が候補のままで、どれが判断に使えるか確定していません",
                possible_answers=["支持する観測", "反証する観測", "未確認"], how_answer_changes_next_step="仮説を比較し、必要な検証や次の質問を整理します",
                source_case_or_rule=[case_id], priority="useful"))
        for comparison in self.e.retrieve_cases(case_id, decision_type=decision_type):
            for diff in comparison["differences"]:
                field = diff["feature"]
                if field not in known_rule_fields and field not in evaluation["unknown_fields"] and diff["current"].get("value") != diff["previous"].get("value"):
                    candidates.append(dict(field_id="relevance:" + field, target_decision=decision_type,
                        missing_information=f"{field} の差が判断に影響するか", why_needed="過去の承認済み事例と値が異なります。重要性は未検証です",
                        possible_answers=["影響する", "影響しない", "未確認"], how_answer_changes_next_step="重要なら新条件の候補を専門家と確認します",
                        source_case_or_rule=[comparison["case_id"]], priority="useful"))
        candidates.extend(validate_candidate(q) for q in proposed)
        with self.e.store.transaction():
            obsolete = self.e.store.many("SELECT * FROM question_candidates WHERE case_id=? AND target_decision=? AND status IN ('proposed','asked')", (case_id, decision_type))
            for old in obsolete:
                if (old["field_id"] == "judgment_basis" and not basis_needed) or (old["field_id"].startswith("relevance:") and old["field_id"][10:] in known_rule_fields):
                    self.e.store.conn.execute("UPDATE question_candidates SET status='superseded' WHERE id=?", (old["id"],))
                    if old["question_id"]:
                        self.e.store.conn.execute("UPDATE questions SET status='superseded' WHERE id=? AND status IN ('open','answered_unverified')", (old["question_id"],))
                        self.e.store.conn.execute("UPDATE workflow SET state='completed' WHERE object_id=?", (old["question_id"],))
                    self.e.store.audit("question_superseded", old["id"], {"reason": "judgment_basis_now_available"}, self.e.clock())
            for candidate in candidates:
                candidate = validate_candidate(candidate)
                if candidate["target_decision"] != decision_type:
                    raise ValueError("質問の判断対象が現在の案件と一致しません")
                field = candidate["field_id"]
                entry = current["features"].get(field, {})
                if entry.get("status") in ACCEPTED_STATUSES and entry.get("value") is not None:
                    continue
                if self.e.store.one("SELECT id FROM question_candidates WHERE case_id=? AND target_decision=? AND field_id=?", (case_id, decision_type, field)):
                    continue
                existing = self.e.store.one("SELECT * FROM questions WHERE case_id=? AND target_decision=? AND field_id=? ORDER BY created_at DESC LIMIT 1", (case_id, decision_type, field))
                status = "answered" if existing and existing["status"] == "confirmed" else "proposed"
                cid = self.e.make_id("QC")
                self.e.store.conn.execute("INSERT INTO question_candidates VALUES(?,?,?,?,?,?,?,?,?)", (
                    cid, case_id, decision_type, field, encode(candidate), status, candidate["priority"], existing["id"] if existing else None, self.e.clock()))
                self.e.store.audit("question_candidate_proposed", cid, {"candidate": candidate, "unverified": candidate in proposed}, self.e.clock())
                if candidate in proposed and not self.e.store.one("SELECT id FROM feature_definitions WHERE id=?", (field,)):
                    self.e.propose_knowledge("concept", {"text": candidate["missing_information"], "source": "llm_question_hypothesis", "feature_id": field,
                        "relation_type": "distinct", "scope": {}, "hypothesis": candidate["why_needed"]}, case_id)
        return self.select(case_id, decision_type)

    def select(self, case_id, decision_type):
        rows = self.e.store.many("SELECT * FROM question_candidates WHERE case_id=? AND target_decision=? AND status IN ('proposed','asked') ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'useful' THEN 1 ELSE 2 END,created_at,id", (case_id, decision_type))
        selected = []
        for row in rows:
            field = row["field_id"]
            value = self.e.case(case_id)["features"].get(field, {})
            existing = self.e.store.one("SELECT * FROM questions WHERE case_id=? AND target_decision=? AND field_id=? ORDER BY created_at DESC LIMIT 1", (case_id, decision_type, field))
            if value.get("status") in ACCEPTED_STATUSES or (existing and existing["status"] == "confirmed"):
                with self.e.store.transaction():
                    self.e.store.conn.execute("UPDATE question_candidates SET status='answered' WHERE id=?", (row["id"],))
                continue
            selected.append({**row, "body": decode(row["body"])})
            if len(selected) == 2:
                break
        return selected

    def ask_batch(self, case_id, decision_type, proposed=()):
        selected = self.plan(case_id, decision_type, proposed)
        with self.e.store.transaction():
            for row in selected:
                qid = row["question_id"] or self.e.ask(case_id, decision_type, row["field_id"])
                self.e.store.conn.execute("UPDATE question_candidates SET status='asked',question_id=? WHERE id=?", (qid, row["id"]))
                row["question_id"] = qid
        return selected
