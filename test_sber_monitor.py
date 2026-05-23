"""Unit tests for sber_monitor.

Покрывает:
- filter_rub_current_accounts: фильтрация счетов (валюта/тип/статус).
- check_and_alert: ежечасная логика — алертим если хотя бы на ОДНОМ из рублёвых
  р/с баланс > threshold, только в окне [hour_start, hour_end] alert_timezone,
  не дублируем в пределах одного часа, сбрасываем при падении всех под порог.
- refresh_access_token: обмен refresh_token → новая пара.
- check_refresh_token_expiry: предупреждение за 14 дней до истечения.
"""

import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests

import sber_monitor


class FilterRubCurrentAccountsTests(unittest.TestCase):
    def test_returns_only_rub_current_active(self):
        accounts = [
            # матч
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": "40802810000000001111",
                "balance": 150000,
            },
            # не-рублёвый
            {
                "currencyCode": "USD",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": "40802840000000002222",
                "balance": 1000,
            },
            # депозит
            {
                "currencyCode": "RUB",
                "accountType": "DEPOSIT",
                "state": "OPEN",
                "accountNumber": "42102810000000003333",
                "balance": 500000,
            },
            # закрытый
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "CLOSED",
                "accountNumber": "40802810000000004444",
                "balance": 999,
            },
            # ещё один активный р/с
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": "40802810000000005555",
                "balance": 50000.50,
            },
        ]
        result = sber_monitor.filter_rub_current_accounts(accounts)
        self.assertEqual(len(result), 2)
        balances = {r["balance"] for r in result}
        self.assertEqual(balances, {150000.0, 50000.50})

    def test_alternative_field_names(self):
        accounts = [
            {
                "currency": "RUB",
                "type": "CURRENT",
                "availableBalance": "75000.00",
                "number": "40802810000000001111",
            },
        ]
        result = sber_monitor.filter_rub_current_accounts(accounts)
        self.assertEqual(result[0]["balance"], 75000.0)
        self.assertEqual(result[0]["accountNumber"], "40802810000000001111")

    def test_currency_code_numeric(self):
        accounts = [
            {
                "currencyCode": "810",
                "accountType": "CURRENT",
                "balance": 10000,
                "accountNumber": "40802810000000001111",
            },
        ]
        result = sber_monitor.filter_rub_current_accounts(accounts)
        self.assertEqual(len(result), 1)

    def test_bad_balance_is_omitted(self):
        # bad balance не пропадает как счёт, но поле balance не ставится —
        # дальше enrich_with_balances его заполнит.
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "balance": "not-a-number",
                "accountNumber": "1",
            },
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "balance": 1000,
                "accountNumber": "2",
            },
        ]
        result = sber_monitor.filter_rub_current_accounts(accounts)
        self.assertEqual(len(result), 2)
        self.assertNotIn("balance", result[0])
        self.assertEqual(result[1]["balance"], 1000)

    def test_calculated_type_recognized(self):
        # Реальный ответ Sber API: type='calculated' (lowercase)
        accounts = [
            {
                "currencyCode": "810",
                "type": "calculated",
                "state": "OPEN",
                "accountNumber": "40802810438720037466",
                "balance": 100,
            },
            {
                "currencyCode": "810",
                "type": "deposit",
                "state": "OPEN",
                "accountNumber": "99999999999999999999",
                "balance": 99999,
            },
        ]
        result = sber_monitor.filter_rub_current_accounts(accounts)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["accountNumber"], "40802810438720037466")

    def test_empty_list(self):
        self.assertEqual(sber_monitor.filter_rub_current_accounts([]), [])

    def test_account_number_normalized(self):
        # Если Сбер вернёт номер с пробелами — в результате их быть не должно
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "accountNumber": "40802 810 5 3872 0065147",
                "balance": 1000,
            },
        ]
        result = sber_monitor.filter_rub_current_accounts(accounts)
        self.assertEqual(result[0]["accountNumber"], "40802810538720065147")


class AccountLabelTests(unittest.TestCase):
    def setUp(self):
        self._orig = dict(sber_monitor.CONFIG["account_labels"])
        sber_monitor.CONFIG["account_labels"] = {
            "40802810538720065147": "Займ Валамис",
            "40802810438720037466": "Основной платёжный",
        }

    def tearDown(self):
        sber_monitor.CONFIG["account_labels"] = self._orig

    def test_known_account_returns_label(self):
        self.assertEqual(
            sber_monitor.account_label("40802810538720065147"),
            "Займ Валамис",
        )

    def test_spaces_normalized(self):
        self.assertEqual(
            sber_monitor.account_label("40802 810 4 3872 0037466"),
            "Основной платёжный",
        )

    def test_unknown_account_returns_last4(self):
        self.assertEqual(sber_monitor.account_label("99999999999999991234"), "...1234")

    def test_empty_number(self):
        self.assertEqual(sber_monitor.account_label(None), "?")


class ParseLabelsTests(unittest.TestCase):
    def test_valid_json(self):
        raw = '{"40802 810 5 3872 0065147":"Займ Валамис"}'
        result = sber_monitor._parse_labels(raw)
        self.assertEqual(result, {"40802810538720065147": "Займ Валамис"})

    def test_invalid_json_returns_empty(self):
        self.assertEqual(sber_monitor._parse_labels("not json"), {})

    def test_empty_string(self):
        self.assertEqual(sber_monitor._parse_labels(""), {})


