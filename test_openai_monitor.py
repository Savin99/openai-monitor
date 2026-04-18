"""Unit tests for openai_monitor.

Покрывает:
- get_costs_for_period: single page, pagination, HTTP errors, retries, network, API errors.
- get_total_costs / today / week / month — корректные окна времени.
- get_billing_balance — 200 (но API уже deprecated, тестим на всякий), не-200, сетевые ошибки.
- openai_monitor.load_state — дефолты поверх пустого state.
- topup / do_topup / do_delete_topup — изменение total_deposited и history.
- get_status_message — ветки billing / manual / полный отказ API.
- check_and_alert — главная логика алертов (пороги, уровни, дедуп, сброс).
- send_status_report — шлёт статус через Telegram.
- main — роутинг CLI-флагов (--bot, --status, --topup <amount>, default).
"""

import json
import sys
import unittest
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests

import openai_monitor
import utils


def _mock_response(status=200, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload or {}
    resp.text = json.dumps(payload or {})
    return resp


def _costs_page(buckets, has_more=False, next_page=None):
    """Helper: build an OpenAI costs API page."""
    return {
        "object": "page",
        "has_more": has_more,
        "next_page": next_page,
        "data": [
            {"object": "bucket",
             "results": [{"amount": {"value": str(v), "currency": "usd"}} for v in vs]}
            for vs in buckets
        ],
    }


class StateIsolationMixin:
    """Redirect openai_monitor's state file to a temp file per test."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._state_path = Path(self._tmp.name) / "monitor_state.json"
        self._patch_state_file = patch.object(utils, "DEFAULT_STATE_FILE", self._state_path)
        self._patch_state_file.start()

        self._orig_config = dict(openai_monitor.CONFIG)
        openai_monitor.CONFIG.update({
            "openai_admin_key": "sk-test",
            "telegram_bot_token": "tg-test",
            "telegram_chat_id": "42",
            "alert_threshold": 100,
        })

    def tearDown(self):
        self._patch_state_file.stop()
        openai_monitor.CONFIG.clear()
        openai_monitor.CONFIG.update(self._orig_config)
        self._tmp.cleanup()

    def write_state(self, data):
        self._state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read_state(self):
        if not self._state_path.exists():
            return {}
        return json.loads(self._state_path.read_text(encoding="utf-8"))


# ──────────────────────────────── get_costs_for_period ─────────────────────────

class GetCostsForPeriodTests(StateIsolationMixin, unittest.TestCase):
    def test_sums_single_page(self):
        page = _costs_page([[1.5, 2.0], [0.75]])
        with patch("openai_monitor.requests.get", return_value=_mock_response(200, page)):
            cost, err = openai_monitor.get_costs_for_period(1000)
        self.assertEqual(cost, 4.25)
        self.assertIsNone(err)

    def test_paginates_until_no_more(self):
        p1 = _costs_page([[10]], has_more=True, next_page="p2")
        p2 = _costs_page([[5.25]], has_more=False)
        with patch("openai_monitor.requests.get",
                   side_effect=[_mock_response(200, p1), _mock_response(200, p2)]) as mock_get:
            cost, err = openai_monitor.get_costs_for_period(1000)
        self.assertEqual(cost, 15.25)
        self.assertIsNone(err)
        self.assertEqual(mock_get.call_count, 2)
        # second call must pass page=p2
        second_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertEqual(second_params["page"], "p2")

    def test_end_time_forwarded_as_param(self):
        with patch("openai_monitor.requests.get",
                   return_value=_mock_response(200, _costs_page([]))) as mock_get:
            openai_monitor.get_costs_for_period(1000, end_ts=2000)
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["start_time"], 1000)
        self.assertEqual(params["end_time"], 2000)
        self.assertEqual(params["limit"], 100)

    def test_http_error_all_retries(self):
        err_resp = _mock_response(500, {"error": "server"})
        with patch("openai_monitor.requests.get", return_value=err_resp) as mock_get, \
             patch("openai_monitor.time.sleep"):
            cost, err = openai_monitor.get_costs_for_period(1000, max_retries=3)
        self.assertIsNone(cost)
        self.assertIn("HTTP 500", err)
        self.assertEqual(mock_get.call_count, 3)

    def test_http_error_then_success(self):
        ok_page = _costs_page([[3.0]])
        with patch("openai_monitor.requests.get",
                   side_effect=[_mock_response(500, {}), _mock_response(200, ok_page)]), \
             patch("openai_monitor.time.sleep"):
            cost, err = openai_monitor.get_costs_for_period(1000, max_retries=2)
        self.assertEqual(cost, 3.0)
        self.assertIsNone(err)

    def test_network_exception_retried_then_success(self):
        ok_page = _costs_page([[1.0]])
        with patch("openai_monitor.requests.get",
                   side_effect=[requests.ConnectionError("boom"),
                                _mock_response(200, ok_page)]), \
             patch("openai_monitor.time.sleep"):
            cost, err = openai_monitor.get_costs_for_period(1000, max_retries=2)
        self.assertEqual(cost, 1.0)
        self.assertIsNone(err)

    def test_network_exception_all_retries_fail(self):
        with patch("openai_monitor.requests.get",
                   side_effect=requests.Timeout("slow")), \
             patch("openai_monitor.time.sleep"):
            cost, err = openai_monitor.get_costs_for_period(1000, max_retries=2)
        self.assertIsNone(cost)
        self.assertIn("Ошибка сети", err)

    def test_api_error_in_response_body(self):
        body = {"error": {"message": "bad key"}}
        with patch("openai_monitor.requests.get", return_value=_mock_response(200, body)):
            cost, err = openai_monitor.get_costs_for_period(1000)
        self.assertIsNone(cost)
        self.assertIn("API error", err)

    def test_empty_data_returns_zero(self):
        with patch("openai_monitor.requests.get",
                   return_value=_mock_response(200, _costs_page([]))):
            cost, err = openai_monitor.get_costs_for_period(1000)
        self.assertEqual(cost, 0)
        self.assertIsNone(err)

    def test_auth_header_includes_admin_key(self):
        with patch("openai_monitor.requests.get",
                   return_value=_mock_response(200, _costs_page([]))) as mock_get:
            openai_monitor.get_costs_for_period(1000)
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Authorization"],
                         "Bearer sk-test")


# ────────────────────────────────── period helpers ─────────────────────────────

class PeriodHelpersTests(StateIsolationMixin, unittest.TestCase):
    def test_get_total_costs_uses_2026_start(self):
        with patch("openai_monitor.get_costs_for_period",
                   return_value=(42.0, None)) as mock_p:
            cost, err = openai_monitor.get_total_costs()
        self.assertEqual(cost, 42.0)
        self.assertIsNone(err)
        mock_p.assert_called_once_with(1767225600)

    def test_get_today_costs_start_is_utc_midnight(self):
        captured = {}

        def fake(start_ts, end_ts=None, **kw):
            captured["start"] = start_ts
            captured["end"] = end_ts
            return (1.23, None)

        with patch("openai_monitor.get_costs_for_period", side_effect=fake):
            got = openai_monitor.get_today_costs()
        self.assertEqual(got, 1.23)
        self.assertIsNone(captured["end"])
        # start should be a recent UTC midnight (<= now, within last 24h)
        import time as _t
        self.assertLessEqual(captured["start"], int(_t.time()))
        self.assertGreaterEqual(captured["start"], int(_t.time()) - 86400)

    def test_get_week_costs_start_is_monday(self):
        from datetime import datetime, timezone
        with patch("openai_monitor.get_costs_for_period",
                   return_value=(7.0, None)) as mock_p:
            openai_monitor.get_week_costs()
        start_ts = mock_p.call_args.args[0]
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        self.assertEqual(dt.weekday(), 0)  # Monday
        self.assertEqual((dt.hour, dt.minute, dt.second), (0, 0, 0))

    def test_get_month_costs_start_is_first_day(self):
        from datetime import datetime, timezone
        with patch("openai_monitor.get_costs_for_period",
                   return_value=(30.0, None)) as mock_p:
            openai_monitor.get_month_costs()
        start_ts = mock_p.call_args.args[0]
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        self.assertEqual(dt.day, 1)
        self.assertEqual((dt.hour, dt.minute, dt.second), (0, 0, 0))

    def test_get_last_weeks_costs_returns_n_weeks_reverse_order(self):
        with patch("openai_monitor.get_costs_for_period",
                   return_value=(100.0, None)):
            weeks = openai_monitor.get_last_weeks_costs(4)
        self.assertEqual(len(weeks), 4)
        # All costs 100
        self.assertTrue(all(w["cost"] == 100.0 for w in weeks))
        # Weeks must start Mondays at 00:00
        for w in weeks:
            self.assertEqual(w["start"].weekday(), 0)

    def test_get_last_months_costs_returns_n_months(self):
        with patch("openai_monitor.get_costs_for_period",
                   return_value=(50.0, None)):
            months = openai_monitor.get_last_months_costs(3)
        self.assertEqual(len(months), 3)
        self.assertTrue(all(m["cost"] == 50.0 for m in months))


# ───────────────────────────── get_billing_balance ─────────────────────────────

class GetBillingBalanceTests(StateIsolationMixin, unittest.TestCase):
    def test_returns_none_on_non_200(self):
        with patch("openai_monitor.requests.get",
                   return_value=_mock_response(403, {"error": "forbidden"})):
            self.assertIsNone(openai_monitor.get_billing_balance())

    def test_returns_none_on_network_error(self):
        with patch("openai_monitor.requests.get",
                   side_effect=requests.ConnectionError("no")):
            self.assertIsNone(openai_monitor.get_billing_balance())

    def test_returns_none_when_fields_missing(self):
        with patch("openai_monitor.requests.get",
                   return_value=_mock_response(200, {"total_granted": None, "total_used": None})):
            self.assertIsNone(openai_monitor.get_billing_balance())

    def test_returns_balance_dict_on_success(self):
        with patch("openai_monitor.requests.get",
                   return_value=_mock_response(200, {"total_granted": "100.5", "total_used": "25.25"})):
            result = openai_monitor.get_billing_balance()
        self.assertEqual(result, {
            "total_granted": 100.5,
            "total_used": 25.25,
            "remaining": 75.25,
        })


# ─────────────────────────── load_state / save_state ───────────────────────────

class LoadStateTests(StateIsolationMixin, unittest.TestCase):
    def test_load_state_fills_defaults_when_file_missing(self):
        state = openai_monitor.load_state()
        self.assertEqual(state["total_deposited"], openai_monitor.DEFAULT_TOTAL_DEPOSITED)
        self.assertIsNone(state["last_alert_date"])
        self.assertIsNone(state["last_balance"])
        self.assertIsNone(state["bot_offset"])
        self.assertEqual(state["topup_history"], [])

    def test_existing_state_overrides_defaults(self):
        self.write_state({"total_deposited": 999.0, "custom": "x"})
        state = openai_monitor.load_state()
        self.assertEqual(state["total_deposited"], 999.0)
        self.assertEqual(state["custom"], "x")
        # missing fields still defaulted
        self.assertEqual(state["topup_history"], [])

    def test_save_roundtrip(self):
        openai_monitor.save_state({"total_deposited": 500, "topup_history": [{"a": 1}]})
        state = openai_monitor.load_state()
        self.assertEqual(state["total_deposited"], 500)
        self.assertEqual(state["topup_history"], [{"a": 1}])


# ──────────────────────────────────── topup ────────────────────────────────────

class TopupTests(StateIsolationMixin, unittest.TestCase):
    def test_topup_adds_to_default_when_state_empty(self):
        openai_monitor.topup(100)
        state = openai_monitor.load_state()
        self.assertEqual(state["total_deposited"],
                         round(openai_monitor.DEFAULT_TOTAL_DEPOSITED + 100, 2))

    def test_topup_adds_to_existing_total(self):
        self.write_state({"total_deposited": 1000})
        openai_monitor.topup(250.5)
        state = openai_monitor.load_state()
        self.assertEqual(state["total_deposited"], 1250.5)

    def test_topup_handles_float_precision(self):
        self.write_state({"total_deposited": 0.1})
        openai_monitor.topup(0.2)
        state = openai_monitor.load_state()
        self.assertEqual(state["total_deposited"], 0.3)  # rounded to 2


# ───────────────────────────── do_topup / do_delete ────────────────────────────

class DoTopupTests(StateIsolationMixin, unittest.TestCase):
    def test_do_topup_appends_history_and_updates_total(self):
        self.write_state({"total_deposited": 1000, "topup_history": []})
        with patch("openai_monitor.reply") as mock_reply:
            openai_monitor.do_topup("42", 500, actual_datetime="01.02.2026 12:00")
        state = openai_monitor.load_state()
        self.assertEqual(state["total_deposited"], 1500)
        self.assertEqual(len(state["topup_history"]), 1)
        entry = state["topup_history"][0]
        self.assertEqual(entry["amount"], 500)
        self.assertEqual(entry["actual_datetime"], "01.02.2026 12:00")
        mock_reply.assert_called_once()
        self.assertIn("+$500", mock_reply.call_args.args[1])

    def test_do_topup_without_datetime_omits_field(self):
        self.write_state({"total_deposited": 100})
        with patch("openai_monitor.reply"):
            openai_monitor.do_topup("42", 50)
        entry = openai_monitor.load_state()["topup_history"][0]
        self.assertNotIn("actual_datetime", entry)


class DoDeleteTopupTests(StateIsolationMixin, unittest.TestCase):
    def test_delete_removes_entry_and_subtracts(self):
        self.write_state({
            "total_deposited": 1500,
            "topup_history": [
                {"date": "d1", "amount": 500},
                {"date": "d2", "amount": 200},
            ],
        })
        with patch("openai_monitor.reply") as mock_reply:
            openai_monitor.do_delete_topup("42", 1)
        state = openai_monitor.load_state()
        self.assertEqual(len(state["topup_history"]), 1)
        self.assertEqual(state["topup_history"][0]["date"], "d2")
        self.assertEqual(state["total_deposited"], 1000)
        self.assertIn("🗑", mock_reply.call_args.args[1])

    def test_delete_invalid_index_replies_error(self):
        self.write_state({"total_deposited": 100, "topup_history": [{"amount": 100}]})
        with patch("openai_monitor.reply") as mock_reply:
            openai_monitor.do_delete_topup("42", 5)
        # state untouched
        state = openai_monitor.load_state()
        self.assertEqual(len(state["topup_history"]), 1)
        self.assertIn("Неверный номер", mock_reply.call_args.args[1])


# ────────────────────────────── get_status_message ─────────────────────────────

class GetStatusMessageTests(StateIsolationMixin, unittest.TestCase):
    def test_manual_mode_formats_balance(self):
        self.write_state({"total_deposited": 1000, "alert_threshold": 50})
        with patch("openai_monitor.get_total_costs", return_value=(200.0, None)), \
             patch("openai_monitor.get_today_costs", return_value=5.0), \
             patch("openai_monitor.get_billing_balance", return_value=None):
            msg = openai_monitor.get_status_message()
        self.assertIn("800", msg)  # 1000 - 200
        self.assertIn("(ручной)", msg)
        self.assertIn("Потрачено за 2026: $200.0", msg)
        self.assertIn("Порог алерта: $50", msg)

    def test_billing_mode_uses_billing_values(self):
        with patch("openai_monitor.get_total_costs", return_value=(100.0, None)), \
             patch("openai_monitor.get_today_costs", return_value=2.0), \
             patch("openai_monitor.get_billing_balance",
                   return_value={"total_granted": 500.0, "total_used": 123.0,
                                 "remaining": 377.0}):
            msg = openai_monitor.get_status_message()
        self.assertIn("$377.0", msg)
        self.assertIn("(авто)", msg)
        self.assertIn("$500.0", msg)

    def test_all_sources_fail_returns_error_message(self):
        with patch("openai_monitor.get_total_costs", return_value=(None, "HTTP 500")), \
             patch("openai_monitor.get_billing_balance", return_value=None):
            msg = openai_monitor.get_status_message()
        self.assertIn("Не удалось получить данные", msg)
        self.assertIn("HTTP 500", msg)


# ───────────────────────────────── check_and_alert ─────────────────────────────

class CheckAndAlertTests(StateIsolationMixin, unittest.TestCase):
    def _run(self, total_spent=50.0, total_deposited=None, threshold=100,
             state_extras=None, telegram_ok=True):
        base = {"total_deposited": total_deposited
                if total_deposited is not None
                else openai_monitor.DEFAULT_TOTAL_DEPOSITED}
        if threshold is not None:
            base["alert_threshold"] = threshold
        if state_extras:
            base.update(state_extras)
        self.write_state(base)

        with patch("openai_monitor.get_total_costs",
                   return_value=(total_spent, None)), \
             patch("openai_monitor.get_today_costs", return_value=1.0), \
             patch("openai_monitor.get_billing_balance", return_value=None), \
             patch("openai_monitor.send_telegram_alert",
                   return_value=telegram_ok) as mock_tg:
            remaining = openai_monitor.check_and_alert()
        return remaining, mock_tg, self.read_state()

    def test_above_threshold_no_alert(self):
        remaining, mock_tg, state = self._run(
            total_spent=50, total_deposited=1000, threshold=100)
        self.assertEqual(remaining, 950.0)
        mock_tg.assert_not_called()
        self.assertNotIn("last_alert_level", state)

    def test_below_threshold_first_time_sends_alert(self):
        remaining, mock_tg, state = self._run(
            total_spent=950, total_deposited=1000, threshold=100)
        self.assertEqual(remaining, 50.0)
        mock_tg.assert_called_once()
        msg = mock_tg.call_args.args[0]
        self.assertIn("Алерт", msg)
        self.assertIn("$50", msg)
        self.assertEqual(state["last_alert_level"], 50)  # 50 // 10 * 10

    def test_same_alert_level_not_duplicated(self):
        # already alerted at level 50, new balance still in [50, 59] — same level
        remaining, mock_tg, state = self._run(
            total_spent=945, total_deposited=1000, threshold=100,
            state_extras={"last_alert_level": 50})
        self.assertEqual(remaining, 55.0)
        mock_tg.assert_not_called()
        self.assertEqual(state["last_alert_level"], 50)

    def test_next_alert_step_sends_again(self):
        # alerted at 50, balance falls to 35 → level 30 < 50 → new alert
        _, mock_tg, state = self._run(
            total_spent=965, total_deposited=1000, threshold=100,
            state_extras={"last_alert_level": 50})
        mock_tg.assert_called_once()
        self.assertEqual(state["last_alert_level"], 30)

    def test_negative_balance_still_alerts(self):
        _, mock_tg, state = self._run(
            total_spent=1200, total_deposited=1000, threshold=100)
        mock_tg.assert_called_once()
        # -200 // 10 * 10 = -200
        self.assertEqual(state["last_alert_level"], -200)
        # Message formats negative balance as "$-200"
        self.assertIn("$-200", mock_tg.call_args.args[0])

    def test_recovery_above_threshold_clears_last_alert_level(self):
        _, mock_tg, state = self._run(
            total_spent=50, total_deposited=1000, threshold=100,
            state_extras={"last_alert_level": 20})
        mock_tg.assert_not_called()
        self.assertNotIn("last_alert_level", state)

    def test_missing_env_vars_aborts_before_api(self):
        openai_monitor.CONFIG["openai_admin_key"] = ""
        with patch("openai_monitor.get_total_costs") as mock_costs, \
             patch("openai_monitor.send_telegram_alert") as mock_tg:
            result = openai_monitor.check_and_alert()
        self.assertIsNone(result)
        mock_costs.assert_not_called()
        mock_tg.assert_not_called()

    def test_costs_api_failure_does_not_crash_and_returns_none(self):
        self.write_state({"total_deposited": 1000, "alert_threshold": 100})
        with patch("openai_monitor.get_total_costs",
                   return_value=(None, "HTTP 500")), \
             patch("openai_monitor.get_billing_balance", return_value=None), \
             patch("openai_monitor.send_telegram_alert") as mock_tg:
            result = openai_monitor.check_and_alert()
        self.assertIsNone(result)
        mock_tg.assert_not_called()

    def test_billing_source_bypasses_manual_formula(self):
        self.write_state({"total_deposited": 1000, "alert_threshold": 100})
        with patch("openai_monitor.get_billing_balance",
                   return_value={"total_granted": 500.0, "total_used": 480.0,
                                 "remaining": 20.0}), \
             patch("openai_monitor.get_today_costs", return_value=1.0), \
             patch("openai_monitor.send_telegram_alert", return_value=True) as mock_tg:
            remaining = openai_monitor.check_and_alert()
        self.assertEqual(remaining, 20.0)
        mock_tg.assert_called_once()


# ─────────────────────────────── send_status_report ────────────────────────────

class SendStatusReportTests(StateIsolationMixin, unittest.TestCase):
    def _common_patches(self, remaining=100, forecast=10, chart_bytes=b"\x89PNG..."):
        """Build a common patch setup — returns stack as list of contextmanagers."""
        return [
            patch("openai_monitor.get_total_costs", return_value=(50.0, None)),
            patch("openai_monitor.get_today_costs", return_value=5.0),
            patch("openai_monitor.get_billing_balance", return_value=None),
            patch("openai_monitor.forecast_days_remaining", return_value=forecast),
            patch("openai_monitor.get_daily_costs",
                  return_value=[(date_type(2026, 4, 18), 5.0)]),
            patch("openai_monitor.get_month_line_item_costs",
                  return_value=[("gpt-5.2-2025-12-11, input", 25.0)]),
        ]

    def test_sends_chart_on_success(self):
        self.write_state({"total_deposited": 150, "alert_threshold": 100,
                          "topup_history": []})
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch("charts.build_status_chart", return_value=b"\x89PNG-FAKE"), \
             patch("openai_monitor.send_telegram_photo",
                   return_value=True) as mock_photo, \
             patch("openai_monitor.send_telegram_alert") as mock_alert:
            openai_monitor.send_status_report()
        mock_photo.assert_called_once()
        # caption must include balance + forecast
        caption = mock_photo.call_args.kwargs["caption"]
        self.assertIn("100", caption)  # remaining = 150-50
        self.assertIn("10 дн", caption)
        mock_alert.assert_not_called()

    def test_falls_back_to_text_when_chart_build_raises(self):
        self.write_state({"total_deposited": 150, "alert_threshold": 100})
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch("charts.build_status_chart",
                   side_effect=RuntimeError("matplotlib died")), \
             patch("openai_monitor.send_telegram_photo") as mock_photo, \
             patch("openai_monitor.send_telegram_alert",
                   return_value=True) as mock_alert:
            openai_monitor.send_status_report()
        mock_photo.assert_not_called()
        mock_alert.assert_called_once()

    def test_falls_back_to_text_when_sendPhoto_fails(self):
        self.write_state({"total_deposited": 150, "alert_threshold": 100})
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch("charts.build_status_chart", return_value=b"\x89PNG"), \
             patch("openai_monitor.send_telegram_photo",
                   return_value=False) as mock_photo, \
             patch("openai_monitor.send_telegram_alert",
                   return_value=True) as mock_alert:
            openai_monitor.send_status_report()
        mock_photo.assert_called_once()
        mock_alert.assert_called_once()

    def test_warning_emoji_when_forecast_low(self):
        self.write_state({"total_deposited": 150, "alert_threshold": 100})
        patches = self._common_patches(forecast=3)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch("charts.build_status_chart", return_value=b"\x89PNG"), \
             patch("openai_monitor.send_telegram_photo",
                   return_value=True) as mock_photo:
            openai_monitor.send_status_report()
        caption = mock_photo.call_args.kwargs["caption"]
        self.assertIn("⚠️", caption)
        self.assertIn("3 дн", caption)

    def test_missing_env_aborts(self):
        openai_monitor.CONFIG["openai_admin_key"] = ""
        with patch("openai_monitor.get_status_message") as mock_msg, \
             patch("openai_monitor.get_total_costs") as mock_costs, \
             patch("openai_monitor.send_telegram_alert") as mock_tg:
            openai_monitor.send_status_report()
        mock_msg.assert_not_called()
        mock_costs.assert_not_called()
        mock_tg.assert_not_called()


# ───────────────────────────────── main routing ────────────────────────────────

class MainRoutingTests(unittest.TestCase):
    def test_bot_flag_invokes_run_bot(self):
        with patch("openai_monitor.run_bot") as mock_bot, \
             patch.object(sys, "argv", ["openai_monitor.py", "--bot"]):
            openai_monitor.main()
        mock_bot.assert_called_once()

    def test_status_flag_invokes_status_report(self):
        with patch("openai_monitor.send_status_report") as mock_st, \
             patch.object(sys, "argv", ["openai_monitor.py", "--status"]):
            openai_monitor.main()
        mock_st.assert_called_once()

    def test_backup_flag_invokes_backup(self):
        with patch("openai_monitor.backup_state") as mock_bk, \
             patch.object(sys, "argv", ["openai_monitor.py", "--backup"]):
            openai_monitor.main()
        mock_bk.assert_called_once()

    def test_topup_flag_with_valid_amount(self):
        with patch("openai_monitor.topup") as mock_tu, \
             patch.object(sys, "argv", ["openai_monitor.py", "--topup", "250.5"]):
            openai_monitor.main()
        mock_tu.assert_called_once_with(250.5)

    def test_topup_flag_with_invalid_amount_does_not_call_topup(self):
        with patch("openai_monitor.topup") as mock_tu, \
             patch.object(sys, "argv", ["openai_monitor.py", "--topup", "abc"]):
            openai_monitor.main()
        mock_tu.assert_not_called()

    def test_no_flag_runs_check_and_alert(self):
        with patch("openai_monitor.check_and_alert") as mock_ca, \
             patch.object(sys, "argv", ["openai_monitor.py"]):
            openai_monitor.main()
        mock_ca.assert_called_once()


# ──────────────────────────────── get_daily_costs ──────────────────────────────

class GetDailyCostsTests(StateIsolationMixin, unittest.TestCase):
    def _bucket(self, ts, amount):
        return {"start_time": ts,
                "results": [{"amount": {"value": str(amount), "currency": "usd"}}]}

    def test_fills_missing_days_with_zero(self):
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        # Return only 1 bucket for yesterday (value 5); expect 3 days, other two = 0
        yesterday_ts = int((today - timedelta(days=1)).timestamp())
        payload = {"data": [self._bucket(yesterday_ts, 5.0)], "has_more": False}
        resp = MagicMock(); resp.status_code = 200; resp.json.return_value = payload
        with patch("openai_monitor.requests.get", return_value=resp):
            daily = openai_monitor.get_daily_costs(3)
        self.assertEqual(len(daily), 3)
        dates = [d for d, _ in daily]
        self.assertEqual(dates, sorted(dates))  # ascending
        amounts = [v for _, v in daily]
        # exactly one day has value 5.0, others are 0
        self.assertEqual(sorted(amounts, reverse=True), [5.0, 0, 0])

    def test_returns_empty_on_http_error(self):
        resp = MagicMock(); resp.status_code = 500; resp.text = "server error"
        with patch("openai_monitor.requests.get", return_value=resp):
            daily = openai_monitor.get_daily_costs(7)
        self.assertEqual(daily, [])

    def test_returns_empty_on_network_error(self):
        with patch("openai_monitor.requests.get",
                   side_effect=requests.ConnectionError("no")):
            self.assertEqual(openai_monitor.get_daily_costs(7), [])


# ─────────────────────────────── get_line_item_costs ───────────────────────────

class GetLineItemCostsTests(StateIsolationMixin, unittest.TestCase):
    def _page(self, items, has_more=False, next_page=None):
        return {
            "has_more": has_more,
            "next_page": next_page,
            "data": [
                {"results": [
                    {"line_item": li, "amount": {"value": str(v), "currency": "usd"}}
                    for li, v in items
                ]}
            ],
        }

    def test_aggregates_and_sorts_desc(self):
        p1 = self._page([("gpt-5.2, input", 10), ("whisper-1, audio", 3),
                         ("gpt-5.2, input", 5)])
        resp = MagicMock(); resp.status_code = 200; resp.json.return_value = p1
        with patch("openai_monitor.requests.get", return_value=resp):
            out = openai_monitor.get_line_item_costs(1000)
        # two items, gpt-5.2 total 15, whisper-1 total 3
        self.assertEqual(out[0], ("gpt-5.2, input", 15.0))
        self.assertEqual(out[1], ("whisper-1, audio", 3.0))

    def test_paginates(self):
        p1 = self._page([("m1, input", 10)], has_more=True, next_page="p2")
        p2 = self._page([("m2, input", 20)])
        responses = []
        for p in (p1, p2):
            r = MagicMock(); r.status_code = 200; r.json.return_value = p
            responses.append(r)
        with patch("openai_monitor.requests.get", side_effect=responses):
            out = openai_monitor.get_line_item_costs(1000)
        self.assertEqual(dict(out), {"m1, input": 10.0, "m2, input": 20.0})

    def test_stops_on_http_error(self):
        resp = MagicMock(); resp.status_code = 500
        with patch("openai_monitor.requests.get", return_value=resp):
            self.assertEqual(openai_monitor.get_line_item_costs(1000), [])


# ───────────────────────────── forecast_days_remaining ─────────────────────────

class ForecastDaysRemainingTests(StateIsolationMixin, unittest.TestCase):
    def test_returns_none_when_no_daily_data(self):
        with patch("openai_monitor.get_daily_costs", return_value=[]):
            self.assertIsNone(openai_monitor.forecast_days_remaining(100))

    def test_returns_none_when_total_zero(self):
        today = date_type.today()
        daily = [(today - timedelta(days=i), 0) for i in range(7)]
        with patch("openai_monitor.get_daily_costs", return_value=daily):
            self.assertIsNone(openai_monitor.forecast_days_remaining(100))

    def test_returns_zero_when_balance_zero_or_negative(self):
        with patch("openai_monitor.get_daily_costs") as mock_d:
            self.assertEqual(openai_monitor.forecast_days_remaining(0), 0)
            self.assertEqual(openai_monitor.forecast_days_remaining(-10), 0)
        mock_d.assert_not_called()

    def test_returns_int_estimate(self):
        today = date_type.today()
        # 7 days, $10/day → avg 10
        daily = [(today - timedelta(days=i), 10) for i in range(7)]
        with patch("openai_monitor.get_daily_costs", return_value=daily):
            self.assertEqual(openai_monitor.forecast_days_remaining(100), 10)

    def test_returns_none_for_none_remaining(self):
        self.assertIsNone(openai_monitor.forecast_days_remaining(None))


# ─────────────────────────────── parse topup date ──────────────────────────────

class ParseTopupDateTests(unittest.TestCase):
    def test_actual_datetime_with_time(self):
        d = openai_monitor._parse_topup_date(
            {"actual_datetime": "01.02.2026 14:30"})
        self.assertEqual(d, date_type(2026, 2, 1))

    def test_date_only(self):
        d = openai_monitor._parse_topup_date({"date": "15.04.2026"})
        self.assertEqual(d, date_type(2026, 4, 15))

    def test_date_with_time_fallback_format(self):
        d = openai_monitor._parse_topup_date({"date": "15.04.2026 10:00"})
        self.assertEqual(d, date_type(2026, 4, 15))

    def test_invalid_returns_none(self):
        self.assertIsNone(openai_monitor._parse_topup_date({"date": "garbage"}))

    def test_empty_returns_none(self):
        self.assertIsNone(openai_monitor._parse_topup_date({}))


# ────────────────────────────────── backup_state ───────────────────────────────

class BackupStateTests(StateIsolationMixin, unittest.TestCase):
    def test_sends_state_file_as_document(self):
        self.write_state({"total_deposited": 100})
        with patch("openai_monitor.send_telegram_document",
                   return_value=True) as mock_send:
            openai_monitor.backup_state()
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        args = mock_send.call_args.args
        sent_bytes = args[0] if args else kwargs.get("document_bytes")
        self.assertIn(b"total_deposited", sent_bytes)

    def test_missing_env_aborts(self):
        openai_monitor.CONFIG["telegram_bot_token"] = ""
        with patch("openai_monitor.send_telegram_document") as mock_send:
            openai_monitor.backup_state()
        mock_send.assert_not_called()

    def test_missing_state_file_aborts(self):
        # No write_state call → file does not exist
        with patch("openai_monitor.send_telegram_document") as mock_send:
            openai_monitor.backup_state()
        mock_send.assert_not_called()

    def test_filename_contains_timestamp(self):
        self.write_state({"x": 1})
        with patch("openai_monitor.send_telegram_document",
                   return_value=True) as mock_send:
            openai_monitor.backup_state()
        filename = mock_send.call_args.kwargs["filename"]
        self.assertTrue(filename.startswith("monitor_state_"))
        self.assertTrue(filename.endswith(".json"))


if __name__ == "__main__":
    unittest.main()
