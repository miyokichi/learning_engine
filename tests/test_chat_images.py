import base64
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from learning_engine import Engine
from learning_engine.conversation import Conversation, chat
from learning_engine.images import load_image, MAX_IMAGE_BYTES
from learning_engine.llm import FakeChatExtractor, LocalChatExtractor
from learning_engine.store import decode


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def question(field="scale"):
    return {"field_id": field, "target_decision": "model_reuse",
            "missing_information": "画像のスケールと単位", "why_needed": "寸法の比較が必要です",
            "possible_answers": ["確認済みの寸法", "未確認"],
            "how_answer_changes_next_step": "寸法を確認後に比較します",
            "source_case_or_rule": [], "priority": "critical"}


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "断面 画像.png"
        self.path.write_bytes(PNG)
        self.db = self.root / "learning.db"
        self.e = Engine(self.db)
        self.addCleanup(self.e.close)

    def model(self):
        return FakeChatExtractor({
            "断面を比較したい": {"intent": "new_case", "payload": {
                "goal": "断面の比較", "features": {"scale": None}},
                "message": "断面を観察しました。スケールは未確認です。", "questions": [question()]},
            "未確認です": {"intent": "answer", "payload": {"values": {"scale": None}}},
            "観察を残して": {"intent": "observation", "payload": {
                "text": "側壁の形状が異なる。材質は未確認。", "features": {"different_wall": True, "material": None}}},
            "何を覚えている？": {"intent": "ask_about_knowledge", "payload": {}},
        })

    def test_multimodal_request_contains_original_bytes_without_temperature(self):
        client = LocalChatExtractor("http://llm.internal/v1", "vision")
        result = {"intent": "clarify", "payload": {}, "message": "何を判断しますか？"}
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"choices": [{"message": {"content": json.dumps(result)}}]}).encode()
        attachment = {"id": "IMAGE-1", **load_image(self.path)}
        with patch("urllib.request.build_opener", return_value=opener):
            self.assertEqual(client.interpret_images("比較したい", {"image_attachments": []}, [attachment])["intent"], "clarify")
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertNotIn("temperature", payload)
        parts = payload["messages"][1]["content"]
        self.assertEqual(parts[2]["image_url"]["url"], "data:image/png;base64," + base64.b64encode(PNG).decode())
        self.assertIn("IMAGE-1", parts[1]["text"])
        self.assertEqual(payload["model"], "vision")

    def test_image_questions_confirmation_knowledge_and_restart(self):
        model = self.model()
        c = Conversation(self.e, model)
        self.assertIn("画像の解釈", c.handle("断面を比較したい", [self.path]))
        self.assertFalse(self.e.store.many("SELECT * FROM cases"))
        self.assertFalse(self.e.store.many("SELECT * FROM knowledge WHERE status='verified'"))
        attachment = self.e.store.one("SELECT * FROM conversation_attachments")
        self.assertEqual(attachment["data"], PNG)
        self.assertEqual(attachment["filename"], self.path.name)
        self.assertNotIn("data", model.contexts[-1]["image_attachments"][0])
        self.assertIn("スケール", c.handle("確認"))
        case_id, sid = c.session["case_id"], c.id
        self.assertEqual(self.e.store.one("SELECT case_id FROM conversation_attachments")["case_id"], case_id)
        self.assertEqual(self.e.case(case_id)["features"]["scale"]["status"], "unknown")
        self.path.unlink()
        self.e.close()
        self.e = Engine(self.db)
        self.addCleanup(self.e.close)
        c = Conversation(self.e, model, sid)
        self.assertIn("スケール", c.describe_pending())
        c.handle("未確認です"); c.handle("確認")
        self.assertIsNone(self.e.case(case_id)["features"]["scale"]["value"])
        c.handle("観察を残して")
        self.assertNotIn("different_wall", self.e.case(case_id)["features"])
        c.handle("確認")
        feature = self.e.case(case_id)["features"]["different_wall"]
        self.assertIs(feature["value"], True)
        self.assertTrue(feature["source"].startswith("chat_turn:"))
        observation = self.e.store.one("SELECT * FROM knowledge WHERE kind='observation' ORDER BY created_at DESC,id DESC LIMIT 1")
        self.assertEqual(observation["status"], "verified")
        self.assertEqual(decode(observation["body"])["image_ids"], [attachment["id"]])
        self.assertIn("側壁", c.handle("何を覚えている？"))
        self.assertEqual(model.contexts[-1]["image_attachments"][0]["sha256"], attachment["sha256"])

    def test_failure_keeps_image_and_pending_and_retry_resends_bytes(self):
        model = self.model()
        c = Conversation(self.e, model)
        c.handle("断面を比較したい")
        action_id = c.pending["id"]
        class Broken:
            def interpret_images(self, *args):
                raise TimeoutError("offline")
        c.llm = Broken()
        self.assertIn("添付画像", c.handle("追加画像です", [self.path]))
        self.assertEqual(c.pending["id"], action_id)
        self.assertEqual(self.e.store.one("SELECT data FROM conversation_attachments")["data"], PNG)
        class Capture:
            def interpret_images(self, text, context, images):
                self.images = images
                return {"intent": "clarify", "payload": {}, "message": "スケールを教えてください"}
        c.llm = Capture()
        c.handle("再試行")
        self.assertEqual(c.llm.images[0]["data"], PNG)

    def test_invalid_files_and_batch_limit_do_not_alter_pending(self):
        c = Conversation(self.e, self.model())
        c.handle("断面を比較したい")
        aid = c.pending["id"]
        invalid = self.root / "fake.png"
        for data in (b"", b"not an image", b"x" * (MAX_IMAGE_BYTES + 1)):
            invalid.write_bytes(data)
            self.assertIn("読み込めません", c.handle("画像", [invalid]))
        self.assertIn("読み込めません", c.handle("画像", [self.root / "missing.png"]))
        self.assertIn("4枚以内", c.handle("画像", [self.path] * 5))
        self.assertEqual(c.pending["id"], aid)
        self.assertFalse(self.e.store.many("SELECT * FROM conversation_attachments"))

    def test_no_model_and_confirmation_caption_never_approve_attachment(self):
        c = Conversation(self.e, self.model())
        c.handle("断面を比較したい")
        self.assertIn("採用は次", c.handle("確認", [self.path]))
        self.assertFalse(self.e.store.many("SELECT * FROM cases"))
        c.llm = None
        self.assertIn("自動解釈は無効", c.handle("画像", [self.path]))
        self.assertEqual(len(self.e.store.many("SELECT * FROM conversation_attachments")), 2)

    def test_cli_accepts_quoted_path_and_continues_after_bad_image(self):
        inputs = iter([f'/image "{self.path}"', "断面を比較したい", "確認",
                       "/image /missing/file.png", "追加画像", "/status", "/exit"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)), contextlib.redirect_stdout(io.StringIO()) as out:
            chat(self.e, self.model())
        self.assertIn("画像の解釈", out.getvalue())
        self.assertIn("読み込めません", out.getvalue())
        self.assertEqual(self.e.store.one("SELECT data FROM conversation_attachments")["data"], PNG)

    def test_migration_is_additive_and_idempotent_for_v03_database(self):
        c = Conversation(self.e, self.model())
        c.handle("断面を比較したい")
        turns = self.e.store.many("SELECT * FROM conversation_turns")
        pending = c.pending
        self.e.store.conn.execute("DROP TABLE conversation_attachments")
        self.e.store.conn.execute("DELETE FROM schema_migrations WHERE version=4")
        self.e.store.conn.commit()
        self.e.close()
        for _ in range(2):
            with Engine(self.db) as engine:
                self.assertEqual(engine.store.many("SELECT * FROM conversation_turns"), turns)
                self.assertEqual(Conversation(engine, session_id=c.id).pending, pending)
                self.assertEqual(engine.store.one("SELECT max(version) v FROM schema_migrations")["v"], 4)
                self.assertEqual(engine.store.conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_case_switch_and_goal_edit_retain_only_new_case_images(self):
        model = self.model()
        c = Conversation(self.e, model)
        c.handle("断面を比較したい", [self.path]); c.handle("確認")
        original_id = c.attachments()[0]["id"]
        original_case = c.session["case_id"]
        model.interpretations["別の案件を比較したい"] = {
            "intent": "new_case", "payload": {"goal": "別案件の比較"}}
        c.handle("別の案件を比較したい", [self.path])
        image_id = c.pending["payload"]["image_ids"][0]
        self.assertNotEqual(image_id, original_id)
        # Editing a new case's goal must preserve its attached image.
        c.handle("別の案件を比較したい")
        self.assertEqual(c.pending["payload"]["image_ids"], [image_id])
        c.handle("確認")
        self.assertEqual([a["id"] for a in c.attachments()], [image_id])
        self.assertEqual(self.e.store.one("SELECT case_id FROM conversation_attachments WHERE id=?", (original_id,))["case_id"], original_case)

    def test_four_image_request_window_retains_all_bytes_without_case_leak(self):
        c = Conversation(self.e, self.model())
        for _ in range(5):
            c.handle("断面を比較したい", [self.path])
        self.assertEqual(len(c.attachments()), 4)
        c.handle("確認")
        original_case = c.session["case_id"]
        rows = self.e.store.many("SELECT case_id,data FROM conversation_attachments")
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["data"] == PNG and r["case_id"] == original_case for r in rows))
        c.handle("断面を比較したい"); c.handle("確認")
        self.assertFalse(c.attachments())

    def test_local_client_uses_persisted_images_on_text_followup(self):
        c = Conversation(self.e, LocalChatExtractor("http://llm.internal/v1", "vision"))
        opener = MagicMock()
        responses = [self.model().interpretations["断面を比較したい"],
                     self.model().interpretations["観察を残して"]]
        opener.open.return_value.__enter__.return_value.read.side_effect = [json.dumps(
            {"choices": [{"message": {"content": json.dumps(r, ensure_ascii=False)}}]}).encode() for r in responses]
        with patch("urllib.request.build_opener", return_value=opener):
            c.handle("断面を比較したい", [self.path]); c.handle("確認")
            self.path.unlink()
            c.handle("観察を残して"); c.handle("確認")
        self.assertEqual(opener.open.call_count, 2)
        for call in opener.open.call_args_list:
            payload = json.loads(call.args[0].data)
            self.assertNotIn("temperature", payload)
            images = [p for p in payload["messages"][1]["content"] if p["type"] == "image_url"]
            self.assertEqual(base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]), PNG)
        self.assertIn("側壁", c._summary(c.context()["observations"]))
