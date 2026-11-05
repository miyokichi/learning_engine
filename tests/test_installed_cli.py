"""Installed entrypoint E2E from outside the source directory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class InstalledCLITests(unittest.TestCase):
    def test_installed_cli_full_learning_cycle_from_other_directory(self):
        executable = Path(sys.executable).with_name("learning-engine.exe" if os.name == "nt" else "learning-engine")
        if not executable.exists():
            self.skipTest("Install with pip install -e . to verify the console entrypoint")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "demo.db")
            def cli(*args, input=None, structured=True):
                result = subprocess.run([str(executable), "--db", db, *args], cwd=directory,
                                        input=input, text=True, capture_output=True, encoding="utf-8",
                                        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout) if structured else result.stdout
            self.assertIn("interact", cli("--help", structured=False))
            self.assertEqual(cli("init")["status"], "initialized")
            demo = cli("demo")
            self.assertEqual(demo["next_case"]["decision"], "REQUIRE_VALIDATION")
            self.assertEqual(cli("demo")["status"], "already_completed")
            self.assertEqual(cli("metrics")["wrong_confirmations"], 0)
            self.assertTrue(cli("replay", "--mode", "point_in_time"))
            self.assertTrue(cli("replay", "--mode", "current_knowledge"))
            self.assertIn("自動解釈は無効", cli("interact", input="6\n0\n", structured=False))
            raw = "断面を比較した。側壁が違うため形状を考慮した計算を試し、判断を保留した。"
            draft = cli("debrief", "--case", "DEMO-NEW", "--text", raw)
            procedure = cli("confirm-debrief", "--id", draft["id"], "--edited", str(root / "examples/procedure_manual.json"))
            cli("approve-procedure", "--id", procedure["procedure_id"], "--expert-approved")
            references = cli("procedures", "--case", "DEMO-NEXT")
            comparison = cli("compare-procedures", "--id", references[0]["id"], "--id", references[1]["id"])
            self.assertFalse(comparison["executable"])
            proposal = cli("propose-knowledge", "--kind", "concept", "--case", "DEMO-NEW", "--body", str(root / "examples/concept_bottom_radius.json"))
            self.assertIn("DEMO-OLD", cli("concept-impacts", "--id", proposal["id"])["cases"])
            cli("adopt-knowledge", "--id", proposal["id"], "--expert-confirmed")
            evaluated = cli("evaluate", "--case", "DEMO-NEXT")
            self.assertEqual(evaluated["decision"], "ASK")
            q = evaluated["questions"][0]
            cli("answer-question", "--id", q["id"], "--text", "架空の測定値は5nm")
            cli("confirm-answer", "--id", q["id"], "--value", "5", "--expert-confirmed")
            self.assertEqual(cli("evaluate", "--case", "DEMO-NEXT")["decision"], "REVIEW")
            cli("review-case", "--case", "DEMO-NEXT", "--decision", "REQUIRE_VALIDATION")
            self.assertEqual(cli("evaluate", "--case", "DEMO-NEXT")["decision"], "REQUIRE_VALIDATION")


if __name__ == "__main__":
    unittest.main()
