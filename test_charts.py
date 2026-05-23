"""Unit tests for charts module."""

import unittest
from datetime import date, timedelta

import charts


class ExtractModelTests(unittest.TestCase):
    def test_strips_date_suffix(self):
        self.assertEqual(charts._extract_model("gpt-5.2-2025-12-11, input"), "gpt-5.2")

    def test_strips_token_type_suffix(self):
        self.assertEqual(charts._extract_model("whisper-1, audio"), "whisper-1")

    def test_handles_no_comma(self):
        self.assertEqual(charts._extract_model("some-model"), "some-model")

    def test_handles_empty(self):
        self.assertEqual(charts._extract_model(""), "unknown")
        self.assertEqual(charts._extract_model(None), "unknown")

    def test_multi_dash_model(self):
        self.assertEqual(
            charts._extract_model("text-embedding-3-large-2024-01-25, input"),
            "text-embedding-3-large",
        )


class AggregateModelsTests(unittest.TestCase):
    def test_groups_by_model_and_sorts_desc(self):
        line_items = [
            ("gpt-5.2-2025-12-11, input", 10.0),
            ("gpt-5.2-2025-12-11, output", 5.0),
            ("whisper-1, audio", 2.0),
            ("gpt-5.2-2025-12-11, cached input", 3.0),
        ]
        result = charts.aggregate_models(line_items)
        self.assertEqual(result[0], ("gpt-5.2", 18.0))
        self.assertEqual(result[1], ("whisper-1", 2.0))

    def test_empty_input(self):
        self.assertEqual(charts.aggregate_models([]), [])


class ReconstructBalanceTimelineTests(unittest.TestCase):
    def test_ends_at_current_balance(self):
        today = date.today()
        daily = [(today - timedelta(days=2), 10),
                 (today - timedelta(days=1), 20),
                 (today, 30)]
        topups = [(today - timedelta(days=2), 100)]
        timeline = charts.reconstruct_balance_timeline(daily, topups, current_balance=40)
        self.assertEqual(timeline[-1][1], 40.0)

    def test_topup_raises_balance_that_day(self):
        today = date.today()
        daily = [(today - timedelta(days=1), 10), (today, 5)]
        topups = [(today, 50)]
        timeline = charts.reconstruct_balance_timeline(daily, topups, current_balance=60)
        # yesterday: start_balance → -10 → balance t-1
        # today: +50 (topup) → -5 → 60 (known)
        # so day before topup should be 60 - 50 + 5 = 15
        self.assertEqual(timeline[0][1], 15.0)
        self.assertEqual(timeline[1][1], 60.0)

    def test_empty_daily_returns_empty(self):
        self.assertEqual(
            charts.reconstruct_balance_timeline([], [(date.today(), 100)], 100),
            [],
        )


class BuildStatusChartSmokeTests(unittest.TestCase):
    def test_returns_png_bytes(self):
        today = date.today()
        daily = [(today - timedelta(days=29 - i), 5 + (i % 7)) for i in range(30)]
        topups = [(today - timedelta(days=10), 200)]
        line_items = [
            ("gpt-5.2-2025-12-11, input", 50.0),
            ("gpt-5.2-2025-12-11, output", 40.0),
            ("whisper-1, audio", 5.0),
        ]
        png = charts.build_status_chart(
            daily_costs=daily,
            topup_events=topups,
            line_item_costs=line_items,
            current_balance=120,
            alert_threshold=50,
            forecast_days=10,
        )
        self.assertIsInstance(png, bytes)
        self.assertTrue(len(png) > 1000)
        # PNG signature
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_handles_empty_daily_costs(self):
        png = charts.build_status_chart(
            daily_costs=[],
            topup_events=[],
            line_item_costs=[],
            current_balance=0,
            alert_threshold=100,
            forecast_days=None,
        )
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_handles_zero_forecast(self):
        today = date.today()
        daily = [(today, 10)]
        png = charts.build_status_chart(
            daily_costs=daily,
            topup_events=[],
            line_item_costs=[("gpt-5.2-2025-12-11, input", 10)],
            current_balance=-5,
            alert_threshold=0,
            forecast_days=0,
        )
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_build_status_charts_returns_three_named_pngs(self):
        today = date.today()
        daily = [(today - timedelta(days=2 - i), 5 + i) for i in range(3)]
        out = charts.build_status_charts(
            daily_costs=daily,
            topup_events=[(today - timedelta(days=1), 100)],
            line_item_costs=[("gpt-5.2-2025-12-11, input", 10)],
            current_balance=50,
            alert_threshold=20,
            forecast_days=5,
        )
        self.assertEqual(
            [name for name, _ in out],
            ["daily-spend.png", "balance.png", "model-spend.png"],
        )
        for _, png in out:
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_build_selectel_balance_chart_returns_png(self):
        today = date.today()
        history = [
            {"ts": (today - timedelta(days=2)).isoformat(), "balance": 120000},
            {"ts": (today - timedelta(days=1)).isoformat(), "balance": 115000},
            {"ts": today.isoformat(), "balance": 112851.66},
        ]
        png = charts.build_selectel_balance_chart(
            history=history,
            threshold=80000,
            currency="RUB",
        )
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
