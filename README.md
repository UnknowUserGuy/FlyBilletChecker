# ✈️ Flybilletbot

Daglig prisovervågning for **Air China**, København (CPH) → Guangzhou (CAN).
Ud **20. marts 2027**, hjem **10. april 2027**.

Botten kører gratis i GitHub Actions én gang om dagen, henter den billigste
Air China-pris via Google Flights (SerpApi), gemmer en prishistorik og sender
dig en besked på **Telegram**. Når prisen er god, køber du selv billetten
direkte på [airchina.com](https://www.airchina.com).

---

## Sådan sætter du det op (ca. 15 min, alt er gratis)

### 1. Lav en Telegram-bot
1. Åbn Telegram og skriv til **@BotFather**.
2. Send `/newbot`, giv den et navn og et brugernavn.
3. Du får en **token** — se sådan ud: `123456789:AAExxxxxxxxxxxxxxxxxxxx`. Gem den.
4. Find dit **chat-id**: skriv en besked til din nye bot, åbn derefter i en browser:
   `https://api.telegram.org/bot<DIN_TOKEN>/getUpdates`
   Find tallet i `"chat":{"id": ...}`. Det er dit `TELEGRAM_CHAT_ID`.

### 2. Få en SerpApi-nøgle (gratis)
1. Opret gratis konto på [serpapi.com](https://serpapi.com/users/sign_up).
2. Kopiér din **API Key** fra dashboardet.
   Gratis-planen giver 100 søgninger/md — botten bruger ~30/md.

### 3. Læg koden på GitHub
1. Opret et **nyt privat repository** på GitHub, fx `flybilletbot`.
2. Upload disse filer (eller push mappen).
3. Gå til **Settings → Secrets and variables → Actions → New repository secret**
   og opret tre secrets:

   | Navn | Værdi |
   |------|-------|
   | `SERPAPI_KEY` | din SerpApi-nøgle |
   | `TELEGRAM_TOKEN` | din bot-token fra BotFather |
   | `TELEGRAM_CHAT_ID` | dit chat-id |

### 4. Kør den første gang
- Gå til fanen **Actions** → vælg **"Daglig flypris-tjek"** → **Run workflow**.
- Efter ~1 min bør du få din første Telegram-besked. 🎉
- Derefter kører den automatisk hver dag kl. 06:00 UTC (07-08 dansk tid).

---

## Test lokalt (valgfrit)
```bash
pip install -r requirements.txt
cp .env.example .env      # udfyld dine nøgler
# På Windows PowerShell, sæt variablerne og kør:
python flight_tracker.py
```

## Tilpasning
Alt kan ændres øverst i [`flight_tracker.py`](flight_tracker.py) eller via
miljøvariabler i workflow-filen:

- **Datoer / rute:** `OUTBOUND_DATE`, `RETURN_DATE`, `DEPARTURE_ID`, `ARRIVAL_ID`
- **Prisalarm-grænse:** `PRICE_ALERT_THRESHOLD` (standard 6000 DKK)
- **Klokkeslæt:** rediger `cron` i [`.github/workflows/track.yml`](.github/workflows/track.yml)

## Prishistorik
Hver kørsel tilføjer en linje til `data/prices.csv`, så du over tid kan se
prisudviklingen og time dit køb.

## Vigtigt
Botten **køber ikke** billetten — den holder dig kun opdateret. Air China har
ikke et offentligt køb-API, og deres side har bot-beskyttelse. Når du får en
god pris, gennemfører du selv købet på airchina.com.
