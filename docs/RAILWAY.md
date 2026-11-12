# Railway deployment guide

Šis dokuments paskaidro, kā uzstādīt Livoltek Trader uz Railway tā, lai tas skrien automātiski reizi dienā.

## Arhitektūra

```
Railway cron (18:00 un 19:00 UTC; strādā tikai tas, kas Rīgā ir 21:xx)
  → spin up Docker container from Dockerfile
    → uv run livoltek-trader --execute --riga-hour 21
      → (nav 21:xx Rīgā? → exit 0)
      → fetch Open-Meteo PV forecast
      → fetch Elering Nord Pool prices (rītdiena + šovakars EV logam)
      → plan_day → DailyPlan → advise_ev (EV logi + mājas baterijas turēšana)
      → forecast_nights (šonakt + 6 naktis; Elering vēsture + Open-Meteo
           Previous Runs vēja/temperatūras prognozes; kļūda → sadaļa izpaliek)
      → ntfy notification (plāns + EV logi + 100 km cena + naktis uz priekšu)
      → IF --execute: Playwright login + write_schedule
           (ToU off → visi 6 sloti kā reģistri → ToU on; katrs solis
            nolasīts atpakaļ no invertora, neapstiprināts → ntfy trauksme)
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
EV_CHARGE_HOURS=3                    # EV loga garums; 0 izslēdz EV ieteikumus
EV_CONSUMPTION_KWH_PER_100KM=20.0    # ieliec auto borta datora vidējo
EV_CHARGING_EFFICIENCY=0.90
```

**NELIEC Railway mainīgajos baterijas konstantes** (`BATTERY_CAPACITY_KWH`,
`ROUND_TRIP_EFFICIENCY` u.c.). Tās ir izmērītas un dzīvo `config.py`; vides
mainīgais tās klusi pārraksta — tieši tā 2026-09-01 ražošana strādāja ar
vecajām nameplate vērtībām. `main.start` logā redzams, kas reāli stājies spēkā.

### 3. Pārliecinies par cron grafiku

`railway.json`: `"cronSchedule": "0 18,19 * * *"` un `startCommand` ar
`--riga-hour 21`. Railway cron saprot tikai UTC, tāpēc tas palaižas divreiz:

| | 18:00 UTC | 19:00 UTC |
|---|---|---|
| Vasarā (UTC+3) | **21:00 Rīgā → strādā** | 22:00 → exit 0 |
| Ziemā (UTC+2) | 20:00 → exit 0 | **21:00 Rīgā → strādā** |

Tā grafiks tiek iestatīts 21:00 pēc Rīgas laika visu gadu, bez DST
labojumiem. Lieko palaidienu logā redz kā `main.skip_not_scheduled_hour`.

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

Railway dashboard -> Service -> Deployments -> "Run Once" — palaiž tūlīt (neatkarīgi no cron).
**Bet** `--riga-hour 21` liek tam uzreiz beigties, ja Rīgā nav 21:xx. Manuālam
palaidienam citā laikā īslaicīgi noņem `--riga-hour 21` no Start Command. Noderīgs:
- Pārbaudi pēc pirmā setup'a
- Sūta paziņojumu pēc tava pieprasījuma
- Testē izmaiņas

## Pārbaudes saraksts pēc pirmā deploy

- [ ] `railway logs` rāda `main.start ... execute=False target_date=YYYY-MM-DD`
- [ ] Saņemtais ntfy paziņojums ir loģisks (vai nu `izlaiž` ar iemeslu, vai cikli ar laikiem)
- [ ] Nav `playwright._impl._errors.TimeoutError` izsaukumu
- [ ] Login bieži notiek bez problēmām (popup pareizi atrisina)

Ja viss ok → aktivē `--execute`.

## Nakšu prognozes modelis

Sadaļa "Naktis uz priekšu" sarindo nākamās naktis pēc prognozētā lētāko 3 h bloka cenas. Koeficienti glabājas `src/livoltek_trader/night_model.json`.

Pārtrenē reizi mēnesī vai tad, kad tirgus būtiski mainās:

```bash
uv run --with numpy python scripts/train_night_model.py
```

Skripts tikai lasa publiskos API (Elering un Open-Meteo). Pēc tam iecommito atjaunoto JSON. Uz servera numpy nav vajadzīgs, jo prognoze ir tīrs Python.

Precizitāte pārbaudē uz vēstures datiem: lētāko no 7 naktīm trāpa 44 %, divās labākajās tā ir 68 % gadījumu. Cenu līmenis ir aptuvens, kļūda ir 25–34 €/MWh. Tāpēc ziņā ir `~` un diapazons.

## Pārvietot uz citu hosting

Ja Railway nav vairs piemērots:
- Dockerfile ir portable — strādās uz jebkurā Docker hosts'os (Fly.io, AWS Fargate, mājas Pi).
- railway.json ir Railway-specific. Citur cron jāpārkonfigurē atsevišķi (Fly.io machines, k8s CronJob, crontab).

## Brīdinājumi

1. **Storage state nepersistē** starp skrējieniem. Tas ir OK — login ir ātrs, bet ja IP adrese mainās bieži, var redzēt vairāk popup uzdomāšanu Livoltek pusē.
2. **Konteinera IP var mainīties** — Railway nesalga statisku IP. Livoltek pagaidām neierobežo, bet ja sāk, pārvieto.
3. **Bezmaksas tier ierobežojumi** — Railway free trial ir $5/mēn. Mūsu skrējiens ~1 min/dienā = nieks (<$0.10/mēn pat brīvajam). Bet image storage ~1GB var pievienot vēl 10-50¢/mēn.
4. **DST pāreja** — atrisināta ar divām UTC stundām + `--riga-hour 21` (skat. 3. soli).

## Atjaunināt deployment

```bash
git push origin master       # ja GitHub-integrated
# vai
railway up                   # ja manuāli
```

Cron automātiski izmantos jauno versiju nākamajā skrējienā.
