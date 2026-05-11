# Railway deployment guide

Šis dokuments paskaidro, kā uzstādīt Livoltek Trader uz Railway tā, lai tas skrien automātiski reizi dienā.

## Arhitektūra

```
Railway cron (22:30 UTC daily)
  → spin up Docker container from Dockerfile
    → uv run livoltek-trader
      → fetch Open-Meteo PV forecast
      → fetch Elering Nord Pool prices
      → plan_day → DailyPlan
      → ntfy notification (dry-run summary OR action confirmation)
      → IF --execute: Playwright login + apply_schedule(save=True)
      → exit
  → container stops (next run at next cron tick)
```

Konteineris pārtraucas pēc katra skrējiena. Storage state nepersistē — katru reizi fresh login (~22 sec). Tā ir apzināta izvēle, lai paliek vienkārši.

## Pirmā setup'a soļi

### 1. Iepazīstinis repo Railway

Variants A (ieteicams): pieslēdz GitHub repo Railway projektam.
```
Railway dashboard -> New Project -> Deploy from GitHub Repo
```

Variants B: pielietot Railway CLI manuāli.
```bash
npm install -g @railway/cli
railway login
railway link            # vai railway init
railway up
```

### 2. Iestati environment variables

Railway dashboard -> Service -> Variables:

**Obligāti:**
```
LIVOLTEK_USERNAME=Plesumu5
LIVOLTEK_PASSWORD=<tava parole>
NTFY_TOPIC=livoltek-janis-7a3f9c
```

**Ieteicami (overrides defaults):**
```
LIVOLTEK_HEADLESS=true              # Dockerfile jau iestata
TZ=Europe/Riga                       # Strukturētajos logos vēl viens timestamps
```

**Opcionāli (overrides defaults):**
```
BATTERY_CAPACITY_KWH=10.24
ROUND_TRIP_EFFICIENCY=0.90
BATTERY_PRICE_EUR=3000
MAX_CYCLES_PER_DAY=6
HOURS_PER_CYCLE=2
MIN_NET_PROFIT_PER_CYCLE_EUR=0.25
EXPECTED_DAILY_LOAD_KWH=22.0
PV_KWH_PER_MJ_M2=2.98
PV_LAT=56.918
PV_LON=24.043
```

### 3. Pārliecinies par cron grafiku

`railway.json` jau satur: `"cronSchedule": "30 22 * * *"` (22:30 UTC).

- Vasarā (EEST UTC+3): atbilst **01:30 Rīgas laikā**
- Ziemā (EET UTC+2): atbilst **00:30 Rīgas laikā**

Abos gadījumos pēc Riga local pusnakts. Tas dod laiku rīta lētajām stundām (parasti 02:00+) iestatīt schedule.

Ja gribi citu laiku, mainies `railway.json` un redeploy. Cron sintakse standarta.

### 4. Pirmais deploy — DRY-RUN režīms

Pēc noklusējuma `railway.json` `startCommand` ir `uv run livoltek-trader` **bez** `--execute`. Tas nozīmē:
- Pirmais cron skrējiens dabūs PV prognozi + cenas + sastādīs plānu
- Nosūtīs ntfy paziņojumu ar `[DRY-RUN]` prefiksu
- **Neraksta neko reālā invertorā**

Pārbaudi ntfy paziņojumu pēc pirmā cron skrējiena. Ja plāns izskatās saprātīgi → pārej uz execute režīmu.

### 5. Aktivē reālo režīmu

Mainies `railway.json`:
```json
"startCommand": "uv run livoltek-trader --execute"
```

Commit, push, redeploy. Nākamais cron skrējiens reālā veidā ielādēs grafiku invertorā.

Alternatīvi: Railway dashboard -> Service -> Settings -> Start Command override (bez kods izmaiņām).

### 6. Manuāli palaist starpposmā

Railway dashboard -> Service -> Deployments -> "Run Once" — palaiž tūlīt (neatkarīgi no cron). Noderīgs:
- Pārbaudi pēc pirmā setup'a
- Sūta paziņojumu pēc tava pieprasījuma
- Testē izmaiņas

## Pārbaudes saraksts pēc pirmā deploy

- [ ] `railway logs` rāda `main.start ... execute=False target_date=YYYY-MM-DD`
- [ ] Saņemtais ntfy paziņojums ir loģisks (vai nu `izlaiž` ar iemeslu, vai cikli ar laikiem)
- [ ] Nav `playwright._impl._errors.TimeoutError` izsaukumu
- [ ] Login bieži notiek bez problēmām (popup pareizi atrisina)

Ja viss ok → aktivē `--execute`.

## Pārvietot uz citu hosting

Ja Railway nav vairs piemērots:
- Dockerfile ir portable — strādās uz jebkurā Docker hosts'os (Fly.io, AWS Fargate, mājas Pi).
- railway.json ir Railway-specific. Citur cron jāpārkonfigurē atsevišķi (Fly.io machines, k8s CronJob, crontab).

## Brīdinājumi

1. **Storage state nepersistē** starp skrējieniem. Tas ir OK — login ir ātrs, bet ja IP adrese mainās bieži, var redzēt vairāk popup uzdomāšanu Livoltek pusē.
2. **Konteinera IP var mainīties** — Railway nesalga statisku IP. Livoltek pagaidām neierobežo, bet ja sāk, pārvieto.
3. **Bezmaksas tier ierobežojumi** — Railway free trial ir $5/mēn. Mūsu skrējiens ~1 min/dienā = nieks (<$0.10/mēn pat brīvajam). Bet image storage ~1GB var pievienot vēl 10-50¢/mēn.
4. **DST pāreja** — cron paliek UTC. Riga local time mainās 2× gadā. Skrējiena laiks var nokrist neērti tieši DST naktī.

## Atjaunināt deployment

```bash
git push origin master       # ja GitHub-integrated
# vai
railway up                   # ja manuāli
```

Cron automātiski izmantos jauno versiju nākamajā skrējienā.
