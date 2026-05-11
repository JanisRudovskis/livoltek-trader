"""ntfy notification client (success summaries, errors, healthcheck alerts).

ntfy.sh accepts plain HTTP POSTs to `https://ntfy.sh/{topic}` with the message
body as the request body and optional metadata in headers (Title, Priority,
Tags, Authorization). User-facing strings here are Latvian; identifiers and
docstrings stay English.
"""

from __future__ import annotations

from typing import Iterable
from zoneinfo import ZoneInfo

import httpx

from livoltek_trader.config import Settings, get_settings
from livoltek_trader.solar import PvForecast
from livoltek_trader.strategy import CyclePair, DailyPlan, HourlyPrice

RIGA_TZ = ZoneInfo("Europe/Riga")


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
            raise NtfyError(f"transport error: {exc}") from exc

        if not (200 <= response.status_code < 300):
            raise NtfyError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )


def _fmt_local(dt) -> str:
    return dt.astimezone(RIGA_TZ).strftime("%H:%M")


def _format_day_context(
    pv_forecast: PvForecast | None,
    hourly_prices: list[HourlyPrice] | None,
    settings: Settings | None,
) -> list[str]:
    lines: list[str] = []
    if pv_forecast is not None:
        lines.append(
            f"PV prognoze: {pv_forecast.expected_kwh:.1f} kWh "
            f"(mākoņu segums {pv_forecast.cloud_cover_pct:.0f}%)"
        )
    if settings is not None:
        load = settings.expected_daily_load_kwh
        lines.append(f"Paredzamais patēriņš: {load:.0f} kWh")
        if pv_forecast is not None:
            gap = max(0.0, load - pv_forecast.expected_kwh)
            lines.append(f"Tīkla imports paredzami: {gap:.1f} kWh")
    if hourly_prices:
        prices = [h.eur_per_kwh for h in hourly_prices]
        spread = max(prices) - min(prices)
        lines.append(
            f"Cenu diapazons: {min(prices):.4f}–{max(prices):.4f} €/kWh "
            f"(spread {spread:.4f})"
        )
    return lines


def _format_cycle_block(
    cycle: CyclePair, idx: int, total: int
) -> list[str]:
    prefix = f"Cikls {idx}: " if total > 1 else ""
    return [
        f"{prefix}lādē no tīkla {_fmt_local(cycle.charge.start)}–"
        f"{_fmt_local(cycle.charge.end)} par "
        f"{cycle.charge.avg_eur_per_kwh:.4f} €/kWh (lētākais brīdis)",
        f"  Mājsaimniecība izlādēs {_fmt_local(cycle.discharge.start)}–"
        f"{_fmt_local(cycle.discharge.end)} par "
        f"{cycle.discharge.avg_eur_per_kwh:.4f} €/kWh, aiztaupot grid importu "
        f"par šo cenu.",
        f"  Bruto €{cycle.gross_revenue_eur:.2f} − nodilums "
        f"€{cycle.wear_cost_eur:.2f} = tīrais €{cycle.net_profit_eur:.2f}",
    ]


def _explain_skip(reason: str) -> str | None:
    if "PV forecast" in reason and "below one cycle output" in reason:
        return (
            "PV ražos vairāk par patēriņu — baterija piepildīsies no saules bez "
            "maksas, un mājsaimniecība dārgajās stundās izmantos pati šo "
            "uzlādi. Tīkla uzlāde tikai tērētu cikla nodilumu bez ietaupījuma."
        )
    if "no cycle nets at least" in reason:
        return (
            "Šodien cenu spread ir pārāk līdzens — neviens cikls nepārsniedz "
            "minimālo neto peļņas slieksni pēc baterijas nodiluma izmaksu "
            "(€0.50/cikls) atskaitīšanas."
        )
    if "max_cycles_per_day is 0" in reason:
        return "Automātiskā tirdzniecība ir manuāli atslēgta konfigurācijā."
    if "not enough hourly data" in reason:
        return (
            "Šodienai trūkst stundu cenu datu (iespējams, Nord Pool publicēšanas "
            "kavēšanās vai datu pārklājums)."
        )
    return None


def format_plan_message(
    plan: DailyPlan,
    *,
    pv_forecast: PvForecast | None = None,
    hourly_prices: list[HourlyPrice] | None = None,
    settings: Settings | None = None,
) -> tuple[str, str, list[str]]:
    """Render a daily plan as (title, body, tags) in Latvian.

    When pv_forecast / hourly_prices / settings are supplied, the body
    includes a day-context preamble (PV forecast, expected load, price
    range) and a per-cycle reasoning block. Times are Riga local.
    """
    if plan.skipped_reason:
        title = f"Livoltek {plan.target_date}: izlaiž"
    else:
        n = len(plan.cycles)
        cycle_word = "cikls" if n == 1 else "cikli"
        title = (
            f"Livoltek {plan.target_date}: {n} {cycle_word}, "
            f"tīrais €{plan.total_net_profit_eur:.2f}"
        )

    sections: list[str] = []

    context = _format_day_context(pv_forecast, hourly_prices, settings)
    if context:
        sections.append("\n".join(context))

    if plan.skipped_reason:
        skip_block = [f"Plāns: izlaiž", f"Iemesls: {plan.skipped_reason}"]
        explanation = _explain_skip(plan.skipped_reason)
        if explanation:
            skip_block.append("")
            skip_block.append(explanation)
        sections.append("\n".join(skip_block))
        return title, "\n\n".join(sections), ["sunny"]

    cycle_lines: list[str] = [f"Plāns: {len(plan.cycles)} cikls(i), tīrais €{plan.total_net_profit_eur:.2f}"]
    for i, c in enumerate(plan.cycles, 1):
        cycle_lines.append("")
        cycle_lines.extend(_format_cycle_block(c, i, len(plan.cycles)))
    sections.append("\n".join(cycle_lines))
    return title, "\n\n".join(sections), ["battery"]


def format_error_message(context: str, exc: BaseException) -> tuple[str, str, int]:
    """Render an error as (title, body, priority)."""
    title = f"Livoltek kļūda: {context}"
    body = f"{type(exc).__name__}: {exc}"
    return title, body, 4
