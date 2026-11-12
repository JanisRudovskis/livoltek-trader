"""ntfy notification client (success summaries, errors, healthcheck alerts).

ntfy.sh accepts plain HTTP POSTs to `https://ntfy.sh/{topic}` with the message
body as the request body and optional metadata in headers (Title, Priority,
Tags, Authorization). User-facing strings here are Latvian; identifiers and
docstrings stay English.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

import httpx

from livoltek_trader.config import Settings, get_settings
from livoltek_trader.ev import EvAdvice
from livoltek_trader.nights import NightOutlook
from livoltek_trader.solar import PvForecast
from livoltek_trader.strategy import DailyPlan, HourlyPrice
from livoltek_trader.telemetry import DailyTotals, format_daily_totals_line

RIGA_TZ = ZoneInfo("Europe/Riga")

# The cron plans TOMORROW, so the day it runs on is target_date - 1. That is
# the reference the actuals line labels itself against.
_ONE_DAY = timedelta(days=1)
_WEEKDAYS = ("P", "O", "T", "C", "Pk", "S", "Sv")


class NtfyError(RuntimeError):
    """Raised when ntfy delivery fails (transport or non-2xx response)."""


class NtfyClient:
    """Thin async client for ntfy.sh-compatible servers."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client

    async def send(
        self,
        message: str,
        *,
        title: str | None = None,
        priority: int = 3,
        tags: Iterable[str] | None = None,
    ) -> None:
        if not self._settings.ntfy_topic:
            raise NtfyError("NTFY_TOPIC is not configured")

        payload: dict = {
            "topic": self._settings.ntfy_topic,
            "message": message,
        }
        if title:
            payload["title"] = title
        if priority != 3:
            payload["priority"] = priority
        if tags:
            tag_list = [t for t in tags if t]
            if tag_list:
                payload["tags"] = tag_list

        url = self._settings.ntfy_base_url.rstrip("/")
        headers: dict[str, str] = {}
        if self._settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self._settings.ntfy_token}"

        async def _post(c: httpx.AsyncClient) -> httpx.Response:
            return await c.post(url, json=payload, headers=headers)

        try:
            if self._client is None:
                async with httpx.AsyncClient(
                    timeout=self._settings.ntfy_timeout_s
                ) as owned:
                    response = await _post(owned)
            else:
                response = await _post(self._client)
        except httpx.HTTPError as exc:
            raise NtfyError(
                f"transport error: {type(exc).__name__}: {exc!r}"
            ) from exc

        if not (200 <= response.status_code < 300):
            raise NtfyError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )


def _fmt_local(dt) -> str:
    return dt.astimezone(RIGA_TZ).strftime("%H:%M")


