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


BG = "#f8fafc"
PANEL = "#ffffff"
TEXT = "#111827"
MUTED = "#6b7280"
GRID = "#d1d5db"
BLUE = "#2563eb"
GREEN = "#059669"
ORANGE = "#f97316"
RED = "#dc2626"


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


def _png_bytes(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _format_date_axis(ax):
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))


def _style_figure(fig, ax):
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#cbd5e1")
    ax.tick_params(colors=MUTED)
    ax.yaxis.label.set_color(MUTED)
    ax.xaxis.label.set_color(MUTED)
    ax.title.set_color(TEXT)


def build_daily_spend_chart(daily_costs, title_suffix=""):
    """Build a readable standalone daily spend chart PNG."""
    fig, ax = plt.subplots(figsize=(11, 5.5))
    _style_figure(fig, ax)
    if daily_costs:
        dates = [d for d, _ in daily_costs]
        values = [float(v) for _, v in daily_costs]
        bars = ax.bar(
            dates, values, color=BLUE, width=0.78, alpha=0.92, label="Daily spend"
        )

        if len(values) >= 7:
            ma = []
            for i in range(len(values)):
                lo = max(0, i - 6)
                ma.append(sum(values[lo:i + 1]) / (i - lo + 1))
            ax.plot(dates, ma, color=ORANGE, linewidth=3, label="7-day avg")
            ax.legend(loc="upper left", fontsize=11, frameon=False)

        max_value = max(values) if values else 0
        if max_value > 0:
            max_idx = values.index(max_value)
            bar = bars[max_idx]
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max_value * 0.035,
                f"${max_value:.2f}",
                ha="center",
                va="bottom",
                fontsize=10,
                color=TEXT,
                fontweight="bold",
            )
        ax.set_ylim(0, max(1, max_value * 1.18))
        _format_date_axis(ax)
    else:
        ax.text(0.5, 0.5, "no daily spend data", ha="center", va="center",
                transform=ax.transAxes, color=MUTED, fontsize=14)

    title = "Daily spend ($)"
    if title_suffix:
        title += f" · {title_suffix}"
    ax.set_title(title, fontsize=18, fontweight="bold", pad=14)
    ax.set_ylabel("$", fontsize=13)
    ax.tick_params(axis="both", labelsize=11)
    ax.grid(True, color=GRID, alpha=0.45, axis="y", linewidth=0.8)
    fig.autofmt_xdate(rotation=30, ha="right")
    return _png_bytes(fig)


def build_balance_chart(
    daily_costs,
    topup_events,
    current_balance,
    alert_threshold,
    forecast_days=None,
    title_suffix="",
):
    """Build a readable standalone balance/forecast chart PNG."""
    fig, ax = plt.subplots(figsize=(11, 5.5))
    _style_figure(fig, ax)
    timeline = reconstruct_balance_timeline(daily_costs, topup_events, current_balance)
    if timeline:
        dates = [d for d, _ in timeline]
        balances = [b for _, b in timeline]
        ax.plot(dates, balances, color=GREEN, linewidth=3.2, label="Balance")
        ax.fill_between(dates, balances, alpha=0.13, color=GREEN)
        ax.scatter(dates[-1], balances[-1], s=85, color=GREEN, zorder=3)
        ax.annotate(
            f"${balances[-1]:.2f}",
            xy=(dates[-1], balances[-1]),
            xytext=(-8, 12),
            textcoords="offset points",
            ha="right",
            fontsize=12,
            color=TEXT,
            fontweight="bold",
        )

        for d, amt in topup_events:
            if dates[0] <= d <= dates[-1]:
                ax.axvline(d, color=BLUE, linestyle=":", alpha=0.7, linewidth=1.5)
                ax.annotate(
                    f"+${amt:g}",
                    xy=(d, max(balances) if balances else current_balance),
                    fontsize=10,
                    color=BLUE,
                    rotation=90,
                    va="top",
                )

        if forecast_days is not None and forecast_days > 0:
            last_date = timeline[-1][0]
            eol_date = last_date + timedelta(days=forecast_days)
            ax.plot(
                [last_date, eol_date], [current_balance, 0],
                color=RED, linestyle="--", linewidth=2,
                label=f"Forecast: {forecast_days} day(s)",
            )
            ax.annotate("$0", xy=(eol_date, 0), fontsize=11, color=RED,
                        va="bottom", ha="right")
        _format_date_axis(ax)
    else:
        ax.text(0.5, 0.5, "no balance timeline data", ha="center", va="center",
                transform=ax.transAxes, color=MUTED, fontsize=14)

    ax.axhline(alert_threshold, color=RED, linestyle="--", linewidth=1.6,
               label=f"Alert threshold (${alert_threshold:g})")
    ax.axhline(0, color=MUTED, linewidth=0.8)

    title = "Balance over time"
    if title_suffix:
        title += f" · {title_suffix}"
    ax.set_title(title, fontsize=18, fontweight="bold", pad=14)
    ax.set_ylabel("$", fontsize=13)
    ax.tick_params(axis="both", labelsize=11)
    ax.grid(True, color=GRID, alpha=0.45, linewidth=0.8)
    ax.legend(loc="best", fontsize=11, frameon=False)
    fig.autofmt_xdate(rotation=30, ha="right")
    return _png_bytes(fig)