class CheckAndAlertTests(unittest.TestCase):
    """Тесты ежечасной логики алерта."""

    TZ = ZoneInfo("Europe/Moscow")

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name) / "monitor_state.json"
        self._patch_state_file = patch("utils.DEFAULT_STATE_FILE", self._tmp_path)
        self._patch_state_file.start()

        self._orig_config = dict(sber_monitor.CONFIG)
        sber_monitor.CONFIG.update(
            {
                "client_id": "test_id",
                "client_secret": "test_secret",
                "telegram_bot_token": "test_token",
                "telegram_chat_id": "12345",
                "balance_threshold": 5_000_000,
                "alert_timezone": "Europe/Moscow",
                "alert_hour_start": 15,
                "alert_hour_end": 23,
                "alert_start_time": "",
                "alert_end_time": "",
                "alert_dedupe": "hour",
            }
        )

    def tearDown(self):
        self._patch_state_file.stop()
        sber_monitor.CONFIG.clear()
        sber_monitor.CONFIG.update(self._orig_config)
        self._tmp.cleanup()

    def _mock_token_and_accounts(self, balances):
        """balances: list of balances — по одному на счёт."""
        tokens = {
            "access_token": "tok",
            "refresh_token": "refresh",
            "expires_at": int(time.time()) + 3600,
            "issued_at": int(time.time()),
            "refresh_issued_at": int(time.time()),
        }
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": f"4080281000000000{i:04d}",
                "balance": b,
            }
            for i, b in enumerate(balances, start=1111)
        ]
        return tokens, accounts

    def _run(self, balances, fake_now):
        if not isinstance(balances, list):
            balances = [balances]
        tokens, accounts = self._mock_token_and_accounts(balances=balances)
        with (
            patch.object(
                sber_monitor, "get_valid_access_token", return_value=("tok", tokens)
            ),
            patch.object(sber_monitor, "get_client_accounts", return_value=accounts),
            patch.object(sber_monitor, "_alert", return_value=True) as mock_alert,
            patch.object(sber_monitor, "check_refresh_token_expiry"),
            patch.object(sber_monitor, "now_in_alert_tz", return_value=fake_now),
        ):
            sber_monitor.check_and_alert()
        state = (
            json.loads(self._tmp_path.read_text()) if self._tmp_path.exists() else {}
        )
        return mock_alert, state.get("sber", {})

    def test_all_accounts_under_threshold_no_alert(self):
        # Два счёта, оба под 5М — тишина
        now = datetime(2026, 4, 15, 16, 30, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[4_000_000, 900_000], fake_now=now)
        mock_alert.assert_not_called()
        self.assertIsNone(sber.get("last_alert_hour"))

    def test_one_account_above_triggers_alert(self):
        # Второй счёт выше 5М → алерт, в сообщении только один номер счёта
        now = datetime(2026, 4, 15, 15, 0, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[2_000_000, 7_500_000], fake_now=now)
        mock_alert.assert_called_once()
        msg = mock_alert.call_args[0][0]
        self.assertIn("положи деньги на депозит", msg)
        self.assertIn("7 500 000", msg)
        # Первый счёт не должен быть в списке превышений
        self.assertNotIn("2 000 000", msg.split("Превышение")[1])
        self.assertEqual(sber["last_alert_hour"], "2026-04-15T15")

    def test_exactly_at_threshold_no_alert(self):
        # Ровно 5М — не алерт (проверка строго > threshold)
        now = datetime(2026, 4, 15, 16, 0, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[5_000_000], fake_now=now)
        mock_alert.assert_not_called()

    def test_above_threshold_before_15_no_alert(self):
        now = datetime(2026, 4, 15, 14, 59, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=now)
        mock_alert.assert_not_called()

    def test_above_threshold_at_16_sends_another_alert(self):
        # В 15:00 уже алертили — в 16:00 должен прийти ещё один
        self._tmp_path.write_text(
            json.dumps({"sber": {"last_alert_hour": "2026-04-15T15"}})
        )
        now = datetime(2026, 4, 15, 16, 0, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=now)
        mock_alert.assert_called_once()
        self.assertEqual(sber["last_alert_hour"], "2026-04-15T16")

    def test_not_duplicated_within_same_hour(self):
        self._tmp_path.write_text(
            json.dumps({"sber": {"last_alert_hour": "2026-04-15T15"}})
        )
        now = datetime(2026, 4, 15, 15, 10, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=now)
        mock_alert.assert_not_called()
        self.assertEqual(sber["last_alert_hour"], "2026-04-15T15")

    def test_after_23_no_alert(self):
        # 2026-04-16 00:30 — ночь среда→четверг, после окончания окна в 23:59 ср
        now_midnight = datetime(2026, 4, 16, 0, 30, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=now_midnight)
        mock_alert.assert_not_called()

    def test_balance_drop_clears_alert_state(self):
        # Пре-заполняем, что уже был алерт — все счета упали под порог
        self._tmp_path.write_text(
            json.dumps({"sber": {"last_alert_hour": "2026-04-15T15"}})
        )
        now = datetime(2026, 4, 15, 16, 0, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[4_000_000, 500_000], fake_now=now)
        mock_alert.assert_not_called()
        self.assertIsNone(sber["last_alert_hour"])

    def test_hourly_pattern_entire_evening(self):
        # С 15 до 20 — 6 алертов, если хотя бы на одном счёте всё ещё выше 5М
        for hour in range(15, 21):
            now = datetime(2026, 4, 15, hour, 0, tzinfo=self.TZ)
            mock_alert, sber = self._run(balances=[6_000_000], fake_now=now)
            mock_alert.assert_called_once()
            self.assertEqual(sber["last_alert_hour"], f"2026-04-15T{hour:02d}")

    def test_telegram_failure_raises_systemexit(self):
        # hourly check: balance above threshold but Telegram fails → sys.exit(1)
        now = datetime(2026, 4, 15, 16, 0, tzinfo=self.TZ)  # Wednesday
        tokens, accounts = self._mock_token_and_accounts([7_000_000])
        with (
            patch.object(
                sber_monitor, "get_valid_access_token", return_value=("tok", tokens)
            ),
            patch.object(sber_monitor, "get_client_accounts", return_value=accounts),
            patch.object(
                sber_monitor, "enrich_with_balances", side_effect=lambda t, a, d: a
            ),
            patch.object(sber_monitor, "check_refresh_token_expiry"),
            patch.object(sber_monitor, "_alert", return_value=False),
            patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
        ):
            with self.assertRaises(SystemExit) as cm:
                sber_monitor.check_and_alert()
            self.assertEqual(cm.exception.code, 1)
        # last_alert_hour must NOT be marked — next run retries
        state = (
            json.loads(self._tmp_path.read_text()) if self._tmp_path.exists() else {}
        )
        self.assertIsNone(state.get("sber", {}).get("last_alert_hour"))

    def test_weekend_skip_saturday(self):
        # Saturday 2026-04-25 — no alert even if above threshold
        now = datetime(2026, 4, 25, 16, 30, tzinfo=self.TZ)
        with (
            patch.object(sber_monitor, "get_valid_access_token") as mock_tok,
            patch.object(sber_monitor, "_alert") as mock_alert,
            patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
        ):
            sber_monitor.check_and_alert()
        # Weekend guard must short-circuit before any API call
        mock_tok.assert_not_called()
        mock_alert.assert_not_called()

    def test_weekend_skip_sunday(self):
        now = datetime(2026, 4, 26, 16, 30, tzinfo=self.TZ)
        with (
            patch.object(sber_monitor, "get_valid_access_token") as mock_tok,
            patch.object(sber_monitor, "_alert") as mock_alert,
            patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
        ):
            sber_monitor.check_and_alert()
        mock_tok.assert_not_called()
        mock_alert.assert_not_called()

    def test_re_alert_after_drop_and_rise_same_day(self):
        # 15:00 — алерт. 16:00 — положил, баланс упал. 17:00 — снова пришёл перевод,
        # баланс > порога → алерт ДОЛЖЕН прийти снова (защита от пропусков, глюков).
        t15 = datetime(2026, 4, 15, 15, 0, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=t15)
        mock_alert.assert_called_once()
        self.assertEqual(sber["last_alert_hour"], "2026-04-15T15")

        # 16:00 — положил, баланс упал
        t16 = datetime(2026, 4, 15, 16, 0, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[1_000_000], fake_now=t16)
        mock_alert.assert_not_called()
        self.assertIsNone(sber["last_alert_hour"])

        # 17:00 — снова перевод, 7М на счёте → новый алерт
        t17 = datetime(2026, 4, 15, 17, 0, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[7_000_000], fake_now=t17)
        mock_alert.assert_called_once()
        self.assertEqual(sber["last_alert_hour"], "2026-04-15T17")

    def test_ten_minute_evening_slots(self):
        sber_monitor.CONFIG.update(
            {
                "alert_start_time": "19:10",
                "alert_end_time": "20:00",
                "alert_dedupe": "minute",
            }
        )

        t1909 = datetime(2026, 4, 15, 19, 9, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=t1909)
        mock_alert.assert_not_called()

        t1910 = datetime(2026, 4, 15, 19, 10, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=t1910)
        mock_alert.assert_called_once()
        self.assertEqual(sber["last_alert_key"], "2026-04-15T19:10")

        t1920 = datetime(2026, 4, 15, 19, 20, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=t1920)
        mock_alert.assert_called_once()
        self.assertEqual(sber["last_alert_key"], "2026-04-15T19:20")

        t2000 = datetime(2026, 4, 15, 20, 0, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=t2000)
        mock_alert.assert_called_once()
        self.assertEqual(sber["last_alert_key"], "2026-04-15T20:00")

        t2001 = datetime(2026, 4, 15, 20, 1, tzinfo=self.TZ)
        mock_alert, sber = self._run(balances=[6_000_000], fake_now=t2001)
        mock_alert.assert_not_called()


