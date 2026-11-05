import json
import unittest
from unittest.mock import MagicMock, patch

from learning_engine.demo import PROCEDURE
from learning_engine.llm import LocalChatExtractor


class EndpointTests(unittest.TestCase):
    def test_explicit_lan_hostname_and_ipv6_endpoints(self):
        for url in ("http://192.168.1.10:11434/v1", "http://llm.internal:1234/v1",
                    "https://configured.example/v1", "http://[fd00::1]:8080/v1"):
            with self.subTest(url=url):
                client = LocalChatExtractor(url, "test-model")
                opener = MagicMock()
                opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
                    {"choices": [{"message": {"content": json.dumps(PROCEDURE)}}]}).encode()
                with patch("urllib.request.build_opener", return_value=opener):
                    self.assertEqual(client.extract("架空の作業報告")["goal"], PROCEDURE["goal"])
                self.assertEqual(opener.open.call_args.args[0].full_url, url + "/chat/completions")

    def test_invalid_urls_are_rejected(self):
        for url in ("", "file:///config", "ftp://server/v1", "http:///v1", "http://host:abc/v1",
                    "http://host:0/v1", "http://host:99999/v1", "http://user:secret@host/v1",
                    "http://host/v1?key=secret", "http://host/v1#fragment", "http://bad host/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalChatExtractor(url, "test-model")
