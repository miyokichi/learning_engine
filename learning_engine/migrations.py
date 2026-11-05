"""Additive v0.2 migration. v0.1 tables and immutable input remain intact."""
SQL = """
CREATE TABLE IF NOT EXISTS feature_definitions (
 id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_metadata (
 case_id TEXT PRIMARY KEY REFERENCES cases(id), body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, case_id TEXT REFERENCES cases(id),
 body TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, object_id TEXT NOT NULL,
 body TEXT NOT NULL, status TEXT NOT NULL, available_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL REFERENCES cases(id),
 decision TEXT, status TEXT NOT NULL, available_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL REFERENCES rules(id),
 status TEXT NOT NULL, available_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow (
 object_id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 state TEXT NOT NULL, body TEXT NOT NULL, available_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
"""


def migrate(conn):
    conn.executescript(SQL)
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
        # v0.1 audit already records review availability; do not invent old labels.
        conn.execute("""INSERT INTO review_history(case_id,decision,status,available_at)
          SELECT object_id,json_extract(payload,'$.decision'),
          json_extract(payload,'$.status'),timestamp FROM audit
          WHERE event_type='case_reviewed' ORDER BY timestamp,id""")
        conn.execute("""INSERT INTO rule_events(rule_id,status,available_at)
          SELECT id,'published',published_at FROM rules WHERE published_at IS NOT NULL""")
        conn.execute("INSERT INTO schema_migrations VALUES(2)")
        conn.execute("PRAGMA user_version=2")
    conn.commit()