class SendFinalWarningTests(unittest.TestCase):
    TZ = ZoneInfo("Europe/Moscow")
    # Thursday 2026-04-16 at 19:55 MSK — default non-Friday weekday.
    # Friday-specific text is covered in a separate test below.
    WEEKDAY_NOW = datetime(2026, 4, 16, 19, 50, tzinfo=TZ)
    FRIDAY_NOW = datetime(2026, 4, 17, 19, 50, tzinfo=TZ)

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name) / "monitor_state.json"
        self._patch_state_file = patch("utils.DEFAULT_STATE_FILE", self._tmp_path)
        self._patch_state_file.start()

        self._orig_config = dict(sber_monitor.CONFIG)
        sber_monitor.CONFIG.update(
            {
                "client_id": "cid",
                "client_secret": "csec",
                "telegram_bot_token": "tok",
                "telegram_chat_id": "42",
                "balance_threshold": 5_000_000,
                "alert_timezone": "Europe/Moscow",
                "alert_hour_start": 15,
                "alert_hour_end": 23,
            }
        )

    def tearDown(self):
        self._patch_state_file.stop()
        sber_monitor.CONFIG.clear()
        sber_monitor.CONFIG.update(self._orig_config)
        self._tmp.cleanup()

    def _run(self, balances, now=None, image_exists=False):
        """Run send_final_warning with mocked I/O.

        By default FINAL_WARNING_IMAGE is redirected to a missing path so
        the fallback _alert branch is exercised. Set image_exists=True to
        point at a real temp file and exercise the sendPhoto branch.
        """
        if now is None:
            now = self.WEEKDAY_NOW
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": f"4080281000000000{i:04d}",
                "balance": b,
            }
            for i, b in enumerate(balances, start=1111)
        ]
        if image_exists:
            image_path = Path(self._tmp.name) / "image.jpg"
            image_path.write_bytes(b"\xff\xd8\xff\xe0FAKE-JPEG")
        else:
            image_path = Path(self._tmp.name) / "nonexistent.jpg"

        with (
            patch.object(sber_monitor, "FINAL_WARNING_IMAGE", image_path),
            patch.object(
                sber_monitor,
                "get_valid_access_token",
                return_value=("tok", {"refresh_issued_at": int(time.time())}),
            ),
            patch.object(sber_monitor, "get_client_accounts", return_value=accounts),
            patch.object(
                sber_monitor, "enrich_with_balances", side_effect=lambda t, a, d: a
            ),
            patch.object(sber_monitor, "check_refresh_token_expiry"),
            patch.object(sber_monitor, "_alert", return_value=True) as mock_alert,
            patch.object(
                sber_monitor, "send_telegram_photo", return_value=True
            ) as mock_photo,
            patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
        ):
            sber_monitor.send_final_warning()
        state = (
            json.loads(self._tmp_path.read_text()) if self._tmp_path.exists() else {}
        )
        return mock_alert, mock_photo, state.get("sber", {})

    def _seed_state(self, **sber_fields):
        self._tmp_path.write_text(json.dumps({"sber": sber_fields}))

    def test_silent_when_all_under_threshold(self):
        mock_alert, mock_photo, _ = self._run(balances=[4_000_000, 900_000])
        mock_alert.assert_not_called()
        mock_photo.assert_not_called()

    def test_sends_text_fallback_when_no_image(self):
        # image missing → should send text via _alert
        mock_alert, mock_photo, sber = self._run(balances=[7_500_000])
        mock_photo.assert_not_called()
        mock_alert.assert_called_once()
        msg = mock_alert.call_args.args[0]
        self.assertIn("ПОСЛЕДНИЙ ЗВОНОК", msg)
        self.assertIn("7 500 000", msg)
        self.assertEqual(sber["last_final_warning_date"], "2026-04-16")

    def test_sends_photo_when_image_exists(self):
        mock_alert, mock_photo, sber = self._run(
            balances=[7_500_000], image_exists=True
        )
        mock_photo.assert_called_once()
        mock_alert.assert_not_called()
        caption = mock_photo.call_args.kwargs["caption"]
        self.assertIn("ПОСЛЕДНИЙ ЗВОНОК", caption)
        self.assertIn("7 500 000", caption)
        self.assertLessEqual(len(caption), 1024, "Telegram caption limit is 1024 chars")
        self.assertEqual(sber["last_final_warning_date"], "2026-04-16")

    def test_falls_back_to_text_when_sendphoto_fails(self):
        image_path = Path(self._tmp.name) / "image.jpg"
        image_path.write_bytes(b"\xff\xd8\xff\xe0FAKE")
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": "40802810111111111111",
                "balance": 7_000_000,
            }
        ]
        with (
            patch.object(sber_monitor, "FINAL_WARNING_IMAGE", image_path),
            patch.object(
                sber_monitor,
                "get_valid_access_token",
                return_value=("tok", {"refresh_issued_at": int(time.time())}),
            ),
            patch.object(sber_monitor, "get_client_accounts", return_value=accounts),
            patch.object(
                sber_monitor, "enrich_with_balances", side_effect=lambda t, a, d: a
            ),
            patch.object(sber_monitor, "check_refresh_token_expiry"),
            patch.object(
                sber_monitor, "send_telegram_photo", return_value=False
            ) as mock_photo,
            patch.object(sber_monitor, "_alert", return_value=True) as mock_alert,
            patch.object(
                sber_monitor, "now_in_alert_tz", return_value=self.WEEKDAY_NOW
            ),
        ):
            sber_monitor.send_final_warning()
        mock_photo.assert_called_once()
        mock_alert.assert_called_once()

    def test_friday_uses_three_day_emphasis(self):
        mock_alert, _mp, sber = self._run(balances=[7_500_000], now=self.FRIDAY_NOW)
        mock_alert.assert_called_once()
        msg = mock_alert.call_args.args[0]
        self.assertIn("ПЯТНИЦА", msg)
        self.assertIn("3 дня", msg)
        self.assertNotIn("ПОСЛЕДНИЙ ЗВОНОК", msg)
        self.assertEqual(sber["last_final_warning_date"], "2026-04-17")

    def test_caption_under_telegram_limit_many_accounts(self):
        # 5 above-threshold accounts — caption must still fit 1024
        balances = [6_000_000 + i * 100_000 for i in range(5)]
        mock_alert, _mp, _ = self._run(balances=balances)
        msg = mock_alert.call_args.args[0]
        self.assertLessEqual(len(msg), 1024)

    def test_includes_hours_left_until_end_of_window(self):
        mock_alert, _mp, _ = self._run(balances=[6_000_000])
        msg = mock_alert.call_args.args[0]
        self.assertIn("5 ч", msg)

    def test_multiple_accounts_all_listed(self):
        mock_alert, _mp, _ = self._run(balances=[6_000_000, 8_000_000])
        msg = mock_alert.call_args.args[0]
        self.assertIn("6 000 000", msg)
        self.assertIn("8 000 000", msg)

    def test_missing_env_aborts(self):
        sber_monitor.CONFIG["telegram_bot_token"] = ""
        with (
            patch.object(sber_monitor, "get_valid_access_token") as mock_tok,
            patch.object(sber_monitor, "_alert") as mock_alert,
            patch.object(sber_monitor, "send_telegram_photo") as mock_photo,
        ):
            sber_monitor.send_final_warning()
        mock_tok.assert_not_called()
        mock_alert.assert_not_called()
        mock_photo.assert_not_called()

    def test_silent_on_saturday(self):
        saturday = datetime(2026, 4, 25, 19, 50, tzinfo=self.TZ)
        mock_alert, mock_photo, _ = self._run(balances=[7_000_000], now=saturday)
        mock_alert.assert_not_called()
        mock_photo.assert_not_called()

    def test_silent_on_sunday(self):
        sunday = datetime(2026, 4, 26, 19, 50, tzinfo=self.TZ)
        mock_alert, mock_photo, _ = self._run(balances=[7_000_000], now=sunday)
        mock_alert.assert_not_called()
        mock_photo.assert_not_called()

    def test_silent_if_already_sent_today(self):
        self._seed_state(last_final_warning_date="2026-04-16")
        mock_alert, mock_photo, sber = self._run(balances=[7_000_000])
        mock_alert.assert_not_called()
        mock_photo.assert_not_called()
        self.assertEqual(sber["last_final_warning_date"], "2026-04-16")

    def test_sends_if_last_warning_was_yesterday(self):
        self._seed_state(last_final_warning_date="2026-04-15")
        mock_alert, _mp, sber = self._run(balances=[7_000_000])
        mock_alert.assert_called_once()
        self.assertEqual(sber["last_final_warning_date"], "2026-04-16")

    def test_telegram_failure_raises_systemexit_and_does_not_mark_sent(self):
        # All delivery paths fail → sys.exit(1) so systemd OnFailure fires.
        # State must NOT be marked sent, so the next run retries cleanly.
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": "40802810111111111111",
                "balance": 7_000_000,
            }
        ]
        missing_image = Path(self._tmp.name) / "nonexistent.jpg"
        with (
            patch.object(sber_monitor, "FINAL_WARNING_IMAGE", missing_image),
            patch.object(
                sber_monitor,
                "get_valid_access_token",
                return_value=("tok", {"refresh_issued_at": int(time.time())}),
            ),
            patch.object(sber_monitor, "get_client_accounts", return_value=accounts),
            patch.object(
                sber_monitor, "enrich_with_balances", side_effect=lambda t, a, d: a
            ),
            patch.object(sber_monitor, "check_refresh_token_expiry"),
            patch.object(sber_monitor, "_alert", return_value=False),
            patch.object(
                sber_monitor, "now_in_alert_tz", return_value=self.WEEKDAY_NOW
            ),
        ):
            with self.assertRaises(SystemExit) as cm:
                sber_monitor.send_final_warning()
            self.assertEqual(cm.exception.code, 1)
        state = (
            json.loads(self._tmp_path.read_text()) if self._tmp_path.exists() else {}
        )
        self.assertNotIn("last_final_warning_date", state.get("sber", {}))

    def test_cli_flag_routes_to_send_final_warning(self):
        import sys as _sys

        with (
            patch.object(sber_monitor, "send_final_warning") as mock_fw,
            patch.object(_sys, "argv", ["sber_monitor.py", "--final-warning"]),
        ):
            sber_monitor.main()
        mock_fw.assert_called_once()


