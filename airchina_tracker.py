#!/usr/bin/env python3
"""
Flybilletbot — daglig prisovervågning direkte fra airchina.dk.

Udfylder Air Chinas egen søgeformular i en rigtig (headless) browser,
aflæser hele den fleksible pris-matrix (7x7 datoer), gemmer den som
historik og sender dagens pris for de faste datoer til Telegram — med et
screenshot af kalenderen vedhæftet.

Flowet er verificeret mod det rigtige site:

  1. availability-URL'en kan IKKE deep-linkes (svarer "Bad Request" uden
     aktiv session) — søgningen skal gennemføres fra forsiden.
  2. De skjulte felter B_LOCATION_1/E_LOCATION_1 ryddes af sidens
     autocomplete, hvis man affyrer events på de synlige felter. Derfor
     sættes koderne til sidst, uden events.
  3. Søgeknappen åbner en "Important notes"-mellemside. Det er knappen
     button.submitForm ("Continue") i den, der faktisk sender søgningen.
  4. Hver celle i matrixen indeholder sine egne datoer i teksten
     ("Outbound 20/03/2027 Inbound 10/04/2027 DKK 8,290.00"), så priserne
     aflæses med en sprog-uafhængig regex frem for skrøbelige selectors.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ----- Konfiguration --------------------------------------------------------
START_URL = os.getenv("AIRCHINA_URL", "https://www.airchina.dk/")

ORIGIN = os.getenv("ORIGIN", "CPH")
ORIGIN_LABEL = os.getenv("ORIGIN_LABEL", "Copenhagen, CPH")
DEST = os.getenv("DEST", "CAN")
DEST_LABEL = os.getenv("DEST_LABEL", "Guangzhou, CAN")

OUTBOUND_DATE = os.getenv("OUTBOUND_DATE", "2027-03-20")  # ud (fast)
RETURN_DATE = os.getenv("RETURN_DATE", "2027-04-10")      # hjem (fast)
CURRENCY = os.getenv("CURRENCY", "DKK")
PRICE_ALERT_THRESHOLD = float(os.getenv("PRICE_ALERT_THRESHOLD", "8000"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE = Path(__file__).parent
DATA = BASE / "data"
# Egen fil — prices.csv bruges af SerpApi-botten og har andre kolonner
CSV_FILE = DATA / "airchina_prices.csv"
SHOT_FILE = DATA / "latest.png"
BOOK_URL = "https://www.airchina.dk/"

PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "60000"))


def dk_date(iso):
    """2027-03-20 -> 20/03/2027 (sidens langDateFormat er dd/MM/yyyy)."""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


# Udfylder søgeformularen. IS_FLEXIBLE=TRUE er det, der giver ±3-dages
# pris-matrixen i stedet for en enkelt pris.
FILL_JS = """
(cfg) => {
  const f = document.forms['flight-search-form'];
  if (!f) return { ok: false, error: 'flight-search-form ikke fundet' };

  const field = (name) => {
    const el = f.elements[name];
    if (!el) return null;
    return (el.length && el.tagName !== 'SELECT') ? el[0] : el;
  };
  const set = (name, value, fire = true) => {
    const el = field(name);
    if (!el) return false;
    el.value = value;
    if (fire) {
      el.dispatchEvent(new Event('input',  { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return true;
  };

  // Synlige felter først (de affyrer autocomplete-JS)
  set('FSB1FromSource', cfg.originLabel);
  set('FSB1ToDestination', cfg.destLabel);
  set('B_DATE_1', cfg.out);
  set('B_DATE_2', cfg.ret);
  set('TRIP_TYPE', 'R');        // retur
  set('IS_FLEXIBLE', 'TRUE');   // giver pris-matrixen
  set('CABIN', 'E');            // economy
  set('NB_ADT', '1');
  set('DIRECT_NON_STOP', 'false');

  // Lufthavnskoderne TIL SIDST og uden events — ellers rydder
  // autocomplete-handleren dem igen.
  set('B_LOCATION_1', cfg.origin, false);
  set('E_LOCATION_1', cfg.dest, false);

  const check = {};
  for (const k of ['B_LOCATION_1', 'E_LOCATION_1', 'B_DATE_1', 'B_DATE_2',
                   'TRIP_TYPE', 'IS_FLEXIBLE', 'CABIN', 'NB_ADT']) {
    const el = field(k);
    check[k] = el ? el.value : 'MANGLER';
  }
  const bad = Object.entries(check).filter(([, v]) => !v || v === 'MANGLER');
  return { ok: bad.length === 0, check, missing: bad.map(([k]) => k) };
}
"""

# Hver celle bærer sine egne datoer, fx:
#   "Outbound 20/03/2027 Inbound 10/04/2027 DKK 8,290.00 DKK 8,411.00"
# Regex'en bruger kun tal og valutakode, så den er uafhængig af sprog.
EXTRACT_JS = r"""
() => {
  const t = document.body.innerText.replace(/\s+/g, ' ');
  const re = /(\d{2}\/\d{2}\/\d{4})[^\d]{1,25}(\d{2}\/\d{2}\/\d{4})[^\d]{0,25}([A-Z]{3})\s*([\d.,]+[.,]\d{2})(?:[^\d]{0,25}([A-Z]{3})?\s*([\d.,]+[.,]\d{2}))?/g;
  const num = s => {
    const d = s.lastIndexOf('.'), c = s.lastIndexOf(',');
    return c > d ? parseFloat(s.replace(/\./g, '').replace(',', '.'))
                 : parseFloat(s.replace(/,/g, ''));
  };
  const cells = [];
  let m;
  while ((m = re.exec(t))) {
    const price = num(m[4]);
    const was = m[6] ? num(m[6]) : null;
    cells.push({
      out: m[1], ret: m[2], currency: m[3], price,
      // "was" er før-prisen (overstreget) — kun relevant hvis den er højere
      was: (was && was > price) ? was : null,
    });
  }
  return { cells, url: location.href, title: document.title };
}
"""


def scrape():
    """Kør søgningen og returnér matrix-celler + diagnostik."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="da-DK",
            timezone_id="Europe/Copenhagen",
            viewport={"width": 1500, "height": 1300},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        DATA.mkdir(parents=True, exist_ok=True)

        def shot():
            try:
                page.screenshot(path=str(SHOT_FILE), full_page=True)
            except Exception as exc:
                print(f"screenshot fejlede: {exc}", file=sys.stderr)

        page.goto(START_URL, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")

        # Cookie-banner: afvis alt ikke-nødvendigt (mest privatlivsvenlige valg)
        for sel in ("#denyCookieButton", "#cookie-notice-popin .closeBtn"):
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2500):
                    el.click()
                    page.wait_for_timeout(600)
                    break
            except Exception:
                continue

        filled = page.evaluate(FILL_JS, {
            "origin": ORIGIN, "originLabel": ORIGIN_LABEL,
            "dest": DEST, "destLabel": DEST_LABEL,
            "out": dk_date(OUTBOUND_DATE), "ret": dk_date(RETURN_DATE),
        })
        if not filled.get("ok"):
            shot()
            browser.close()
            return {"cells": [], "error": f"formular ikke udfyldt: {filled}"}

        page.click("button.fssubmit", timeout=PAGE_TIMEOUT)

        # Søgeknappen åbner en "Important notes"-mellemside;
        # det er Continue-knappen i den, der sender søgningen.
        try:
            cont = page.locator("#flight-search-form-prompt button.submitForm").first
            cont.wait_for(state="attached", timeout=8000)
            cont.click(timeout=8000)
        except Exception:
            pass  # mellemsiden vises ikke altid

        # Vent til matrixen er tegnet (celler har formen dd/mm/yyyy + beløb)
        try:
            page.wait_for_function(
                "() => /\\d{2}\\/\\d{2}\\/\\d{4}[^\\d]{1,25}\\d{2}\\/\\d{2}\\/\\d{4}"
                "[^\\d]{0,25}[A-Z]{3}\\s*[\\d.,]+[.,]\\d{2}/.test(document.body.innerText)",
                timeout=PAGE_TIMEOUT,
            )
        except Exception:
            pass  # screenshot tages alligevel, så fejlen kan ses
        page.wait_for_timeout(2500)

        shot()
        result = page.evaluate(EXTRACT_JS)
        browser.close()
        return result


def pick(cells, out_date, ret_date):
    o, r = dk_date(out_date), dk_date(ret_date)
    for c in cells:
        if c["out"] == o and c["ret"] == r:
            return c
    return None


def save_history(cells, mine):
    stamp = datetime.now(timezone.utc)
    day = stamp.strftime("%Y-%m-%d")

    (DATA / "matrix").mkdir(parents=True, exist_ok=True)
    (DATA / "matrix" / f"{day}.json").write_text(
        json.dumps(cells, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    cheapest = min(cells, key=lambda c: c["price"]) if cells else None
    new = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["checked_at", "outbound", "return", "price", "was",
                        "currency", "cells_seen", "cheapest_price",
                        "cheapest_out", "cheapest_ret"])
        w.writerow([
            stamp.strftime("%Y-%m-%d %H:%M UTC"),
            OUTBOUND_DATE, RETURN_DATE,
            mine["price"] if mine else "",
            (mine or {}).get("was") or "",
            CURRENCY, len(cells),
            cheapest["price"] if cheapest else "",
            cheapest["out"] if cheapest else "",
            cheapest["ret"] if cheapest else "",
        ])


def previous_price():
    if not CSV_FILE.exists():
        return None
    try:
        with CSV_FILE.open(encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("price")]
        return float(rows[-1]["price"]) if rows else None
    except (KeyError, ValueError, IndexError):
        return None


def dk(n):
    """8290 -> '8.290' (dansk tusindtalsseparator)."""
    return f"{n:,.0f}".replace(",", ".")


def build_message(mine, prev, cells, diag):
    head = (
        f"✈️ <b>Air China — {ORIGIN} → {DEST}</b>\n"
        f"Ud: {dk_date(OUTBOUND_DATE)}  •  Hjem: {dk_date(RETURN_DATE)}\n\n"
    )
    if not mine:
        return (
            head
            + "⚠️ Kunne ikke aflæse prisen for dine datoer i dag.\n"
            + f"<i>{len(cells)} celler fundet.</i>\n"
            + (f"<i>{diag['error']}</i>\n" if diag.get("error") else "")
            + "Se screenshottet — måske er siden ændret."
        )

    price = mine["price"]
    body = f"💰 <b>{dk(price)} {CURRENCY}</b>"
    if mine.get("was"):
        body += f"  <s>{dk(mine['was'])}</s>"

    if prev is None:
        trend = "🆕 første måling"
    elif price < prev:
        trend = f"📉 <b>{dk(prev - price)} {CURRENCY} billigere</b> end i går"
    elif price > prev:
        trend = f"📈 {dk(price - prev)} {CURRENCY} dyrere end i går"
    else:
        trend = "➡️ uændret siden i går"

    alert = ""
    if price <= PRICE_ALERT_THRESHOLD:
        alert = f"\n\n🔥 <b>GODT KØB!</b> Under din grænse på {dk(PRICE_ALERT_THRESHOLD)} {CURRENCY}."

    return (
        head + body + "\n" + trend + alert
        + f"\n\n<i>{len(cells)} priser gemt i historikken.</i>"
        + f"\n👉 Køb hos <a href=\"{BOOK_URL}\">Air China</a>"
    )


def send_telegram(text, photo=None):
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if photo and Path(photo).exists():
        with open(photo, "rb") as fh:
            r = requests.post(
                f"{api}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": text, "parse_mode": "HTML"},
                files={"photo": fh}, timeout=60,
            )
        if r.ok:
            return
        print(f"sendPhoto fejlede ({r.status_code}), falder tilbage til tekst", file=sys.stderr)

    requests.post(
        f"{api}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    ).raise_for_status()


def main():
    missing = [k for k, v in {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }.items() if not v]
    if missing:
        print(f"FEJL: manglende variabler: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    diag = scrape()
    cells = diag.get("cells") or []
    mine = pick(cells, OUTBOUND_DATE, RETURN_DATE)

    # Læs gårsdagens pris FØR vi gemmer dagens, ellers sammenligner vi med os selv
    prev = previous_price()
    if cells:
        save_history(cells, mine)

    msg = build_message(mine, prev, cells, diag)
    send_telegram(msg, SHOT_FILE)
    print(msg)
    print(json.dumps({k: v for k, v in diag.items() if k != "cells"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
