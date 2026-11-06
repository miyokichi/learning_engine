import json
import importlib.metadata
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from learning_engine import Engine
from learning_engine.demo_conversation import SAMPLE_TEXTS
from learning_engine.migrations import SQL
from learning_engine.store import SCHEMA


class V03CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.db = self.directory / "learning.db"
        self.executable = Path(sys.executable).with_name("learning-engine.exe" if os.name == "nt" else "learning-engine")
        if not self.executable.exists():
            self.skipTest("pip install -e . is required for installed CLI E2E")

    def cli(self, *args, text=None, db=None):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "LE_LLM_URL": "", "LE_LLM_MODEL": ""}
        result = subprocess.run([str(self.executable), "--db", str(db or self.db), *args], cwd=self.directory,
                                input=text, capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_installed_chat_full_cycle_restart_without_ids_or_json_input(self):
        t = SAMPLE_TEXTS
        commands = [t[0], "確認", t[1], "確認", "確認", t[2], "確認", t[3], "確認", t[4], "確認", "確認", t[5], "確認", t[6], "確認", "確認", "/exit"]
        output = self.cli("chat", "--sample", text="\n".join(commands) + "\n")
        self.assertIn("状態：ASK", output)
        self.assertIn("ルールを公開しました", output)
        self.assertIn("判断：REQUIRE_VALIDATION", output)
        self.assertIn("参考候補として保存", output)
        self.assertNotIn("処理を保留しました", output)
        session = re.search(r"CHAT-[0-9a-f]+", output)[0]
        with Engine(self.db) as e:
            count = len(e.store.many("SELECT * FROM cases"))
            rules = e.rules()
        resumed = self.cli("chat", "--sample", "--session", session, text="/status\n確認\n/exit\n")
        self.assertIn("二重採用", resumed)
        with Engine(self.db) as e:
            self.assertEqual(count, len(e.store.many("SELECT * FROM cases")))
            self.assertEqual(rules, e.rules())
        # Default launch offers selection rather than silently replacing the session.
        selection = self.cli("chat", "--sample", text="1\n/exit\n")
        self.assertIn("前回の会話", selection)

    def test_installed_package_version_matches_source(self):
        from learning_engine import __version__
        self.assertEqual(__version__, "0.5.0")
        self.assertEqual(importlib.metadata.version("learning-engine"), __version__)

    def test_tutorial_guided_commands_and_resume(self):
        first = self.cli("tutorial", text="確認\n確認\n\n")
        self.assertIn("中断しました", first)
        rest = self.cli("tutorial", text=("確認\n" * 5))
        self.assertIn("未経験案件の判断：REQUIRE_VALIDATION", rest)
        again = self.cli("tutorial", text="")
        self.assertIn("完了済み", again)
        self.assertNotIn("1/7", again)

    def test_experiment_does_not_open_production_db_and_is_repeatable(self):
        path = self.directory / "must-not-open" / "production.db"
        a = self.cli("replay-experiment", db=path)
        b = self.cli("replay-experiment", db=path)
        self.assertEqual(json.loads(a), json.loads(b))
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_doctor_does_not_connect_and_empty_chat_explains_limit(self):
        output = json.loads(self.cli("doctor"))
        self.assertEqual(output["llm_connection"], "not_requested")
        self.assertEqual(output["schema_migration"], 6)
        chat = self.cli("chat", text="作業報告を残したい\n/exit\n")
        self.assertIn("自動解釈は無効", chat)
        with Engine(self.db) as e:
            self.assertTrue(e.store.one("SELECT * FROM conversation_turns WHERE role='human'"))

    def test_invalid_db_has_actionable_cli_error_without_traceback(self):
        result = subprocess.run([str(self.executable), "--db", str(self.directory), "doctor"],
                                capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class V02MigrationTests(unittest.TestCase):
    def test_v02_data_is_unchanged_by_idempotent_v03_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v02.db"
            conn = sqlite3.connect(path)
            conn.executescript(SCHEMA + SQL)
            stamp = "2025-01-01T00:00:00.000000+00:00"
            conn.execute("INSERT INTO cases VALUES(?,?,?,?,?,?,?)", ("OLD", stamp, "{}", "expert_approved", "OK", 1, stamp))
            conn.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("ROOT", None, "model_reuse", '[{"feature":"x","op":"eq","value":true}]', "OK", "OLD", None, "legacy explanation", None, "published", stamp, stamp))
            conn.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?)", ("FB", "OLD", None, "original correction", "OK", "interpretation", 1, stamp))
            conn.execute("INSERT INTO debriefs VALUES(?,?,?,?,?,?,?,?)", ("D", "OLD", "原文", None, "needs_extraction", "[]", "human_self_report", stamp))
            conn.execute("INSERT INTO procedure_candidates VALUES(?,?,?,?,?,?,?,?)", ("P", "OLD", "D", '{"goal":"old","steps":[]}', "approved_candidate", stamp, stamp, stamp))
            conn.execute("INSERT INTO audit(event_type,object_id,payload,timestamp) VALUES(?,?,?,?)", ("legacy_event", "OLD", '{"text":"原文"}', stamp))
            conn.execute("INSERT INTO schema_migrations VALUES(2)")
            conn.execute("PRAGMA user_version=2")
            tables = ["cases", "rules", "feedback", "debriefs", "procedure_candidates", "audit"]
            before = {t: conn.execute("SELECT * FROM " + t).fetchall() for t in tables}
            conn.commit(); conn.close()
            for _ in range(2):
                with Engine(path) as e:
                    self.assertEqual(e.store.one("SELECT max(version) v FROM schema_migrations")["v"], 6)
                    after = {t: [tuple(row) for row in e.store.conn.execute("SELECT * FROM " + t).fetchall()] for t in tables}
                    self.assertEqual(before, after)