class SendFridayMorningReminderTests(unittest.TestCase):
    TZ = ZoneInfo("Europe/Moscow")
    FRIDAY_NOW = datetime(2026, 4, 17, 10, 0, tzinfo=TZ)

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name) / "monitor_state.json"
        self._patch_state_file = patch("utils.DEFAULT_STATE_FILE", self._tmp_path)
        self._patch_state_file.start()

        self._orig_config = dict(sber_monitor.CONFIG)
        sber_monitor.CONFIG.update(
            {
                "client_id": "cid",
                "client_secret": "csec",
                "telegram_bot_token": "tok",
                "telegram_chat_id": "42",
                "balance_threshold": 5_000_000,
                "alert_timezone": "Europe/Moscow",
                "alert_hour_start": 15,
                "alert_hour_end": 19,
            }
        )

    def tearDown(self):
        self._patch_state_file.stop()
        sber_monitor.CONFIG.clear()
        sber_monitor.CONFIG.update(self._orig_config)
        self._tmp.cleanup()

    def _run(self, balances, now=None):
        if now is None:
            now = self.FRIDAY_NOW
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": f"4080281000000000{i:04d}",
                "balance": b,
            }
            for i, b in enumerate(balances, start=1111)
        ]
        with (
            patch.object(
                sber_monitor,
                "get_valid_access_token",
                return_value=("tok", {"refresh_issued_at": int(time.time())}),
            ),
            patch.object(sber_monitor, "get_client_accounts", return_value=accounts),
            patch.object(
                sber_monitor, "enrich_with_balances", side_effect=lambda t, a, d: a
            ),
            patch.object(sber_monitor, "check_refresh_token_expiry"),
            patch.object(sber_monitor, "_alert", return_value=True) as mock_alert,
            patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
        ):
            sber_monitor.send_friday_morning_reminder()
        state = (
            json.loads(self._tmp_path.read_text()) if self._tmp_path.exists() else {}
        )
        return mock_alert, state.get("sber", {})

    def test_sends_on_friday_when_above(self):
        mock_alert, sber = self._run(balances=[7_500_000])
        mock_alert.assert_called_once()
        msg = mock_alert.call_args.args[0]
        self.assertIn("Пятница", msg)
        self.assertIn("3 дня простоя", msg)
        self.assertIn("7 500 000", msg)
        self.assertEqual(sber["last_friday_reminder_date"], "2026-04-17")

    def test_silent_on_friday_when_all_under(self):
        mock_alert, sber = self._run(balances=[3_000_000, 1_000_000])
        mock_alert.assert_not_called()
        self.assertNotIn("last_friday_reminder_date", sber)

    def test_silent_on_non_friday_thursday(self):
        thursday = datetime(2026, 4, 16, 10, 0, tzinfo=self.TZ)
        with (
            patch.object(sber_monitor, "get_valid_access_token") as mock_tok,
            patch.object(sber_monitor, "_alert") as mock_alert,
            patch.object(sber_monitor, "now_in_alert_tz", return_value=thursday),
        ):
            sber_monitor.send_friday_morning_reminder()
        mock_tok.assert_not_called()
        mock_alert.assert_not_called()

    def test_silent_on_saturday(self):
        saturday = datetime(2026, 4, 25, 10, 0, tzinfo=self.TZ)
        with (
            patch.object(sber_monitor, "get_valid_access_token") as mock_tok,
            patch.object(sber_monitor, "_alert") as mock_alert,
            patch.object(sber_monitor, "now_in_alert_tz", return_value=saturday),
        ):
            sber_monitor.send_friday_morning_reminder()
        mock_tok.assert_not_called()
        mock_alert.assert_not_called()

    def test_silent_if_already_sent_today(self):
        self._tmp_path.write_text(
            json.dumps({"sber": {"last_friday_reminder_date": "2026-04-17"}})
        )
        mock_alert, sber = self._run(balances=[7_000_000])
        mock_alert.assert_not_called()
        self.assertEqual(sber["last_friday_reminder_date"], "2026-04-17")

    def test_sends_if_last_reminder_was_previous_friday(self):
        self._tmp_path.write_text(
            json.dumps({"sber": {"last_friday_reminder_date": "2026-04-10"}})
        )
        mock_alert, sber = self._run(balances=[7_000_000])
        mock_alert.assert_called_once()
        self.assertEqual(sber["last_friday_reminder_date"], "2026-04-17")

    def test_missing_env_aborts(self):
        sber_monitor.CONFIG["telegram_bot_token"] = ""
        with (
            patch.object(sber_monitor, "get_valid_access_token") as mock_tok,
            patch.object(sber_monitor, "_alert") as mock_alert,
        ):
            sber_monitor.send_friday_morning_reminder()
        mock_tok.assert_not_called()
        mock_alert.assert_not_called()

    def test_telegram_failure_raises_systemexit_and_does_not_mark_sent(self):
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": "40802810111111111111",
                "balance": 7_000_000,
            }
        ]
        with (
            patch.object(
                sber_monitor,
                "get_valid_access_token",
                return_value=("tok", {"refresh_issued_at": int(time.time())}),
            ),
            patch.object(sber_monitor, "get_client_accounts", return_value=accounts),
            patch.object(
                sber_monitor, "enrich_with_balances", side_effect=lambda t, a, d: a
            ),
            patch.object(sber_monitor, "check_refresh_token_expiry"),
            patch.object(sber_monitor, "_alert", return_value=False),
            patch.object(sber_monitor, "now_in_alert_tz", return_value=self.FRIDAY_NOW),
        ):
            with self.assertRaises(SystemExit) as cm:
                sber_monitor.send_friday_morning_reminder()
            self.assertEqual(cm.exception.code, 1)
        state = (
            json.loads(self._tmp_path.read_text()) if self._tmp_path.exists() else {}
        )
        self.assertNotIn("last_friday_reminder_date", state.get("sber", {}))

    def test_cli_flag_routes(self):
        import sys as _sys

        with (
            patch.object(sber_monitor, "send_friday_morning_reminder") as mock_fw,
            patch.object(_sys, "argv", ["sber_monitor.py", "--friday-reminder"]),
        ):
            sber_monitor.main()
        mock_fw.assert_called_once()


