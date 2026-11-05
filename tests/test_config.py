import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from learning_engine.config import read_env
from learning_engine.cli import extractor


class ConfigTests(unittest.TestCase):
    def test_utf8_bom_quotes_comments_and_literal_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.env"
            path.write_text('# コメント\nexport LE_LLM_MODEL="日本語 model" # comment\n'
                            "LE_LLM_API_KEY='secret#literal'\nLE_LLM_URL=http://localhost:1234/v1 # endpoint\n"
                            "LITERAL=${NO_EXPANSION}\n", encoding="utf-8-sig")
            values = read_env(path)
            self.assertEqual(values["LE_LLM_MODEL"], "日本語 model")
            self.assertEqual(values["LE_LLM_API_KEY"], "secret#literal")
            self.assertEqual(values["LE_LLM_URL"], "http://localhost:1234/v1")
            self.assertEqual(values["LITERAL"], "${NO_EXPANSION}")

    def test_precedence_and_no_environment_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.env"
            path.write_text("LE_LLM_URL=http://localhost:1234/v1\nLE_LLM_MODEL=file-model\nLE_LLM_API_KEY=file-key\n", encoding="utf-8")
            args = argparse.Namespace(env_file=str(path), llm_url=None, model=None)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(extractor(args).model, "file-model")
                self.assertNotIn("LE_LLM_MODEL", os.environ)
            with patch.dict(os.environ, {"LE_LLM_MODEL": "env-model"}, clear=True):
                self.assertEqual(extractor(args).model, "env-model")
                args.model = "cli-model"
                args.llm_url = "http://127.0.0.1:5678/v1"
                client = extractor(args)
                self.assertEqual(client.model, "cli-model")
                self.assertEqual(client.url, args.llm_url)
                self.assertEqual(client.api_key, "file-key")

    def test_missing_optional_file_and_errors_do_not_expose_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.env"
            self.assertEqual(read_env(path), {})
            with self.assertRaises(ValueError):
                read_env(path, required=True)
            for text in ["SECRET='sensitive", "INVALID sensitive"]:
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError) as error:
                    read_env(path)
                self.assertNotIn("sensitive", str(error.exception))

    def test_cli_automatically_reads_working_directory_env_and_explicit_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("LE_LLM_URL=http://localhost:1234/v1\nLE_LLM_MODEL=offline-test\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("LE_LLM_")}
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            for options in [[], ["--env-file", str(path)]]:
                result = subprocess.run([sys.executable, "-m", "learning_engine", *options, "--db", "test.db", "interact"],
                                        cwd=directory, input="0\n", capture_output=True, text=True, encoding="utf-8", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("自動解釈は無効", result.stdout)


if __name__ == "__main__":
    unittest.main()
