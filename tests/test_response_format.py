import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from learning_engine.cli import extractor, main
from learning_engine.demo import PROCEDURE
from learning_engine.llm import LocalChatExtractor


class ResponseFormatTests(unittest.TestCase):
    def opener(self, content):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"choices": [{"message": {"content": content}}]}).encode()
        return opener

    def test_default_and_opt_out_requests_parse_the_same_procedure(self):
        for mode in ("json_object", "none"):
            with self.subTest(mode=mode):
                client = LocalChatExtractor("http://llm.internal/v1", "model", response_format=mode)
                opener = self.opener(json.dumps(PROCEDURE))
                with patch("urllib.request.build_opener", return_value=opener):
                    self.assertEqual(client.extract("作業報告")["goal"], PROCEDURE["goal"])
                payload = json.loads(opener.open.call_args.args[0].data)
                if mode == "none":
                    self.assertNotIn("response_format", payload)
                else:
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                self.assertNotIn("temperature", payload)
        self.assertEqual(LocalChatExtractor("http://llm.internal/v1", "model").response_format, "json_object")

    def test_vision_opt_out_retains_image_payload(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        client = LocalChatExtractor("http://llm.internal/v1", "vision", response_format="none")
        opener = self.opener(json.dumps({"intent": "clarify", "payload": {}, "message": "スケールは？"}))
        with patch("urllib.request.build_opener", return_value=opener):
            client.interpret_images("画像です", {}, [{"id": "IMAGE-1", "filename": "断面.png", "mime_type": "image/png", "data": png}])
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertNotIn("response_format", payload)
        parts = payload["messages"][1]["content"]
        self.assertEqual(parts[-1]["image_url"]["url"], "data:image/png;base64," + base64.b64encode(png).decode())

    def test_opt_out_still_rejects_non_json_and_invalid_schema(self):
        client = LocalChatExtractor("http://llm.internal/v1", "model", response_format="none")
        for content in ("JSONではない返答", '{"intent":"publish_rule","payload":{}}'):
            with self.subTest(content=content):
                opener = self.opener(content)
                with patch("urllib.request.build_opener", return_value=opener), self.assertRaises(ValueError):
                    client.interpret("相談", {})
                self.assertEqual(opener.open.call_count, 1)

    def test_config_precedence_and_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.env"
            path.write_text("LE_LLM_URL=http://llm.internal/v1\nLE_LLM_MODEL=model\nLE_LLM_RESPONSE_FORMAT=none\n", encoding="utf-8")
            args = argparse.Namespace(env_file=str(path), llm_url=None, model=None, response_format=None)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(extractor(args).response_format, "none")
                self.assertNotIn("LE_LLM_RESPONSE_FORMAT", os.environ)
            with patch.dict(os.environ, {"LE_LLM_RESPONSE_FORMAT": "json_object"}, clear=True):
                self.assertEqual(extractor(args).response_format, "json_object")
                args.response_format = "none"
                self.assertEqual(extractor(args).response_format, "none")
            args.response_format = None
            with patch.dict(os.environ, {"LE_LLM_RESPONSE_FORMAT": "invalid"}, clear=True), self.assertRaisesRegex(ValueError, "LE_LLM_RESPONSE_FORMAT"):
                extractor(args)

    def test_all_llm_commands_accept_cli_override(self):
        for command, options in (("chat", []), ("interact", []), ("doctor", []),
                                 ("consult", ["--case", "C", "--text", "相談"]),
                                 ("compare-procedures", ["--id", "P"]),
                                 ("debrief", ["--case", "C"]), ("extend-debrief", ["--id", "D", "--text", "追加"])):
            with self.subTest(command=command), patch("learning_engine.cli.Engine") as engine, patch("learning_engine.cli.dispatch") as dispatch:
                main([command, *options, "--response-format", "none"])
                self.assertEqual(dispatch.call_args.args[0].response_format, "none")

    def test_doctor_connects_to_server_rejecting_response_format(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                self.send_response(400 if "response_format" in body else 200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode())

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = root / "local.env"
                    config.write_text(f"LE_LLM_URL=http://127.0.0.1:{server.server_port}/v1\nLE_LLM_MODEL=model\nLE_LLM_RESPONSE_FORMAT=none\n", encoding="utf-8")
                    env = {k: v for k, v in os.environ.items() if not k.startswith("LE_LLM_")}
                    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
                    for override, cli, expected in (({}, [], "ok"), ({"LE_LLM_RESPONSE_FORMAT": "json_object"}, [], "failed:HTTPError"),
                                                    ({"LE_LLM_RESPONSE_FORMAT": "json_object"}, ["--response-format", "none"], "ok")):
                        result = subprocess.run([sys.executable, "-m", "learning_engine", "--env-file", str(config), "--db", str(root / "test.db"),
                                                 "doctor", "--check-llm", *cli], env={**env, **override}, cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout)["llm_connection"], expected)
                    self.assertNotIn("response_format", requests[0])
                    self.assertEqual(requests[1]["response_format"], {"type": "json_object"})
                    self.assertNotIn("response_format", requests[2])
            finally:
                server.shutdown()
                thread.join(timeout=2)