class BalanceChangeHistoryTests(unittest.TestCase):
    """Stale balance detection — phase 3 of reliability work."""

    def test_first_seen_records_timestamp(self):
        accounts = [{"accountNumber": "A1", "balance": 100_000.0}]
        sber_state = {}
        hist = sber_monitor.update_balance_change_history(
            accounts, sber_state, now_ts=1000
        )
        self.assertEqual(hist["A1"]["balance"], 100_000.0)
        self.assertEqual(hist["A1"]["last_changed_at"], 1000)
        # also mutated in-place
        self.assertIs(sber_state["balance_changes"], hist)

    def test_unchanged_balance_keeps_timestamp(self):
        sber_state = {
            "balance_changes": {"A1": {"balance": 100.0, "last_changed_at": 500}}
        }
        accounts = [{"accountNumber": "A1", "balance": 100.0}]
        sber_monitor.update_balance_change_history(accounts, sber_state, now_ts=99999)
        self.assertEqual(sber_state["balance_changes"]["A1"]["last_changed_at"], 500)

    def test_changed_balance_bumps_timestamp(self):
        sber_state = {
            "balance_changes": {"A1": {"balance": 100.0, "last_changed_at": 500}}
        }
        accounts = [{"accountNumber": "A1", "balance": 250.0}]
        sber_monitor.update_balance_change_history(accounts, sber_state, now_ts=99999)
        self.assertEqual(
            sber_state["balance_changes"]["A1"],
            {"balance": 250.0, "last_changed_at": 99999},
        )

    def test_skips_account_with_no_number(self):
        sber_state = {}
        accounts = [{"balance": 100.0}]  # no accountNumber
        hist = sber_monitor.update_balance_change_history(
            accounts, sber_state, now_ts=1000
        )
        self.assertEqual(hist, {})

    def test_skips_account_with_none_balance(self):
        sber_state = {}
        accounts = [{"accountNumber": "A1", "balance": None}]
        sber_monitor.update_balance_change_history(accounts, sber_state, now_ts=1000)
        self.assertNotIn("A1", sber_state["balance_changes"])


