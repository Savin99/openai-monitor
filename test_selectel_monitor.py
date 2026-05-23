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
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests
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


class SelectelApiTests(unittest.TestCase):
    def setUp(self):
        self._orig_config = dict(selectel_monitor.CONFIG)
        selectel_monitor.CONFIG.update(
            {
                "api_token": "token",
                "api_base_url": "https://api.selectel.ru",
                "balance_threshold": 1000.0,
                "amount_scale": 100.0,
                "telegram_bot_token": "tg",
                "telegram_chat_id": "42",
                "alert_timezone": "Europe/Moscow",
                "topup_lookup_days": 365,
            }
        )

    def tearDown(self):
        selectel_monitor.CONFIG.clear()
        selectel_monitor.CONFIG.update(self._orig_config)

    def _response(self, status=200, payload=None, text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload or {}
        resp.text = text or json.dumps(payload or {})
        return resp

    def test_selectel_get_uses_x_token(self):
        with patch(
            "selectel_monitor.requests.get",
            return_value=self._response(payload={"status": "success"}),
        ) as mock_get:
            out = selectel_monitor._selectel_get("/v3/balances")
        self.assertEqual(out["status"], "success")
        self.assertEqual(mock_get.call_args.kwargs["headers"]["X-Token"], "token")
        self.assertEqual(
            mock_get.call_args.args[0], "https://api.selectel.ru/v3/balances"
        )

    def test_selectel_get_raises_on_http_error(self):
        with patch(
            "selectel_monitor.requests.get",
            return_value=self._response(status=401, text="bad token"),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                selectel_monitor._selectel_get("/v3/balances")

    def test_selectel_get_raises_on_network_error(self):
        with patch(
            "selectel_monitor.requests.get",
            side_effect=requests.Timeout("slow"),
        ):
            with self.assertRaisesRegex(RuntimeError, "network"):
                selectel_monitor._selectel_get("/v3/balances")

    def test_summarize_balances(self):
        payload = {
            "data": {
                "settings": {"currency": "RUB"},
                "debt_status": "clean",
                "billings": [
                    {
                        "billing_type": "primary",
                        "balances_values_sum": 150000,
                        "debt_sum": 10000,
                        "final_sum": 140000,
                        "balances": [
                            {"balance_type": "main", "value": 120000},
                            {"balance_type": "bonus", "value": 30000},
                        ],
                    }
                ],
            }
        }
        summary = selectel_monitor.summarize_balances(payload)
        self.assertEqual(summary["currency"], "RUB")
        self.assertEqual(summary["total_final"], 1400)
        self.assertEqual(summary["total_debt"], 100)
        self.assertEqual(summary["billings"][0]["balances"][0]["type"], "main")

    def test_format_api_status_is_human_readable(self):
        summary = {
            "currency": "RUB",
            "debt_status": "Success",
            "total_balances": 112851.66,
            "total_debt": 0,
            "total_final": 112851.66,
            "billings": [
                {
                    "type": "primary",
                    "final_sum": 112851.66,
                    "balances": [
                        {"type": "main", "value": 112851.66},
                        {"type": "bonus", "value": 0},
                    ],
                }
            ],
        }
        msg = selectel_monitor.format_api_status(
            summary, {"data": {"primary": 286, "storage": None}}
        )
        self.assertIn("Selectel — баланс", msg)
        self.assertIn("Сейчас: <b>112 851.66 RUB</b>", msg)
        self.assertIn("Порог: 1 000.00 RUB", msg)
        self.assertIn("Задолженность: нет задолженности", msg)
        self.assertIn("Хватит примерно", msg)
        self.assertIn("Основной баланс", msg)
        self.assertNotIn("Debt status", msg)
        self.assertNotIn("None", msg)
        self.assertNotIn("bonus 0.00", msg)

    def test_summarize_transactions_reconstructs_history(self):
        payload = {
            "data": [
                {
                    "created": "2026-05-14T10:00:00",
                    "price": 1000000,
                    "public_description": {"ru": "Пополнение баланса"},
                    "dir": "incoming",
                },
                {
                    "created": "2026-05-15T10:00:00",
                    "price": -250000,
                    "public_description": {"ru": "Оплата услуг"},
                    "dir": "outgoing",
                },
            ]
        }
        out = selectel_monitor.summarize_transactions(
            payload,
            11250,
            "RUB",
            now=datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out["incoming"], 10000)
        self.assertEqual(out["outgoing"], 2500)
        self.assertEqual(out["outgoing_yesterday"], 2500)
        self.assertEqual(out["outgoing_week"], 2500)
        self.assertEqual(out["last_payment"]["date"], "14.05.2026")
        self.assertEqual(out["last_payment"]["days_since"], 2)
        self.assertEqual(len(out["payments"]), 1)
        self.assertEqual(out["history"][-1]["balance"], 11250)
        self.assertEqual(out["history"][0]["balance"], 3750)

    def test_format_api_report_includes_transactions(self):
        summary = {
            "currency": "RUB",
            "debt_status": "Success",
            "total_balances": 112851.66,
            "total_debt": 0,
            "total_final": 112851.66,
            "billings": [],
        }
        tx = {
            "incoming": 10000,
            "outgoing": 2500,
            "outgoing_yesterday": 500,
            "outgoing_week": 2000,
            "last_payment": {
                "date": "14.05.2026",
                "amount": 10000,
                "days_since": 2,
            },
            "payments": [
                {"ts": "2026-05-14T10:00:00+00:00", "amount": 10000}
            ],
        }
        msg = selectel_monitor.format_api_report(summary, transactions=tx)
        self.assertIn("Вчера: 500.00 RUB", msg)
        self.assertIn("За 7 дней: 2 000.00 RUB", msg)
        self.assertIn("Последнее пополнение: 14.05.2026", msg)
        self.assertIn("+10 000.00 RUB (2 дн. назад)", msg)
        self.assertIn("За 90 дней", msg)
        self.assertIn("Списано: 2 500.00 RUB", msg)
        self.assertNotIn("Пополнено:", msg)

    def test_check_api_balance_sends_only_when_low_or_forced(self):
        high = {
            "data": {
                "settings": {"currency": "RUB"},
                "billings": [{"billing_type": "primary", "final_sum": 150000}],
            }
        }
        with (
            patch("selectel_monitor.fetch_selectel_balances", return_value=high),
            patch("selectel_monitor.fetch_selectel_prediction", return_value={}),
            patch(
                "selectel_monitor.fetch_selectel_transactions",
                return_value={"data": []},
            ),
            patch("selectel_monitor.record_api_history", return_value=[]),
            patch("selectel_monitor.send_api_report", return_value=True) as send,
        ):
            selectel_monitor.check_api_balance()
            send.assert_not_called()
            selectel_monitor.check_api_balance(send_ok_status=True)
            send.assert_called_once()

    def test_check_api_balance_loads_last_topup_when_outside_report_window(self):
        high = {
            "data": {
                "settings": {"currency": "RUB"},
                "billings": [{"billing_type": "primary", "final_sum": 150000}],
            }
        }
        recent_transactions = {
            "data": [
                {
                    "created": "2026-05-15T10:00:00",
                    "price": -250000,
                    "public_description": {"ru": "Оплата услуг"},
                    "dir": "outgoing",
                },
            ]
        }
        long_transactions = {
            "data": [
                {
                    "created": "2026-04-01T10:00:00",
                    "price": 1000000,
                    "public_description": {"ru": "Пополнение баланса"},
                    "dir": "incoming",
                },
            ]
        }
        with (
            patch("selectel_monitor.fetch_selectel_balances", return_value=high),
            patch("selectel_monitor.fetch_selectel_prediction", return_value={}),
            patch(
                "selectel_monitor.fetch_selectel_transactions",
                side_effect=[recent_transactions, long_transactions],
            ) as fetch_transactions,
            patch("selectel_monitor.record_api_history", return_value=[]),
            patch("selectel_monitor.send_api_report", return_value=True) as send,
        ):
            selectel_monitor.check_api_balance(send_ok_status=True)

        self.assertEqual(fetch_transactions.call_count, 2)
        self.assertEqual(
            fetch_transactions.call_args_list[1].kwargs["days"],
            selectel_monitor.CONFIG["topup_lookup_days"],
        )
        sent_transactions = send.call_args.args[2]
        self.assertEqual(sent_transactions["last_payment"]["amount"], 10000)

    def test_check_api_balance_alerts_when_low(self):
        low = {
            "data": {
                "settings": {"currency": "RUB"},
                "billings": [{"billing_type": "primary", "final_sum": 90000}],
            }
        }
        with (
            patch("selectel_monitor.fetch_selectel_balances", return_value=low),
            patch(
                "selectel_monitor.fetch_selectel_prediction",
                return_value={"data": {"primary": 48}},
            ),
            patch(
                "selectel_monitor.fetch_selectel_transactions",
                return_value={"data": []},
            ),
            patch("selectel_monitor.record_api_history", return_value=[]),
            patch("selectel_monitor._today_in_alert_timezone", return_value="2026-05-20"),
            patch("selectel_monitor.load_state", return_value={}),
            patch("selectel_monitor.save_state"),
            patch("selectel_monitor.send_api_report", return_value=True) as send,
        ):
            selectel_monitor.check_api_balance()
        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs["force_alert"])

    def test_check_api_balance_sends_low_alert_once_per_day(self):
        low = {
            "data": {
                "settings": {"currency": "RUB"},
                "billings": [{"billing_type": "primary", "final_sum": 90000}],
            }
        }
        state = {}

        with (
            patch("selectel_monitor.fetch_selectel_balances", return_value=low),
            patch("selectel_monitor.fetch_selectel_prediction", return_value={}),
            patch(
                "selectel_monitor.fetch_selectel_transactions",
                return_value={"data": []},
            ),
            patch("selectel_monitor.record_api_history", return_value=[]),
            patch("selectel_monitor._today_in_alert_timezone", return_value="2026-05-20"),
            patch("selectel_monitor.load_state", side_effect=lambda: state),
            patch("selectel_monitor.save_state"),
            patch("selectel_monitor.send_api_report", return_value=True) as send,
        ):
            selectel_monitor.check_api_balance()
            selectel_monitor.check_api_balance()

        send.assert_called_once()


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
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64url("<p>html version</p>")},
                },
                {"mimeType": "text/plain", "body": {"data": _b64url("plain version")}},
            ],
        }
        self.assertEqual(selectel_monitor._extract_body(payload), "plain version")

    def test_text_html_fallback_strips_tags(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64url("<p>Hello <b>world</b></p>")},
                },
            ],
        }
        result = selectel_monitor._extract_body(payload)
        self.assertEqual(result, "Hello world")

    def test_text_html_drops_style_and_script_contents(self):
        # Регрессия: раньше strip-tags убирал только теги, а CSS/JS внутри
        # <style>/<script> протекал в превью Telegram (Selectel-письма).
        raw = (
            "<html><head>"
            "<style type='text/css'>a {text-decoration: none;} "
            "#mshidden { display: none; }</style>"
            "<script>var x = 1;</script>"
            "</head><body><p>Пора пополнить баланс</p></body></html>"
        )
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64url(raw)},
        }
        result = selectel_monitor._extract_body(payload)
        self.assertEqual(result, "Пора пополнить баланс")
        self.assertNotIn("text-decoration", result)
        self.assertNotIn("var x", result)

    def test_text_html_drops_comments_including_mso_conditionals(self):
        raw = (
            "<!--[if mso]><style>table {border:0;}</style><![endif]-->"
            "<p>Важный текст</p><!-- tracker -->"
        )
        payload = {"mimeType": "text/html", "body": {"data": _b64url(raw)}}
        self.assertEqual(selectel_monitor._extract_body(payload), "Важный текст")

    def test_empty_payload(self):
        self.assertEqual(selectel_monitor._extract_body({}), "")
        self.assertEqual(selectel_monitor._extract_body(None), "")

    def test_nested_multipart(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _b64url("nested plain")},
                        },
                    ],
                },
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
        selectel_monitor.CONFIG.update(
            {
                "telegram_bot_token": "test_token",
                "telegram_chat_id": "12345",
                "sender_filter": "no-reply@selectel.ru",
                "lookback": "2d",
                "body_preview_len": 500,
                "service_label": "Selectel",
                "max_processed_ids": 200,
                # Tests run without an image by default — flip per-test if needed.
                "image_path": str(Path(self._tmp.name) / "no-such-image.png"),
                "instance": "selectel",
            }
        )

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
        gmail = _build_fake_gmail(
            {
                "id-A": self._make_msg("Subj A", "body A"),
                "id-B": self._make_msg("Subj B", "body B"),
            }
        )
        # Pre-populate state with id-A → only id-B should be forwarded.
        self._tmp_path.write_text(
            json.dumps({"selectel": {"processed_message_ids": ["id-A"]}})
        )

        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(
                selectel_monitor, "send_telegram_alert", return_value=True
            ) as send,
        ):
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
        gmail = _build_fake_gmail(
            {
                "id-A": self._make_msg("Subj A", "body A"),
            }
        )
        self._tmp_path.write_text(
            json.dumps({"selectel": {"processed_message_ids": ["id-A"]}})
        )

        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(
                selectel_monitor, "send_telegram_alert", return_value=True
            ) as send,
        ):
            selectel_monitor.forward_new()

        send.assert_not_called()

    def test_telegram_fail_exits_without_state_save(self):
        gmail = _build_fake_gmail(
            {
                "id-X": self._make_msg("Subj X", "body X"),
            }
        )
        # No prior state.

        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(selectel_monitor, "send_telegram_alert", return_value=False),
        ):
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

        gmail = _build_fake_gmail(
            {
                "id-Z": self._make_msg("Subj Z", "body Z"),
            }
        )

        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(
                selectel_monitor, "send_telegram_photo", return_value=True
            ) as photo,
            patch.object(
                selectel_monitor, "send_telegram_alert", return_value=True
            ) as text,
        ):
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

        gmail = _build_fake_gmail(
            {
                "id-Y": self._make_msg("Subj Y", "body Y"),
            }
        )

        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(
                selectel_monitor, "send_telegram_photo", return_value=False
            ) as photo,
            patch.object(
                selectel_monitor, "send_telegram_alert", return_value=True
            ) as text,
        ):
            selectel_monitor.forward_new()

        photo.assert_called_once()
        text.assert_called_once()

    def test_instance_isolates_state_bucket(self):
        """Two instances (selectel, vdska) should not share processed_ids."""
        # Pre-populate state with vdska bucket → selectel run shouldn't see those.
        self._tmp_path.write_text(
            json.dumps(
                {
                    "vdska": {"processed_message_ids": ["id-A", "id-B"]},
                }
            )
        )
        gmail = _build_fake_gmail(
            {
                "id-A": self._make_msg("Subj A", "body A"),
            }
        )

        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(
                selectel_monitor, "send_telegram_alert", return_value=True
            ) as send,
        ):
            selectel_monitor.forward_new()

        # selectel saw it as new because its bucket was empty.
        send.assert_called_once()
        state = json.loads(self._tmp_path.read_text())
        self.assertEqual(
            state["selectel"]["processed_message_ids"],
            ["id-A"],
        )
        # vdska bucket untouched.
        self.assertEqual(
            state["vdska"]["processed_message_ids"],
            ["id-A", "id-B"],
        )

    def test_processed_ids_fifo_trimmed(self):
        selectel_monitor.CONFIG["max_processed_ids"] = 3
        # 5 messages, no prior state — list returns oldest-last (Gmail order),
        # forward_new processes oldest-first, so final state keeps last 3 ids
        # in chronological order.
        msgs = {f"id-{i}": self._make_msg(f"S{i}", f"b{i}") for i in range(5)}
        gmail = _build_fake_gmail(msgs)

        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(selectel_monitor, "send_telegram_alert", return_value=True),
        ):
            selectel_monitor.forward_new()

        state = json.loads(self._tmp_path.read_text())
        ids = state["selectel"]["processed_message_ids"]
        self.assertEqual(len(ids), 3)
        # Newest 3 (in processing order = reversed list order).
        self.assertEqual(ids, ["id-2", "id-1", "id-0"])


