"""Unit tests for utils (shared state I/O + Telegram sender)."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests

import utils


class LoadStateTests(unittest.TestCase):
    def test_returns_empty_dict_when_file_missing(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(utils.load_state(missing), {})

    def test_reads_existing_json(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"a": 1, "b": "два"}), encoding="utf-8")
            self.assertEqual(utils.load_state(path), {"a": 1, "b": "два"})

    def test_uses_default_state_file_when_none(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "default.json"
            path.write_text('{"x": 42}')
            with patch.object(utils, "DEFAULT_STATE_FILE", path):
                self.assertEqual(utils.load_state(), {"x": 42})


class SaveStateTests(unittest.TestCase):
    def test_writes_utf8_pretty_json(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            utils.save_state({"total": 100.5, "note": "Займ"}, path)
            raw = path.read_text(encoding="utf-8")
            # non-ASCII preserved (ensure_ascii=False)
            self.assertIn("Займ", raw)
            # indent=2 → multiple lines
            self.assertGreater(raw.count("\n"), 1)
            self.assertEqual(json.loads(raw), {"total": 100.5, "note": "Займ"})

    def test_roundtrip_through_load(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            data = {"list": [1, 2, 3], "nested": {"k": "v"}}
            utils.save_state(data, path)
            self.assertEqual(utils.load_state(path), data)

    def test_overwrites_existing_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            utils.save_state({"a": 1}, path)
            utils.save_state({"b": 2}, path)
            self.assertEqual(utils.load_state(path), {"b": 2})


class SendTelegramAlertTests(unittest.TestCase):
    def _ok_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"ok": True, "result": {}}
        return resp

    def _err_response(self, description="bad", status=400):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = {"ok": False, "description": description}
        return resp

    def test_returns_false_when_token_missing(self):
        with patch("utils.requests.post") as mock_post:
            self.assertFalse(utils.send_telegram_alert("hi", "", "123"))
            mock_post.assert_not_called()

    def test_returns_false_when_chat_id_missing(self):
        with patch("utils.requests.post") as mock_post:
            self.assertFalse(utils.send_telegram_alert("hi", "token", ""))
            mock_post.assert_not_called()

    def test_success_returns_true_and_sends_expected_payload(self):
        with patch("utils.requests.post", return_value=self._ok_response()) as mock_post:
            ok = utils.send_telegram_alert("hello", "TKN", "42")
        self.assertTrue(ok)
        mock_post.assert_called_once()
        kwargs = mock_post.call_args.kwargs
        self.assertIn("api.telegram.org/botTKN/sendMessage", mock_post.call_args.args[0])
        self.assertEqual(kwargs["json"]["chat_id"], "42")
        self.assertEqual(kwargs["json"]["text"], "hello")
        self.assertEqual(kwargs["json"]["parse_mode"], "HTML")
        self.assertEqual(kwargs["timeout"], 10)

    def test_custom_parse_mode(self):
        with patch("utils.requests.post", return_value=self._ok_response()) as mock_post:
            utils.send_telegram_alert("m", "t", "c", parse_mode="MarkdownV2")
        self.assertEqual(mock_post.call_args.kwargs["json"]["parse_mode"], "MarkdownV2")

    def test_retries_on_http_error_then_succeeds(self):
        responses = [self._err_response("rate limit"), self._ok_response()]
        with patch("utils.requests.post", side_effect=responses), \
             patch("utils.time.sleep") as mock_sleep:
            ok = utils.send_telegram_alert("m", "t", "c", max_retries=2)
        self.assertTrue(ok)
        mock_sleep.assert_called_once()  # slept once before second attempt

    def test_returns_false_after_all_retries_fail(self):
        with patch("utils.requests.post",
                   side_effect=[self._err_response()] * 3) as mock_post, \
             patch("utils.time.sleep"):
            ok = utils.send_telegram_alert("m", "t", "c", max_retries=3)
        self.assertFalse(ok)
        self.assertEqual(mock_post.call_count, 3)

    def test_retries_on_network_exception(self):
        ok_resp = self._ok_response()
        with patch("utils.requests.post",
                   side_effect=[requests.ConnectionError("boom"), ok_resp]), \
             patch("utils.time.sleep"):
            ok = utils.send_telegram_alert("m", "t", "c", max_retries=2)
        self.assertTrue(ok)

    def test_returns_false_when_all_attempts_raise(self):
        with patch("utils.requests.post",
                   side_effect=requests.Timeout("slow")), \
             patch("utils.time.sleep"):
            self.assertFalse(utils.send_telegram_alert("m", "t", "c", max_retries=3))


class SendTelegramPhotoTests(unittest.TestCase):
    def _ok(self):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"ok": True}
        return r

    def _err(self, description="bad"):
        r = MagicMock()
        r.status_code = 400
        r.json.return_value = {"ok": False, "description": description}
        return r

    def test_no_creds_skips_request(self):
        with patch("utils.requests.post") as mock_post:
            self.assertFalse(utils.send_telegram_photo(b"PNG", "", "1"))
            self.assertFalse(utils.send_telegram_photo(b"PNG", "t", ""))
            mock_post.assert_not_called()

    def test_success_posts_multipart(self):
        with patch("utils.requests.post", return_value=self._ok()) as mock_post:
            ok = utils.send_telegram_photo(b"PNGDATA", "TKN", "42",
                                           caption="hi", filename="x.png")
        self.assertTrue(ok)
        url = mock_post.call_args.args[0]
        self.assertIn("/sendPhoto", url)
        self.assertIn("botTKN", url)
        kwargs = mock_post.call_args.kwargs
        self.assertIn("files", kwargs)
        self.assertEqual(kwargs["files"]["photo"][0], "x.png")
        self.assertEqual(kwargs["files"]["photo"][1], b"PNGDATA")
        self.assertEqual(kwargs["data"]["chat_id"], "42")
        self.assertEqual(kwargs["data"]["caption"], "hi")

    def test_no_caption_omits_caption_fields(self):
        with patch("utils.requests.post", return_value=self._ok()) as mock_post:
            utils.send_telegram_photo(b"X", "t", "c")
        data = mock_post.call_args.kwargs["data"]
        self.assertNotIn("caption", data)
        self.assertNotIn("parse_mode", data)

    def test_retries_on_error_then_success(self):
        with patch("utils.requests.post",
                   side_effect=[self._err(), self._ok()]), \
             patch("utils.time.sleep"):
            ok = utils.send_telegram_photo(b"X", "t", "c", max_retries=2)
        self.assertTrue(ok)

    def test_returns_false_after_all_retries(self):
        with patch("utils.requests.post", side_effect=[self._err()] * 3), \
             patch("utils.time.sleep"):
            self.assertFalse(
                utils.send_telegram_photo(b"X", "t", "c", max_retries=3))

    def test_network_exception_retried(self):
        with patch("utils.requests.post",
                   side_effect=[requests.ConnectionError("x"), self._ok()]), \
             patch("utils.time.sleep"):
            self.assertTrue(
                utils.send_telegram_photo(b"X", "t", "c", max_retries=2))


class SendTelegramDocumentTests(unittest.TestCase):
    def _ok(self):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"ok": True}
        return r

    def test_no_creds_skips(self):
        with patch("utils.requests.post") as mock_post:
            self.assertFalse(utils.send_telegram_document(b"X", "", "1"))
            mock_post.assert_not_called()

    def test_success(self):
        with patch("utils.requests.post", return_value=self._ok()) as mock_post:
            ok = utils.send_telegram_document(
                b"DATA", "TKN", "99", caption="cap", filename="state.json")
        self.assertTrue(ok)
        url = mock_post.call_args.args[0]
        self.assertIn("/sendDocument", url)
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs["files"]["document"][0], "state.json")
        self.assertEqual(kwargs["files"]["document"][1], b"DATA")
        self.assertEqual(kwargs["data"]["caption"], "cap")

    def test_network_failure_returns_false(self):
        with patch("utils.requests.post",
                   side_effect=requests.Timeout("t")), \
             patch("utils.time.sleep"):
            self.assertFalse(
                utils.send_telegram_document(b"X", "t", "c", max_retries=2))


if __name__ == "__main__":
    unittest.main()
