#!/usr/bin/env python3
"""
Flybilletbot — daglig prisovervågning for Air China, CPH -> CAN (Guangzhou).

Henter prisen via SerpApi (Google Flights), filtrerer på Air China,
gemmer historik i data/prices.csv og sender en Telegram-besked.

Konfigureres via miljøvariabler (se README.md). Kan køres lokalt eller i
GitHub Actions.
"""

import csv
import os
import sys
import traceback
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import requests

# ----- Rute-konfiguration (kan ændres her eller via env) --------------------
DEPARTURE_ID = os.getenv("DEPARTURE_ID", "CPH")          # København
ARRIVAL_ID = os.getenv("ARRIVAL_ID", "CAN")              # Guangzhou
OUTBOUND_DATE = os.getenv("OUTBOUND_DATE", "2027-03-20")  # ud
RETURN_DATE = os.getenv("RETURN_DATE", "2027-04-10")      # hjem
CURRENCY = os.getenv("CURRENCY", "DKK")
AIRLINE_CODE = os.getenv("AIRLINE_CODE", "CA")            # Air China IATA
# Send ekstra "GODT KØB"-alarm hvis prisen er under denne grænse:
PRICE_ALERT_THRESHOLD = float(os.getenv("PRICE_ALERT_THRESHOLD", "6000"))

# ----- Hemmeligheder (sættes som GitHub Secrets / env) ----------------------
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DATA_FILE = Path(__file__).parent / "data" / "prices.csv"
BOOK_URL = "https://www.airchina.com"


def fetch_flights():
    """Kald SerpApi og returnér listen af Air China-flyvninger."""
    params = {
        "engine": "google_flights",
        "departure_id": DEPARTURE_ID,
        "arrival_id": ARRIVAL_ID,
        "outbound_date": OUTBOUND_DATE,
        "return_date": RETURN_DATE,
        "type": "1",                  # 1 = retur
        "currency": CURRENCY,
        "hl": "en",
        "gl": "dk",
        "include_airlines": AIRLINE_CODE,  # filtrér til Air China
        "api_key": SERPAPI_KEY,
    }
    resp = requests.get("https://serpapi.com/search", params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"SerpApi-fejl: {data['error']}")

    flights = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    return flights


def summarize(flights):
    """Find den billigste Air China-flyvning og returnér et resumé-dict."""
    if not flights:
        return None

    cheapest = min(flights, key=lambda f: f.get("price", float("inf")))
    legs = cheapest.get("flights", [])
    airlines = sorted({leg.get("airline", "?") for leg in legs})
    stops = max(len(legs) - 1, 0)
    layovers = ", ".join(l.get("name", "?") for l in cheapest.get("layovers", []))

    return {
        "price": cheapest.get("price"),
        "airlines": " + ".join(airlines),
        "stops": stops,
        "layovers": layovers,
        "duration_min": cheapest.get("total_duration"),
    }


def save_history(summary):
    """Tilføj dagens pris til CSV-historikken."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    is_new = not DATA_FILE.exists()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    with DATA_FILE.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(
                ["checked_at", "route", "outbound", "return",
                 "price", "currency", "airlines", "stops"]
            )
        writer.writerow([
            now,
            f"{DEPARTURE_ID}-{ARRIVAL_ID}",
            OUTBOUND_DATE,
            RETURN_DATE,
            summary["price"],
            CURRENCY,
            summary["airlines"],
            summary["stops"],
        ])


def previous_price():
    """Læs sidste registrerede pris (før i dag) til at vise ændring."""
    if not DATA_FILE.exists():
        return None
    try:
        with DATA_FILE.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            return None
        return float(rows[-1]["price"])
    except (KeyError, ValueError):
        return None


def build_message(summary, prev):
    """Byg Telegram-teksten (HTML)."""
    if summary is None:
        return (
            f"⚠️ <b>Flybilletbot</b>\n"
            f"Ingen Air China-flyvninger fundet for "
            f"{DEPARTURE_ID}→{ARRIVAL_ID} ({OUTBOUND_DATE} / {RETURN_DATE}).\n"
            f"Prøv igen i morgen, eller tjek datoerne."
        )

    price = summary["price"]
    hours, mins = divmod(summary["duration_min"] or 0, 60)
    stops_txt = "direkte" if summary["stops"] == 0 else f"{summary['stops']} mellemlanding(er)"

    # Ændring siden sidst
    if prev is not None:
        diff = price - prev
        if diff < 0:
            trend = f"📉 <b>{abs(diff):,.0f} {CURRENCY} billigere</b> end sidst"
        elif diff > 0:
            trend = f"📈 {diff:,.0f} {CURRENCY} dyrere end sidst"
        else:
            trend = "➡️ samme pris som sidst"
    else:
        trend = "🆕 første måling"

    alert = ""
    if price <= PRICE_ALERT_THRESHOLD:
        alert = f"\n\n🔥 <b>GODT KØB!</b> Under din grænse på {PRICE_ALERT_THRESHOLD:,.0f} {CURRENCY}."

    return (
        f"✈️ <b>Flybilletbot — Air China</b>\n"
        f"{DEPARTURE_ID} → {ARRIVAL_ID} (København → Guangzhou)\n"
        f"Ud: {OUTBOUND_DATE}  •  Hjem: {RETURN_DATE}\n\n"
        f"💰 <b>{price:,.0f} {CURRENCY}</b>\n"
        f"{trend}\n"
        f"🛫 {summary['airlines']} • {stops_txt}"
        + (f" via {summary['layovers']}" if summary["layovers"] else "")
        + f"\n⏱️ {hours}t {mins}m rejsetid"
        + alert
        + f"\n\n👉 Køb direkte hos <a href=\"{BOOK_URL}\">Air China</a>"
    )


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, data=payload, timeout=30)
    resp.raise_for_status()


def main():
    missing = [k for k, v in {
        "SERPAPI_KEY": SERPAPI_KEY,
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }.items() if not v]
    if missing:
        print(f"FEJL: manglende miljøvariabler: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    flights = fetch_flights()
    summary = summarize(flights)
    prev = previous_price()

    if summary is not None:
        save_history(summary)

    message = build_message(summary, prev)
    send_telegram(message)
    print("Sendt til Telegram:\n" + message)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Dette er nu den eneste priskilde, så en crash må aldrig blive til
        # tavshed. Uden det her fejler jobbet bare i Actions, og man opdager
        # først at botten er død, når man undrer sig over ikke at have hørt
        # fra den i flere dage. Typiske årsager: SerpApi-kvoten er brugt op,
        # nøglen er udløbet, eller deres API er nede.
        traceback.print_exc()
        try:
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                send_telegram(
                    "🛑 <b>Flybilletbotten fejlede</b>\n\n"
                    f"<i>{escape(type(exc).__name__)}: "
                    f"{escape(str(exc)[:600])}</i>\n\n"
                    "Tjek om SerpApi-kvoten er brugt op "
                    "(gratis niveau: 100 søgninger/md)."
                )
        except Exception:
            print("kunne heller ikke sende fejlbesked til Telegram", file=sys.stderr)
        sys.exit(1)