class LoadImageBytesTests(unittest.TestCase):
    def test_returns_none_when_path_empty(self):
        self.assertIsNone(selectel_monitor._load_image_bytes(""))
        self.assertIsNone(selectel_monitor._load_image_bytes(None))

    def test_returns_none_when_file_missing(self):
        self.assertIsNone(selectel_monitor._load_image_bytes("/no/such/file.png"))

    def test_returns_bytes_when_file_exists(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "img.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            data = selectel_monitor._load_image_bytes(str(p))
            self.assertIsInstance(data, bytes)
            self.assertTrue(data.startswith(b"\x89PNG"))


class SendStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name) / "monitor_state.json"
        self._patch_state = patch("utils.DEFAULT_STATE_FILE", self._tmp_path)
        self._patch_state.start()

        self._orig_config = dict(selectel_monitor.CONFIG)
        selectel_monitor.CONFIG.update(
            {
                "telegram_bot_token": "test_token",
                "telegram_chat_id": "12345",
                "sender_filter": "no-reply@selectel.ru",
                "lookback": "2d",
                "service_label": "Selectel",
                "instance": "selectel",
            }
        )

    def tearDown(self):
        self._patch_state.stop()
        selectel_monitor.CONFIG.clear()
        selectel_monitor.CONFIG.update(self._orig_config)
        self._tmp.cleanup()

    def test_reports_counts_and_uses_instance_bucket(self):
        gmail = _build_fake_gmail(
            {
                "id-1": {
                    "payload": {"headers": [], "mimeType": "text/plain", "body": {}}
                },
                "id-2": {
                    "payload": {"headers": [], "mimeType": "text/plain", "body": {}}
                },
            }
        )
        # Mark id-1 as already processed → new_count=1.
        self._tmp_path.write_text(
            json.dumps({"selectel": {"processed_message_ids": ["id-1"]}})
        )

        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(
                selectel_monitor, "send_telegram_alert", return_value=True
            ) as send,
        ):
            selectel_monitor.send_status()

        send.assert_called_once()
        msg = send.call_args[0][0]
        self.assertIn("Найдено писем: 2", msg)
        self.assertIn("Из них новых: 1", msg)

    def test_telegram_fail_exits(self):
        gmail = _build_fake_gmail({})
        with (
            patch.object(
                selectel_monitor, "_load_credentials", return_value=MagicMock()
            ),
            patch.object(selectel_monitor, "_build_gmail_service", return_value=gmail),
            patch.object(selectel_monitor, "send_telegram_alert", return_value=False),
        ):
            with self.assertRaises(SystemExit) as cm:
                selectel_monitor.send_status()
            self.assertEqual(cm.exception.code, 1)