class FormatStaleNoteTests(unittest.TestCase):
    def test_empty_when_no_history(self):
        self.assertEqual(sber_monitor.format_stale_note("A1", {}, now_ts=1000), "")

    def test_empty_when_age_under_one_day(self):
        s = {"balance_changes": {"A1": {"balance": 100.0, "last_changed_at": 1000}}}
        # 12h later
        self.assertEqual(
            sber_monitor.format_stale_note("A1", s, now_ts=1000 + 12 * 3600),
            "",
        )

    def test_renders_age_without_warning_for_short_period(self):
        s = {"balance_changes": {"A1": {"balance": 100.0, "last_changed_at": 1000}}}
        # 3 days later (<7)
        note = sber_monitor.format_stale_note("A1", s, now_ts=1000 + 3 * 86400)
        self.assertEqual(note, " · 3 д. без движения")
        self.assertNotIn("⚠️", note)

    def test_warns_when_older_than_threshold(self):
        s = {"balance_changes": {"A1": {"balance": 100.0, "last_changed_at": 1000}}}
        note = sber_monitor.format_stale_note("A1", s, now_ts=1000 + 8 * 86400)
        self.assertIn("8 д.", note)
        self.assertIn("⚠️", note)

    def test_check_and_alert_integrates_stale_note_in_message(self):
        # end-to-end: check_and_alert should include " ⚠️ X д. без движения"
        # when balance hasn't moved in 8+ days
        tz = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 4, 15, 16, 0, tzinfo=tz)  # Wednesday 16:00
        tmp = TemporaryDirectory()
        tmp_path = Path(tmp.name) / "monitor_state.json"
        old_ts = int(now.timestamp()) - 9 * 86400
        tmp_path.write_text(
            json.dumps(
                {
                    "sber": {
                        "balance_changes": {
                            "40802810000000001111": {
                                "balance": 7_000_000.0,
                                "last_changed_at": old_ts,
                            }
                        }
                    }
                }
            )
        )
        try:
            with (
                patch("utils.DEFAULT_STATE_FILE", tmp_path),
                patch.dict(
                    sber_monitor.CONFIG,
                    {
                        "client_id": "cid",
                        "client_secret": "cs",
                        "telegram_bot_token": "t",
                        "telegram_chat_id": "1",
                        "balance_threshold": 5_000_000,
                        "alert_timezone": "Europe/Moscow",
                        "alert_hour_start": 15,
                        "alert_hour_end": 19,
                    },
                ),
                patch.object(
                    sber_monitor,
                    "get_valid_access_token",
                    return_value=("tok", {"refresh_issued_at": int(time.time())}),
                ),
                patch.object(
                    sber_monitor,
                    "get_client_accounts",
                    return_value=[
                        {
                            "currencyCode": "RUB",
                            "accountType": "CURRENT",
                            "state": "OPEN",
                            "accountNumber": "40802810000000001111",
                            "balance": 7_000_000.0,
                        }
                    ],
                ),
                patch.object(
                    sber_monitor, "enrich_with_balances", side_effect=lambda t, a, d: a
                ),
                patch.object(sber_monitor, "check_refresh_token_expiry"),
                patch.object(sber_monitor, "_alert", return_value=True) as mock_alert,
                patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
            ):
                sber_monitor.check_and_alert()
            mock_alert.assert_called_once()
            msg = mock_alert.call_args.args[0]
            self.assertIn("9 д. без движения", msg)
            self.assertIn("⚠️", msg)
        finally:
            tmp.cleanup()


class RefreshAccessTokenTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._tokens_path = Path(self._tmp.name) / "sber_tokens.json"
        self._patch = patch.object(sber_monitor, "TOKENS_FILE", self._tokens_path)
        self._patch.start()
        sber_monitor.CONFIG["client_id"] = "cid"
        sber_monitor.CONFIG["client_secret"] = "csecret"

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_refresh_rotates_tokens_and_saves(self):
        old = {
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "expires_at": int(time.time()) - 100,
            "issued_at": int(time.time()) - 3700,
            "refresh_issued_at": int(time.time()) - 7 * 86400,
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
            "id_token": "id_tok",
        }
        with patch(
            "sber_monitor.requests.request", return_value=mock_resp
        ) as mock_request:
            new = sber_monitor.refresh_access_token(old)

        mock_request.assert_called_once()
        self.assertEqual(mock_request.call_args.args[0], "POST")
        call_kwargs = mock_request.call_args.kwargs
        self.assertEqual(call_kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(call_kwargs["data"]["refresh_token"], "old_refresh")
        self.assertEqual(call_kwargs["data"]["client_id"], "cid")
        self.assertEqual(call_kwargs["data"]["client_secret"], "csecret")

        self.assertEqual(new["access_token"], "new_access")
        self.assertEqual(new["refresh_token"], "new_refresh")
        self.assertGreater(new["expires_at"], int(time.time()))

        saved = json.loads(self._tokens_path.read_text())
        self.assertEqual(saved["access_token"], "new_access")

    def test_refresh_preserves_old_refresh_if_not_rotated(self):
        # Если сервер не вернул новый refresh_token — оставляем старый
        old = {
            "access_token": "old",
            "refresh_token": "keep_me",
            "expires_at": 0,
            "issued_at": 0,
            "refresh_issued_at": int(time.time()) - 30 * 86400,
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new", "expires_in": 3600}
        with patch("sber_monitor.requests.request", return_value=mock_resp):
            new = sber_monitor.refresh_access_token(old)
        self.assertEqual(new["refresh_token"], "keep_me")
        # refresh_issued_at не должен обновиться, т.к. сам refresh_token не новый
        self.assertEqual(new["refresh_issued_at"], old["refresh_issued_at"])

    def test_refresh_requires_client_secret(self):
        old = {
            "access_token": "old",
            "refresh_token": "refresh",
            "expires_at": 0,
            "issued_at": 0,
        }
        sber_monitor.CONFIG["client_secret"] = ""
        with patch("sber_monitor.requests.request") as mock_request:
            with self.assertRaises(SystemExit) as cm:
                sber_monitor.refresh_access_token(old)
        self.assertIn("client_secret", str(cm.exception))
        mock_request.assert_not_called()


class RefreshTokenExpiryTests(unittest.TestCase):
    def setUp(self):
        sber_monitor.CONFIG["telegram_bot_token"] = "t"
        sber_monitor.CONFIG["telegram_chat_id"] = "1"

    def test_warns_14_days_before(self):
        tokens = {"refresh_issued_at": int(time.time()) - (180 - 10) * 86400}
        with patch.object(sber_monitor, "_alert") as mock_alert:
            sber_monitor.check_refresh_token_expiry(tokens)
        mock_alert.assert_called_once()
        self.assertIn("скоро истечёт", mock_alert.call_args[0][0])

    def test_warns_when_expired(self):
        tokens = {"refresh_issued_at": int(time.time()) - 200 * 86400}
        with patch.object(sber_monitor, "_alert") as mock_alert:
            sber_monitor.check_refresh_token_expiry(tokens)
        mock_alert.assert_called_once()
        self.assertIn("истёк", mock_alert.call_args[0][0])

    def test_no_warning_when_plenty_of_time(self):
        tokens = {"refresh_issued_at": int(time.time()) - 30 * 86400}
        with patch.object(sber_monitor, "_alert") as mock_alert:
            sber_monitor.check_refresh_token_expiry(tokens)
        mock_alert.assert_not_called()


class SberRequestRetryTests(unittest.TestCase):
    def test_retries_on_connect_timeout_then_raises(self):
        with (
            patch.object(
                sber_monitor.requests,
                "request",
                side_effect=requests.ConnectTimeout("boom"),
            ) as mock_req,
            patch.object(sber_monitor.time, "sleep") as mock_sleep,
        ):
            with self.assertRaises(sber_monitor.SberAPIUnavailable) as cm:
                sber_monitor._sber_request("GET", "https://example.invalid/")
        self.assertEqual(mock_req.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)
        # exponential backoff: 1s, 2s
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [1, 2])
        self.assertIn("boom", str(cm.exception))

    def test_returns_response_on_first_success(self):
        ok = MagicMock(status_code=200)
        with (
            patch.object(sber_monitor.requests, "request", return_value=ok) as mock_req,
            patch.object(sber_monitor.time, "sleep") as mock_sleep,
        ):
            resp = sber_monitor._sber_request("GET", "https://example.invalid/")
        self.assertIs(resp, ok)
        self.assertEqual(mock_req.call_count, 1)
        mock_sleep.assert_not_called()

    def test_recovers_after_one_failure(self):
        ok = MagicMock(status_code=200)
        with (
            patch.object(
                sber_monitor.requests,
                "request",
                side_effect=[requests.ConnectTimeout("blip"), ok],
            ),
            patch.object(sber_monitor.time, "sleep"),
        ):
            resp = sber_monitor._sber_request("GET", "https://example.invalid/")
        self.assertIs(resp, ok)

    def test_get_account_balance_reraises_sber_unavailable(self):
        # Если _sber_request исчерпал попытки, get_account_balance должен
        # пробросить SberAPIUnavailable наверх (не глотать per-account).
        with patch.object(
            sber_monitor,
            "_sber_request",
            side_effect=sber_monitor.SberAPIUnavailable("timeout"),
        ):
            with self.assertRaises(sber_monitor.SberAPIUnavailable):
                sber_monitor.get_account_balance(
                    "tok", "40802810000000001111", "2026-04-20"
                )


class SberOutageHandlingTests(unittest.TestCase):
    """Тесты мягкой обработки SberAPIUnavailable в check_and_alert."""

    TZ = ZoneInfo("Europe/Moscow")

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._tmp_path = Path(self._tmp.name) / "monitor_state.json"
        self._patch_state_file = patch("utils.DEFAULT_STATE_FILE", self._tmp_path)
        self._patch_state_file.start()

        self._orig_config = dict(sber_monitor.CONFIG)
        sber_monitor.CONFIG.update(
            {
                "client_id": "cid",
                "client_secret": "csec",
                "telegram_bot_token": "tok",
                "telegram_chat_id": "42",
                "balance_threshold": 5_000_000,
                "alert_timezone": "Europe/Moscow",
                "alert_hour_start": 15,
                "alert_hour_end": 19,
            }
        )

    def tearDown(self):
        self._patch_state_file.stop()
        sber_monitor.CONFIG.clear()
        sber_monitor.CONFIG.update(self._orig_config)
        self._tmp.cleanup()

    def _today_alert_tz(self):
        return datetime.now(timezone.utc).astimezone(self.TZ).strftime("%Y-%m-%d")

    def _run_check_with_outage(self, seed_state=None):
        if seed_state is not None:
            self._tmp_path.write_text(json.dumps({"sber": seed_state}))
        now = datetime(2026, 4, 15, 16, 0, tzinfo=self.TZ)  # Wed in window
        with (
            patch.object(
                sber_monitor,
                "get_valid_access_token",
                side_effect=sber_monitor.SberAPIUnavailable("connect timeout"),
            ),
            patch.object(sber_monitor, "_alert", return_value=True) as mock_alert,
            patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
        ):
            sber_monitor.check_and_alert()
        state = json.loads(self._tmp_path.read_text())
        return mock_alert, state.get("sber", {})

    def test_soft_fail_without_last_success_is_silent(self):
        # Нет last_successful_api_at → gap неизвестен → не алертим, не падаем.
        mock_alert, sber = self._run_check_with_outage(seed_state={})
        mock_alert.assert_not_called()
        self.assertIn("last_api_failure_at", sber)
        self.assertNotIn("last_api_down_alert_date", sber)

    def test_short_outage_below_threshold_is_silent(self):
        # Gap ~2ч — ниже порога в 6ч → тишина
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        mock_alert, sber = self._run_check_with_outage(
            seed_state={"last_successful_api_at": two_hours_ago}
        )
        mock_alert.assert_not_called()
        self.assertNotIn("last_api_down_alert_date", sber)

    def test_prolonged_outage_triggers_single_alert(self):
        seven_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        mock_alert, sber = self._run_check_with_outage(
            seed_state={"last_successful_api_at": seven_hours_ago}
        )
        mock_alert.assert_called_once()
        msg = mock_alert.call_args.args[0]
        self.assertIn("Sber API недоступен", msg)
        self.assertIn("7 ч", msg)
        self.assertEqual(sber["last_api_down_alert_date"], self._today_alert_tz())

    def test_prolonged_outage_silent_after_first_alert_same_day(self):
        seven_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        mock_alert, sber = self._run_check_with_outage(
            seed_state={
                "last_successful_api_at": seven_hours_ago,
                "last_api_down_alert_date": self._today_alert_tz(),
            }
        )
        mock_alert.assert_not_called()
        # Дата «уже алертили сегодня» сохраняется
        self.assertEqual(sber["last_api_down_alert_date"], self._today_alert_tz())

    def test_outage_alert_telegram_fail_triggers_exit(self):
        # При длительном outage, если Telegram не принял — sys.exit(1) (Phase 1).
        seven_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        self._tmp_path.write_text(
            json.dumps({"sber": {"last_successful_api_at": seven_hours_ago}})
        )
        now = datetime(2026, 4, 15, 16, 0, tzinfo=self.TZ)
        with (
            patch.object(
                sber_monitor,
                "get_valid_access_token",
                side_effect=sber_monitor.SberAPIUnavailable("dead"),
            ),
            patch.object(sber_monitor, "_alert", return_value=False),
            patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
        ):
            with self.assertRaises(SystemExit) as cm:
                sber_monitor.check_and_alert()
            self.assertEqual(cm.exception.code, 1)
        # last_api_down_alert_date НЕ записывается при провале доставки —
        # следующий запуск повторит попытку.
        state = json.loads(self._tmp_path.read_text())
        self.assertNotIn("last_api_down_alert_date", state.get("sber", {}))
        self.assertIn("last_api_failure_at", state.get("sber", {}))

    def test_successful_call_records_last_success_and_clears_outage(self):
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        self._tmp_path.write_text(
            json.dumps(
                {
                    "sber": {
                        "last_successful_api_at": yesterday.isoformat(),
                        "last_api_down_alert_date": yesterday.astimezone(
                            self.TZ
                        ).strftime("%Y-%m-%d"),
                        "last_api_failure_at": yesterday.isoformat(),
                    }
                }
            )
        )
        tokens = {
            "access_token": "tok",
            "refresh_token": "r",
            "expires_at": int(time.time()) + 3600,
            "issued_at": int(time.time()),
            "refresh_issued_at": int(time.time()),
        }
        accounts = [
            {
                "currencyCode": "RUB",
                "accountType": "CURRENT",
                "state": "OPEN",
                "accountNumber": "40802810000000001111",
                "balance": 1_000_000,
            },  # below
        ]
        now = datetime(2026, 4, 15, 16, 0, tzinfo=self.TZ)
        with (
            patch.object(
                sber_monitor, "get_valid_access_token", return_value=("tok", tokens)
            ),
            patch.object(sber_monitor, "get_client_accounts", return_value=accounts),
            patch.object(sber_monitor, "_alert") as mock_alert,
            patch.object(sber_monitor, "check_refresh_token_expiry"),
            patch.object(sber_monitor, "now_in_alert_tz", return_value=now),
        ):
            sber_monitor.check_and_alert()
        mock_alert.assert_not_called()
        state = json.loads(self._tmp_path.read_text())
        sber = state["sber"]
        # last_successful_api_at обновлён (уже не вчера)
        new_ts = datetime.fromisoformat(sber["last_successful_api_at"])
        self.assertGreater(new_ts, yesterday)
        self.assertNotIn("last_api_down_alert_date", sber)


if __name__ == "__main__":
    unittest.main()