def build_model_breakdown_chart(line_item_costs, title_suffix=""):
    """Build a readable standalone per-model spend chart PNG."""
    models = aggregate_models(line_item_costs)
    fig_h = max(4.8, min(8.5, 1.0 + 0.55 * max(len(models), 1)))
    fig, ax = plt.subplots(figsize=(11, fig_h))
    _style_figure(fig, ax)
    if models:
        top = models[:10]
        other = sum(v for _, v in models[10:])
        if other > 0:
            top.append(("other", other))

        labels = [m for m, _ in top][::-1]
        values = [float(v) for _, v in top][::-1]
        colors = [BLUE] * len(values)
        if values:
            colors[-1] = GREEN
        bars = ax.barh(labels, values, color=colors, alpha=0.9)
        max_value = max(values) if values else 0
        ax.set_xlim(0, max(1, max_value * 1.2))
        for bar, value in zip(bars, values):
            ax.text(
                bar.get_width() + max_value * 0.015,
                bar.get_y() + bar.get_height() / 2,
                f"${value:.2f}",
                va="center",
                fontsize=11,
                color=TEXT,
                fontweight="bold" if value == max_value else "normal",
            )
        ax.set_xlabel("$", fontsize=13)
        total = sum(v for _, v in models)
        title = f"Spend by model · total ${total:.2f}"
    else:
        ax.text(0.5, 0.5, "no model spend data", ha="center", va="center",
                transform=ax.transAxes, color=MUTED, fontsize=14)
        title = "Spend by model"

    if title_suffix:
        title += f" · {title_suffix}"
    ax.set_title(title, fontsize=18, fontweight="bold", pad=14)
    ax.tick_params(axis="both", labelsize=11)
    ax.grid(True, color=GRID, alpha=0.45, axis="x", linewidth=0.8)
    return _png_bytes(fig)


def build_selectel_balance_chart(history, threshold, currency="RUB", title_suffix=""):
    """Build a standalone Selectel balance history chart PNG."""
    fig, ax = plt.subplots(figsize=(11, 5.5))
    _style_figure(fig, ax)

    points = []
    for item in history:
        try:
            ts = item.get("ts")
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(ts))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            points.append((dt, float(item["balance"])))
        except (KeyError, TypeError, ValueError):
            continue
    points.sort(key=lambda x: x[0])

    if points:
        dates = [d for d, _ in points]
        values = [v for _, v in points]
        color = GREEN if values[-1] > threshold else RED
        ax.plot(dates, values, color=color, linewidth=3.2, label="Баланс")
        ax.fill_between(dates, values, threshold, color=color, alpha=0.12)
        ax.scatter(dates[-1], values[-1], s=90, color=color, zorder=3)
        ax.annotate(
            f"{values[-1]:,.2f} {currency}".replace(",", " "),
            xy=(dates[-1], values[-1]),
            xytext=(-8, 14),
            textcoords="offset points",
            ha="right",
            fontsize=12,
            color=TEXT,
            fontweight="bold",
        )
        if len(dates) == 1 or (dates[-1] - dates[0]) < timedelta(hours=2):
            center = dates[-1]
            ax.set_xlim(center - timedelta(hours=12), center + timedelta(hours=12))
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M\n%d.%m"))
        else:
            _format_date_axis(ax)

        lo = min(min(values), threshold)
        hi = max(max(values), threshold)
        pad = max((hi - lo) * 0.2, threshold * 0.05, 1000)
        ax.set_ylim(max(0, lo - pad), hi + pad)
    else:
        ax.text(0.5, 0.5, "нет истории баланса", ha="center", va="center",
                transform=ax.transAxes, color=MUTED, fontsize=14)

    ax.axhline(threshold, color=RED, linestyle="--", linewidth=1.8,
               label=f"Порог {threshold:,.0f} {currency}".replace(",", " "))
    title = "Selectel: баланс"
    if title_suffix:
        title += f" · {title_suffix}"
    ax.set_title(title, fontsize=18, fontweight="bold", pad=14)
    ax.set_ylabel(currency, fontsize=13)
    ax.tick_params(axis="both", labelsize=11)
    ax.grid(True, color=GRID, alpha=0.45, linewidth=0.8)
    ax.legend(loc="best", fontsize=11, frameon=False)
    fig.autofmt_xdate(rotation=30, ha="right")
    return _png_bytes(fig)


def build_status_charts(
    daily_costs,
    topup_events,
    line_item_costs,
    current_balance,
    alert_threshold,
    forecast_days=None,
    title_suffix="",
):
    """Build separate readable status chart PNGs."""
    return [
        ("daily-spend.png", build_daily_spend_chart(daily_costs, title_suffix)),
        (
            "balance.png",
            build_balance_chart(
                daily_costs,
                topup_events,
                current_balance,
                alert_threshold,
                forecast_days=forecast_days,
                title_suffix=title_suffix,
            ),
        ),
        ("model-spend.png", build_model_breakdown_chart(line_item_costs, title_suffix)),
    ]


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
