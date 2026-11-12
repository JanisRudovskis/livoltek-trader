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
LIVOLTEK_USERNAME=<portāla lietotājvārds>
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

### 3. Iestati cron grafiku (Railway dashboard)

Railway → Service → Settings → **Cron Schedule** → Custom: `0 18,19 * * *`.

Palaišanas komanda **nav** jāiestata dashboard. Tā dzīvo `Dockerfile` `CMD`:
`uv run --no-sync livoltek-trader --execute --riga-hour 21`. Lauku **Custom Start Command**
atstāj tukšu, citādi tas pārraksta Dockerfile.

> `railway.json` (Railway "Config as Code") tika izņemts 2026-11-12. Railway to
> pārtrauc lasīt 2026-12-01, un dashboard vērtības tad paliek vienīgās. Builder
> Railway nosaka automātiski, jo saknē ir `Dockerfile`. Cron servisiem restart
> policy nav.

Railway cron saprot tikai UTC, tāpēc tas palaižas divreiz:

| | 18:00 UTC | 19:00 UTC |
|---|---|---|
| Vasarā (UTC+3) | **21:00 Rīgā → strādā** | 22:00 → exit 0 |
| Ziemā (UTC+2) | 20:00 → exit 0 | **21:00 Rīgā → strādā** |

Tā grafiks tiek iestatīts 21:00 pēc Rīgas laika visu gadu, bez DST
labojumiem. Lieko palaidienu logā redz kā `main.skip_not_scheduled_hour`.

### 4. Pārbaudi, ka izvietots tieši jaunākais commit

Push uz `master` izvieto automātiski (Settings → Source: repo, zars `master`,
"Auto deploys" ieslēgts). **Nepaļaujies uz to akli.** No 2026-09-23 līdz
2026-11-12 push klusi neizvietojās, un produkcijā strādāja vecs kods.

Ko produkcija tiešām darbina, redzams GitHub izvietošanas ierakstā:
```bash
curl -s "https://api.github.com/repos/JanisRudovskis/livoltek-trader/deployments?per_page=1"
```
Vēl viena pazīme ir ntfy ziņā: bez "EV uzlāde" sadaļas ir vecs kods. Ja vajag
izvietot ar roku: Railway → Ctrl+K → **Deploy latest commit**. Neizmanto
"Redeploy", jo tas atkārto veco commit.

### 5. Dry-run (bez rakstīšanas invertorā)

Īslaicīgi iestati Custom Start Command uz `uv run --no-sync livoltek-trader`. Ziņa tad
nāk ar `[DRY-RUN]` prefiksu, un invertorā nekas netiek rakstīts. Pēc tam lauku
iztukšo.

### 6. Manuāli palaist starpposmā

Railway dashboard -> Service -> Deployments -> "Run Once" — palaiž tūlīt (neatkarīgi no cron).
**Bet** `--riga-hour 21` liek tam uzreiz beigties, ja Rīgā nav 21:xx. Manuālam
palaidienam citā laikā īslaicīgi iestati Custom Start Command uz
`uv run --no-sync livoltek-trader --execute` (bez `--riga-hour`), pēc tam iztukšo. Noderīgs:
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
- Cron grafiks dzīvo Railway dashboard. Citur tas jāpārkonfigurē atsevišķi (Fly.io machines, k8s CronJob, crontab). Komanda ir Dockerfile `CMD`.

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
