import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, features TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',
  expert_decision TEXT, feature_schema_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_feature_updates (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, feature_id TEXT NOT NULL,
  feature_json TEXT NOT NULL, source_question_id TEXT,
  recorded_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES cases(id)
);
CREATE TABLE IF NOT EXISTS rules (
  id TEXT PRIMARY KEY, parent_id TEXT, decision_type TEXT NOT NULL,
  predicates TEXT NOT NULL, conclusion TEXT NOT NULL,
  cornerstone_case_id TEXT, corrected_case_id TEXT,
  explanation TEXT NOT NULL, feedback_id TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL, published_at TEXT,
  FOREIGN KEY(parent_id) REFERENCES rules(id)
);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, decision_type TEXT NOT NULL,
  conclusion TEXT NOT NULL, rule_id TEXT, status TEXT NOT NULL,
  snapshot TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, decision_id TEXT,
  free_text TEXT NOT NULL, corrected_to TEXT, interpretation TEXT,
  confirmed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, target_decision TEXT NOT NULL,
  field_id TEXT NOT NULL, question TEXT NOT NULL, status TEXT NOT NULL,
  answer TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS debriefs (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, report_text TEXT NOT NULL,
  interpretation TEXT, interpretation_status TEXT NOT NULL,
  followups TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'human_self_report',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS procedure_candidates (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, debrief_id TEXT NOT NULL,
  body TEXT NOT NULL, status TEXT NOT NULL,
  confirmed_at TEXT, approved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
  object_id TEXT NOT NULL, payload TEXT NOT NULL, timestamp TEXT NOT NULL
);
"""

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def decode(value):
    return json.loads(value) if value is not None else None

class Store:
    def __init__(self, path="learning.sqlite3"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            from .migrations import migrate
            migrate(self.conn)
        except Exception:
            self.conn.close()
            raise

    @contextmanager
    def transaction(self):
        nested = self.conn.in_transaction
        savepoint = "tx_" + uuid.uuid4().hex
        self.conn.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
        try:
            yield self.conn
            if nested:
                self.conn.execute("RELEASE SAVEPOINT " + savepoint)
            else:
                self.conn.commit()
        except Exception:
            if nested:
                self.conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                self.conn.execute("RELEASE SAVEPOINT " + savepoint)
            else:
                self.conn.rollback()
            raise

    def one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def many(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def audit(self, event_type, object_id, payload, timestamp):
        payload = {"actor": "local_user", "available_at": timestamp, **payload}
        self.conn.execute(
            "INSERT INTO audit(event_type, object_id, payload, timestamp) VALUES (?,?,?,?)",
            (event_type, object_id, encode(payload), timestamp)
        )
