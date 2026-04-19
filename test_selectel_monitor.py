"""Unit tests for selectel_monitor.

Покрывает:
- format_for_telegram: HTML-escape, обрезка body по preview_len, дефолт subject.
- _extract_body: text/plain prefer, text/html fallback, multipart, пустой payload.
- forward_new: дедуп по message-id, FIFO-обрезка processed_ids, sys.exit(1)
  при Telegram fail (state НЕ сохраняется).
"""

import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import selectel_monitor


def _b64url(s):
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


class FormatForTelegramTests(unittest.TestCase):
    def test_basic_format(self):
        msg = {"subject": "Пора пополнить баланс", "body": "Аккаунт 419013."}
        out = selectel_monitor.format_for_telegram(msg, "Selectel", 500)
        self.assertIn("📩 Selectel", out)
        self.assertIn("<b>Пора пополнить баланс</b>", out)
        self.assertIn("Аккаунт 419013.", out)

    def test_html_escaped(self):
        msg = {"subject": "Alert <script>", "body": "x & y > z"}
        out = selectel_monitor.format_for_telegram(msg, "Selectel", 500)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("x &amp; y &gt; z", out)
        self.assertNotIn("<script>", out)

    def test_body_truncated_with_ellipsis(self):
        msg = {"subject": "S", "body": "a" * 1000}
        out = selectel_monitor.format_for_telegram(msg, "Selectel", 100)
        # body section should contain 100 'a's then ellipsis
        self.assertIn("a" * 100 + "…", out)
        self.assertNotIn("a" * 101, out)

    def test_missing_subject_default(self):
        msg = {"body": "x"}
        out = selectel_monitor.format_for_telegram(msg, "Selectel", 500)
        self.assertIn("<b>(без темы)</b>", out)

    def test_empty_body_does_not_crash(self):
        msg = {"subject": "S", "body": ""}
        out = selectel_monitor.format_for_telegram(msg, "Selectel", 500)
        self.assertIn("<b>S</b>", out)


class ExtractBodyTests(unittest.TestCase):
    def test_single_part_text_plain(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _b64url("hello world")},
        }
        self.assertEqual(selectel_monitor._extract_body(payload), "hello world")

    def test_multipart_prefers_text_plain(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html",
                 "body": {"data": _b64url("<p>html version</p>")}},
                {"mimeType": "text/plain",
                 "body": {"data": _b64url("plain version")}},
            ],
        }
        self.assertEqual(selectel_monitor._extract_body(payload), "plain version")

    def test_text_html_fallback_strips_tags(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html",
                 "body": {"data": _b64url("<p>Hello <b>world</b></p>")}},
            ],
        }
        result = selectel_monitor._extract_body(payload)
        self.assertEqual(result, "Hello world")

    def test_empty_payload(self):
        self.assertEqual(selectel_monitor._extract_body({}), "")
        self.assertEqual(selectel_monitor._extract_body(None), "")

    def test_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "multipart/alternative",
                 "parts": [
                     {"mimeType": "text/plain",
                      "body": {"data": _b64url("nested plain")}},
                 ]},
            ],
        }
        self.assertEqual(selectel_monitor._extract_body(payload), "nested plain")


def _build_fake_gmail(messages_by_id):
    """Build a MagicMock Gmail service that returns the given messages."""
    service = MagicMock()
    list_resp = {"messages": [{"id": mid} for mid in messages_by_id.keys()]}
    service.users().messages().list().execute.return_value = list_resp

    def get_side_effect(userId=None, id=None, format=None):
        # Returns a chained mock with .execute() returning the message.
        executor = MagicMock()
        executor.execute.return_value = messages_by_id[id]
        return executor

    service.users().messages().get.side_effect = get_side_effect
    return service


class ForwardNewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name) / "monitor_state.json"
        self._patch_state = patch("utils.DEFAULT_STATE_FILE", self._tmp_path)
        self._patch_state.start()

        self._orig_config = dict(selectel_monitor.CONFIG)
        selectel_monitor.CONFIG.update({
            "telegram_bot_token": "test_token",
            "telegram_chat_id": "12345",
            "sender_filter": "no-reply@selectel.ru",
            "lookback": "2d",
            "body_preview_len": 500,
            "service_label": "Selectel",
            "max_processed_ids": 200,
            # Tests run without an image by default — flip per-test if needed.
            "image_path": str(Path(self._tmp.name) / "no-such-image.png"),
        })

    def tearDown(self):
        self._patch_state.stop()
        selectel_monitor.CONFIG.clear()
        selectel_monitor.CONFIG.update(self._orig_config)
        self._tmp.cleanup()

    def _make_msg(self, subject, body):
        return {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": "Selectel <no-reply@selectel.ru>"},
                    {"name": "Date", "value": "Sat, 18 Apr 2026 22:10:00 +0000"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64url(body)},
            }
        }

    def test_forwards_only_unseen(self):
        gmail = _build_fake_gmail({
            "id-A": self._make_msg("Subj A", "body A"),
            "id-B": self._make_msg("Subj B", "body B"),
        })
        # Pre-populate state with id-A → only id-B should be forwarded.
        self._tmp_path.write_text(json.dumps({
            "selectel": {"processed_message_ids": ["id-A"]}
        }))

        with patch.object(selectel_monitor, "_load_credentials", return_value=MagicMock()), \
             patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail), \
             patch.object(selectel_monitor, "send_telegram_alert", return_value=True) as send:
            selectel_monitor.forward_new()

        send.assert_called_once()
        sent_text = send.call_args[0][0]
        self.assertIn("Subj B", sent_text)
        self.assertNotIn("Subj A", sent_text)

        state = json.loads(self._tmp_path.read_text())
        self.assertEqual(
            state["selectel"]["processed_message_ids"],
            ["id-A", "id-B"],
        )

    def test_no_new_messages_no_send(self):
        gmail = _build_fake_gmail({
            "id-A": self._make_msg("Subj A", "body A"),
        })
        self._tmp_path.write_text(json.dumps({
            "selectel": {"processed_message_ids": ["id-A"]}
        }))

        with patch.object(selectel_monitor, "_load_credentials", return_value=MagicMock()), \
             patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail), \
             patch.object(selectel_monitor, "send_telegram_alert", return_value=True) as send:
            selectel_monitor.forward_new()

        send.assert_not_called()

    def test_telegram_fail_exits_without_state_save(self):
        gmail = _build_fake_gmail({
            "id-X": self._make_msg("Subj X", "body X"),
        })
        # No prior state.

        with patch.object(selectel_monitor, "_load_credentials", return_value=MagicMock()), \
             patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail), \
             patch.object(selectel_monitor, "send_telegram_alert", return_value=False):
            with self.assertRaises(SystemExit) as cm:
                selectel_monitor.forward_new()
            self.assertEqual(cm.exception.code, 1)

        # State file should not have been written with id-X.
        if self._tmp_path.exists():
            state = json.loads(self._tmp_path.read_text())
            self.assertNotIn(
                "id-X",
                state.get("selectel", {}).get("processed_message_ids", []),
            )

    def test_uses_photo_when_image_present(self):
        """If image file exists, send via sendPhoto; sendMessage not called."""
        img_path = Path(self._tmp.name) / "logo.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-image-data")
        selectel_monitor.CONFIG["image_path"] = str(img_path)

        gmail = _build_fake_gmail({
            "id-Z": self._make_msg("Subj Z", "body Z"),
        })

        with patch.object(selectel_monitor, "_load_credentials", return_value=MagicMock()), \
             patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail), \
             patch.object(selectel_monitor, "send_telegram_photo", return_value=True) as photo, \
             patch.object(selectel_monitor, "send_telegram_alert", return_value=True) as text:
            selectel_monitor.forward_new()

        photo.assert_called_once()
        text.assert_not_called()
        # Caption (positional arg 4 since send_telegram_photo is keyword-heavy
        # — check via kwargs).
        kwargs = photo.call_args.kwargs
        self.assertIn("Subj Z", kwargs["caption"])

    def test_falls_back_to_text_when_photo_fails(self):
        """If sendPhoto returns False, retry via sendMessage."""
        img_path = Path(self._tmp.name) / "logo.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        selectel_monitor.CONFIG["image_path"] = str(img_path)

        gmail = _build_fake_gmail({
            "id-Y": self._make_msg("Subj Y", "body Y"),
        })

        with patch.object(selectel_monitor, "_load_credentials", return_value=MagicMock()), \
             patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail), \
             patch.object(selectel_monitor, "send_telegram_photo", return_value=False) as photo, \
             patch.object(selectel_monitor, "send_telegram_alert", return_value=True) as text:
            selectel_monitor.forward_new()

        photo.assert_called_once()
        text.assert_called_once()

    def test_processed_ids_fifo_trimmed(self):
        selectel_monitor.CONFIG["max_processed_ids"] = 3
        # 5 messages, no prior state — list returns oldest-last (Gmail order),
        # forward_new processes oldest-first, so final state keeps last 3 ids
        # in chronological order.
        msgs = {f"id-{i}": self._make_msg(f"S{i}", f"b{i}") for i in range(5)}
        gmail = _build_fake_gmail(msgs)

        with patch.object(selectel_monitor, "_load_credentials", return_value=MagicMock()), \
             patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail), \
             patch.object(selectel_monitor, "send_telegram_alert", return_value=True):
            selectel_monitor.forward_new()

        state = json.loads(self._tmp_path.read_text())
        ids = state["selectel"]["processed_message_ids"]
        self.assertEqual(len(ids), 3)
        # Newest 3 (in processing order = reversed list order).
        self.assertEqual(ids, ["id-2", "id-1", "id-0"])


if __name__ == "__main__":
    unittest.main()