def format_plan_message(
    plan: DailyPlan,
    *,
    pv_forecast: PvForecast | None = None,
    hourly_prices: list[HourlyPrice] | None = None,
    settings: Settings | None = None,
    yesterday: DailyTotals | None = None,
    ev: EvAdvice | None = None,
    nights: list[NightOutlook] | None = None,
) -> tuple[str, str, list[str]]:
    """Render a daily plan as (title, body, tags) — minimal slot listing.

    Body shows the PV forecast, a flat list of scheduled slots (start–end +
    strategy, Riga local time), the EV windows, the nights-ahead ranking and —
    when available — one line of YESTERDAY'S ACTUALS. Skip days show
    "ToU izslēgts".

    The actuals line exists because the message used to describe only the plan
    and never the outcome. That left battery SOC in the app as the only
    feedback, and SOC is misleading on its own: 30 % against a 0.4 kW load is
    fine, 30 % against a heat pump is not. Grid import in kWh is unambiguous,
    so it goes in every notification.

    `hourly_prices` and `settings` are accepted for backward compatibility;
    only `settings` is still read (for the morning-discharge SOC target).
    """
    has_stop = plan.stop_window is not None
    has_cycles = bool(plan.cycles)

    # Morning Discharge SOC floor (displayed alongside the strategy label so
    # users can distinguish it from the normal full-charge cycle slots).
    morning_soc = (
        settings.morning_discharge_target_soc_pct if settings is not None else 15
    )

    if plan.is_empty:
        title = f"Livoltek {plan.target_date}: ToU izslēgts"
        tags = ["sunny"]
    elif not has_stop and not has_cycles:
        title = f"Livoltek {plan.target_date}: baterija stāv EV laikā"
        tags = ["car"]
    elif has_stop and not has_cycles:
        title = f"Livoltek {plan.target_date}: Discharge {morning_soc}%"
        tags = ["sunny"]
    elif has_stop and has_cycles:
        n = len(plan.cycles)
        word = "cikls" if n == 1 else "cikli"
        title = (
            f"Livoltek {plan.target_date}: Discharge {morning_soc}% + {n} {word}"
        )
        tags = ["battery"]
    else:
        n = len(plan.cycles)
        word = "cikls" if n == 1 else "cikli"
        title = f"Livoltek {plan.target_date}: {n} {word}"
        tags = ["battery"]

    lines: list[str] = []
    if pv_forecast is not None:
        lines.append(f"PV: {pv_forecast.expected_kwh:.1f} kWh")
        lines.append("")

    if plan.is_empty:
        lines.append("ToU izslēgts")
    else:
        if has_stop:
            sw = plan.stop_window
            lines.append(
                f"{_fmt_local(sw.start)}-{_fmt_local(sw.end)} Discharge {morning_soc}%"
            )
        for c in plan.cycles:
            if c.hold_until is not None:
                lines.append(
                    f"{_fmt_local(c.charge.start)}-{_fmt_local(c.hold_until)} "
                    f"Charge 100% (pilna no {_fmt_local(c.charge.end)}, "
                    f"tur pilnu EV dēļ)"
                )
            else:
                lines.append(
                    f"{_fmt_local(c.charge.start)}-{_fmt_local(c.charge.end)} Charge 100%"
                )
        for w in plan.idle_windows:
            tonight = w.start.astimezone(RIGA_TZ).date() < plan.target_date
            lines.append(
                f"{'šovakar ' if tonight else ''}"
                f"{_fmt_local(w.start)}-{_fmt_local(w.end)} Stop "
                f"(baterija stāv, lai EV to neiztērē)"
            )

    if ev is not None and ev.options:
        lines.append("")
        hours = int(
            (ev.options[0].window.end - ev.options[0].window.start).total_seconds()
            // 3600
        )
        lines.append(f"EV uzlāde ({hours} h):")
        for i, o in enumerate(ev.options):
            mark = "⭐" if i == ev.best else "  "
            line = (
                f"{mark} {o.label} {_fmt_local(o.window.start)}-"
                f"{_fmt_local(o.window.end)} · "
                f"{o.window.avg_eur_per_kwh * 100:.1f} c/kWh · "
                f"100 km €{o.cost_100km_eur:.2f}"
            )
            if o.drains_battery:
                line += " ⚠ iztukšos mājas bateriju"
            lines.append(line)

    if nights:
        lines.append("")
        lines.extend(_nights_lines(nights))

    if yesterday is not None:
        lines.append("")
        # No EUR figure: an honest cost needs hourly import weighted by hourly
        # price, and multiplying by a daily mean would understate it (imports
        # cluster at the peaks). kWh alone is unambiguous.
        lines.append(
            format_daily_totals_line(yesterday, today=plan.target_date - _ONE_DAY)
        )

    return title, "\n".join(lines), tags


def _night_label(night) -> str:
    return f"{_WEEKDAYS[night.weekday()]}→{_WEEKDAYS[(night.weekday() + 1) % 7]}"


def _nights_lines(nights: list[NightOutlook]) -> list[str]:
    """Tonight (known) and the forecast nights, in date order, cheapest starred.

    Only the order is trustworthy: the backtest picks the cheapest of seven
    nights 44 % of the time and has it in the top two 68 % of the time, while
    the level itself is off by 25-34 EUR/MWh. Hence `~` and the band.
    """
    order = sorted(range(len(nights)), key=lambda i: nights[i].eur_per_kwh)
    marks = {order[0]: "⭐"}
    if len(order) > 1:
        marks[order[1]] = "☆"
    lines = ["Naktis uz priekšu (lētākās 3 h, c/kWh):"]
    for i, n in enumerate(nights):
        head = f"{marks.get(i, '▫️')} {_night_label(n.night)} {n.night:%d.%m}"
        if n.known:
            start = n.block_start.astimezone(RIGA_TZ)
            end = start + timedelta(hours=3)
            lines.append(
                f"{head} · {n.eur_per_kwh * 100:.1f} · zināma, {start:%H:%M}-{end:%H:%M}"
            )
        else:
            lines.append(
                f"{head} · ~{n.eur_per_kwh * 100:.0f} "
                f"({n.low * 100:.0f}–{n.high * 100:.0f})"
            )
    best = nights[order[0]]
    if best.known:
        lines.append(f"Lētākā: {_night_label(best.night)} (šonakt)")
    else:
        verdict = f"Lētākā: {_night_label(best.night)}"
        tonight = next((i for i, n in enumerate(nights) if n.known), None)
        if tonight is not None:
            verdict += f"; šonakt ir {order.index(tonight) + 1}. vietā"
        lines.append(verdict)
    return lines


def format_error_message(context: str, exc: BaseException) -> tuple[str, str, int]:
    """Render an error as (title, body, priority)."""
    title = f"Livoltek kļūda: {context}"
    body = f"{type(exc).__name__}: {exc}"
    return title, body, 4
