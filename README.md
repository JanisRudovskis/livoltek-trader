# Livoltek Trader

Automatizē Livoltek Gen2 hibrīdinvertora baterijas uzlādes/izlādes plānu, balstoties uz Nord Pool spot cenām Latvijā. Skripts skrien reizi dienā, paņem rītdienas cenas no Elering API, atrod izdevīgākos uzlādes/izlādes laikus un caur browser automation maina charge schedule Livoltek cloud portālā.

> Šī ir apzināta "apčakara" implementācija — browser automation pret cloud portālu. Tīrais ceļš (RS485/Modbus tieši pret invertoru) ir atstāts nākotnei, kad būs pieejama reģistru karte.

## Sistēmas konteksts

- **Invertors:** Livoltek Gen2 hibrīds
- **Baterija:** 10 kWh
- **Limits:** ne vairāk par 2 cikliem dienā
- **Vadības interfeiss:** Livoltek cloud portāls (Playwright + Chromium)

## Tehnoloģiju stack

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) — package manager
- [Playwright](https://playwright.dev/python/) — browser automation
- [httpx](https://www.python-httpx.org/) — Elering API klients
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — konfigurācija no `.env`
- [structlog](https://www.structlog.org/) — strukturēta logošana
- [ntfy](https://ntfy.sh/) — paziņojumi
- [pytest](https://docs.pytest.org/) — testi

## Setup

Pirms sākt, pārliecinies, ka ir uzstādīts [`uv`](https://docs.astral.sh/uv/getting-started/installation/).

```powershell
# 1. Klonē repo un ej iekšā
git clone <repo-url>
cd livoltek-trader

# 2. Instalē atkarības (uv automātiski lejupielādēs Python 3.12)
uv sync

# 3. Instalē Playwright Chromium
uv run playwright install chromium

# 4. Sagatavo .env
copy .env.example .env
# Aizpildi LIVOLTEK_USERNAME, LIVOLTEK_PASSWORD un NTFY_TOPIC
```

## Lietošana

> Šobrīd aktīvā izstrāde — pielietošanas komandas pievienosies, kad būs gatavs `main` entry point (8-soļu plāna 7. solis).

```powershell
# Palaist testus
uv run pytest

# Dry-run (kad būs gatavs main.py)
uv run livoltek-trader --dry-run
```

## Izstrādes plāns

Skat. [PROJECT_BRIEF.md](PROJECT_BRIEF.md) pilnam aprakstam. Īsumā:

1. ✅ Projekta setup
2. ⏳ Elering API klients + testi
3. ⏳ Trading stratēģijas modulis (pure funkcijas) + testi
4. ⏳ Ntfy paziņojumi
5. ⏳ Playwright login + navigation
6. ⏳ Playwright settings change
7. ⏳ Main entry point + dry-run režīms
8. ⏳ Cron / scheduler

## Drošība

- Visi secrets glabājas tikai `.env` failā, kas ir `.gitignore`-tots
- `DRY_RUN=true` ir noklusējums — pirmais reālais portāla raksts ir manuāli jāatļauj
- Sāc ar nedēļu read-only/dry-run režīmā pirms pieslēdz reālas izmaiņas

## Licence

Privāts projekts, nav publiskots.
