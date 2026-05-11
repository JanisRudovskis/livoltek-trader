# Livoltek Trader — projekta brīfs

## Mērķis
Automatizēt Livoltek Gen2 hibrīdinvertora baterijas uzlādes/izlādes 
plānu, balstoties uz Nord Pool spot cenām Latvijā. Skripts skrien 
reizi dienā un caur browser automation maina charge schedule 
Livoltek cloud portālā.

## Sistēmas konteksts
- **Invertors:** Livoltek Gen2 hibrīds
- **Baterija:** 10 kWh
- **Lādēšanas jauda:** neierobežota (no tīkla)
- **Limits:** max 2 cikli dienā (uzlāde + izlāde = 1 cikls)
- **Vadības interfeiss:** Livoltek cloud portāls (browser automation)
- **Modbus opcija:** atstāta nākotnei, šobrīd ejam ar browser

## Tehnoloģiju stack
- Python 3.12+
- `uv` package manager
- Playwright (browser automation, Chromium)
- httpx (Elering Nord Pool API klients)
- pydantic-settings (konfigurācija no .env)
- structlog (logošana)
- apprise vai requests (ntfy paziņojumi)
- pytest (testi)

## Datu avoti
- **Spot cenas:** Elering API (https://dashboard.elering.ee/api), 
  reģions LV, bezmaksas, bez auth
- **Portāls:** Livoltek monitor portāls (login ar username/password)

## Trading stratēģija (sākotnējā)
1. Reizi dienā ap 22:00 (kad rītdienas cenas publicētas)
2. Dabū 24h cenas no Elering
3. Atrod N lētākās stundas (uzlāde) un M dārgākās (izlāde)
4. Ja starpība > minimālais slieksnis (piem. 0.10 €/kWh) → plāno ciklu
5. Ja nav vērts (mazs spread) → izlaiž dienu
6. Max 2 cikli dienā

## Paziņojumi
Ntfy uz konkrētu topiku par:
- Veiksmīgs run (kopsavilkums: kuras stundas, paredzamā ekonomija)
- Kļūda jebkurā solī
- Login problēma
- Skripts nav skrējis 2+ dienas (healthcheck)

## Drošība
- Visi secrets `.env` failā, nekad repo
- `.gitignore` no pirmās dienas
- Sāk ar read-only/dry-run režīmu pirms reālām izmaiņām

## Izstrādes plāns (secībā)
1. Projekta setup (struktūra, deps, .env.example)
2. Elering API klients + testi
3. Trading stratēģijas modulis (pure funkcijas) + testi
4. Ntfy paziņojumu modulis
5. Playwright login + navigation
6. Playwright settings change
7. Main entry point + dry-run režīms
8. Cron / scheduler (vēlāk, kad lokāli strādā 5+ dienas)

## Hosting plāns
Sākumā lokāli (manuāla palaišana). Vēlāk migrē uz Railway 
vai mājas mini PC, kad loģika validēta.

## Valoda
- Kods: angļu valodā (mainīgie, komentāri, docstrings)
- README un user-facing teksti: latviešu valodā
- Ntfy paziņojumi: latviešu valodā