class MainDispatchTests(unittest.TestCase):
    """main() routes to forward_new/send_status and writes instance-named heartbeat."""

    def setUp(self):
        self._orig_config = dict(selectel_monitor.CONFIG)
        selectel_monitor.CONFIG["instance"] = "vdska"

    def tearDown(self):
        selectel_monitor.CONFIG.clear()
        selectel_monitor.CONFIG.update(self._orig_config)

    def test_default_calls_forward_new_then_heartbeat(self):
        with (
            patch.object(selectel_monitor, "forward_new") as fn,
            patch.object(selectel_monitor, "send_status") as ss,
            patch.object(selectel_monitor, "touch_heartbeat") as hb,
            patch.object(selectel_monitor.sys, "argv", ["selectel_monitor.py"]),
        ):
            selectel_monitor.main()
        fn.assert_called_once()
        ss.assert_not_called()
        hb.assert_called_once_with("vdska-monitor-check")

    def test_status_flag_calls_send_status(self):
        with (
            patch.object(selectel_monitor, "forward_new") as fn,
            patch.object(selectel_monitor, "send_status") as ss,
            patch.object(selectel_monitor, "touch_heartbeat") as hb,
            patch.object(
                selectel_monitor.sys, "argv", ["selectel_monitor.py", "--status"]
            ),
        ):
            selectel_monitor.main()
        ss.assert_called_once()
        fn.assert_not_called()
        hb.assert_called_once_with("vdska-monitor-check")

    def test_heartbeat_skipped_on_systemexit(self):
        """If forward_new raises SystemExit (Telegram fail), heartbeat is NOT written."""
        with (
            patch.object(selectel_monitor, "forward_new", side_effect=SystemExit(1)),
            patch.object(selectel_monitor, "touch_heartbeat") as hb,
            patch.object(selectel_monitor.sys, "argv", ["selectel_monitor.py"]),
        ):
            with self.assertRaises(SystemExit):
                selectel_monitor.main()
        hb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
