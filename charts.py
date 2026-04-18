"""Charts for the OpenAI monitor daily status report.

Returns PNG bytes — to be sent via Telegram sendPhoto. Pure data-in / bytes-out;
no I/O besides matplotlib rendering, so easy to unit-test.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")  # headless; never tries to open a display

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402


# ─────────────────────────────── helpers ──────────────────────────────────────


def _extract_model(line_item: str) -> str:
    """From 'gpt-5.2-2025-12-11, input' → 'gpt-5.2'. Falls back to the raw item.

    Drops the trailing date (YYYY-MM-DD) and the ', input|output|...' suffix.
    """
    if not line_item:
        return "unknown"
    head = line_item.split(",")[0].strip()
    # strip trailing YYYY-MM-DD
    head = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", head)
    return head or "unknown"


def aggregate_models(line_item_costs):
    """[(line_item, amount), ...] → [(model, total_amount), ...] sorted desc."""
    totals = {}
    for item, amount in line_item_costs:
        model = _extract_model(item)
        totals[model] = totals.get(model, 0) + float(amount)
    return sorted(totals.items(), key=lambda x: x[1], reverse=True)


def reconstruct_balance_timeline(daily_costs, topup_events, current_balance):
    """Walk forward through time to produce (date, balance_at_eod) series.

    Inputs:
      daily_costs:  [(date, amount)] sorted ascending
      topup_events: [(date, amount)] — topups credited on that date
      current_balance: balance at end-of-range (the known truth point)

    Strategy: sum(topups) - sum(costs) across the range gives net delta; the
    balance at the start is current_balance - net_delta. Then we iterate day
    by day, adjusting. Guarantees the timeline ends exactly at current_balance.
    """
    if not daily_costs:
        return []

    topup_by_date = {}
    for d, amt in topup_events:
        topup_by_date[d] = topup_by_date.get(d, 0) + float(amt)

    net_delta = sum(amt for _, amt in topup_events) - sum(c for _, c in daily_costs)
    start_balance = current_balance - net_delta

    series = []
    bal = start_balance
    for d, cost in daily_costs:
        bal += topup_by_date.get(d, 0)  # topup happens at start of day
        bal -= float(cost)  # all costs of the day
        series.append((d, round(bal, 2)))
    return series


# ─────────────────────────────── chart ────────────────────────────────────────


def build_status_chart(
    daily_costs,
    topup_events,
    line_item_costs,
    current_balance,
    alert_threshold,
    forecast_days=None,
    title_suffix="",
):
    """Build a 3-panel status chart PNG.

    Args:
      daily_costs: list of (date, amount) for the last N days, ascending.
      topup_events: list of (date, amount) for topups within the same window.
      line_item_costs: list of (line_item_string, amount) for the month.
      current_balance: float — remaining balance NOW (used for forecast line).
      alert_threshold: float — horizontal alert line on panel 2.
      forecast_days: optional int — overlay "runs out in N days" as dashed line.
      title_suffix: optional string appended to the figure title.

    Returns:
      bytes — PNG image.
    """
    fig, axs = plt.subplots(
        3, 1,
        figsize=(10, 11),
        gridspec_kw={"height_ratios": [1, 1.1, 1.1]},
    )
    fig.suptitle(
        f"OpenAI API — Daily Status{(' · ' + title_suffix) if title_suffix else ''}",
        fontsize=13,
        fontweight="bold",
    )

    # ── Panel 1: daily spending bars + 7-day moving avg ───────────────────────
    ax1 = axs[0]
    if daily_costs:
        dates = [d for d, _ in daily_costs]
        values = [float(v) for _, v in daily_costs]
        ax1.bar(dates, values, color="#3b82f6", width=0.8, alpha=0.85, label="Daily spend")

        # 7-day moving average
        if len(values) >= 7:
            ma = []
            for i in range(len(values)):
                lo = max(0, i - 6)
                ma.append(sum(values[lo:i + 1]) / (i - lo + 1))
            ax1.plot(dates, ma, color="#f97316", linewidth=2, label="7-day avg")
        ax1.legend(loc="upper left", fontsize=9)
    ax1.set_title("Daily spend ($) — last {} days".format(len(daily_costs)), fontsize=11)
    ax1.set_ylabel("$")
    ax1.grid(True, alpha=0.3, axis="y")
    ax1.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate(rotation=30, ha="right")

    # ── Panel 2: balance over time + alert threshold + forecast ───────────────
    ax2 = axs[1]
    timeline = reconstruct_balance_timeline(daily_costs, topup_events, current_balance)
    if timeline:
        ts = [d for d, _ in timeline]
        bals = [b for _, b in timeline]
        ax2.plot(ts, bals, color="#16a34a", linewidth=2, label="Balance")
        ax2.fill_between(ts, bals, alpha=0.15, color="#16a34a")

        # topup annotations
        for d, amt in topup_events:
            if ts and ts[0] <= d <= ts[-1]:
                ax2.axvline(d, color="#0ea5e9", linestyle=":", alpha=0.6, linewidth=1)
                ax2.annotate(
                    f"+${amt:g}",
                    xy=(d, current_balance * 0.95 if current_balance > 0 else 100),
                    fontsize=8,
                    color="#0ea5e9",
                    rotation=90,
                    va="top",
                )

    ax2.axhline(alert_threshold, color="#dc2626", linestyle="--", linewidth=1.2,
                label=f"Alert threshold (${alert_threshold:g})")
    ax2.axhline(0, color="#6b7280", linestyle="-", linewidth=0.6)

    # forecast extension
    if forecast_days is not None and forecast_days > 0 and timeline:
        last_date = timeline[-1][0]
        eol_date = last_date + timedelta(days=forecast_days)
        ax2.plot(
            [last_date, eol_date], [current_balance, 0],
            color="#dc2626", linestyle="--", linewidth=1.5, alpha=0.8,
            label=f"Forecast: {forecast_days} day(s) left",
        )
        ax2.annotate(
            "→ $0",
            xy=(eol_date, 0),
            fontsize=9,
            color="#dc2626",
            va="bottom",
            ha="right",
        )

    ax2.set_title("Balance over time", fontsize=11)
    ax2.set_ylabel("$")
    ax2.grid(True, alpha=0.3)
    ax2.legend(loc="upper right", fontsize=9)
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))

    # ── Panel 3: per-model breakdown (pie with a tail for "other") ────────────
    ax3 = axs[2]
    models = aggregate_models(line_item_costs)
    if models:
        # top-6 models, rest aggregated as "other"
        top = models[:6]
        other = sum(v for _, v in models[6:])
        if other > 0:
            top.append(("other", other))
        labels = [f"{m}\n${v:.2f}" for m, v in top]
        values = [v for _, v in top]

        colors = ["#3b82f6", "#16a34a", "#f97316", "#a855f7", "#eab308",
                  "#ec4899", "#6b7280"]
        ax3.pie(
            values,
            labels=labels,
            colors=colors[:len(values)],
            autopct="%1.0f%%",
            startangle=90,
            textprops={"fontsize": 9},
            pctdistance=0.75,
        )
        ax3.set_title(
            "Spend breakdown by model (total ${:.2f})".format(sum(values)),
            fontsize=11,
        )
    else:
        ax3.text(0.5, 0.5, "no spend data", ha="center", va="center",
                 transform=ax3.transAxes, color="#6b7280")
        ax3.set_title("Spend breakdown by model", fontsize=11)
        ax3.axis("off")

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
