#!/usr/bin/env python3
"""
Flybilletbot — daglig prisovervågning direkte fra airchina.com.

Åbner Air Chinas priskalender i en rigtig (headless) browser, aflæser hele
pris-matrixen, gemmer den som historik og sender dagens pris for de faste
datoer til Telegram — sammen med et screenshot af selve kalenderen.

Aflæsningen bruger geometri (hvor priserne står på skærmen) frem for
skrøbelige CSS-selectors, så den overlever mindre ændringer i sidens HTML.
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
# URL til Air Chinas søgeresultat/priskalender for CPH -> CAN.
AIRCHINA_URL = os.getenv("AIRCHINA_URL", "")

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
BOOK_URL = "https://www.airchina.com"

# Hvor længe vi venter på at priskalenderen er tegnet færdig (ms)
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "60000"))


# ----- Aflæsning af pris-matrixen ------------------------------------------
# Kører inde i browseren. Finder alle priser og alle dato-overskrifter, og
# kobler dem sammen ud fra deres position: kolonne = nærmeste dato ovenover,
# række = nærmeste dato til venstre.
EXTRACT_JS = r"""
() => {
  const DATE_RE = /(\d{1,2})\s*[月\/\-.]\s*(\d{1,2})\s*日?/;

  const leaves = Array.from(document.querySelectorAll('td, th, div, span, a, p'))
    .filter(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 5 || r.height < 5) return false;
      // kun "blad"-elementer, så vi ikke tæller containere med
      return !Array.from(el.children).some(c => {
        const cr = c.getBoundingClientRect();
        return cr.width > 5 && cr.height > 5;
      });
    });

  const centre = el => {
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  };

  // --- priser ---
  const prices = [];
  for (const el of leaves) {
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 60) continue;
    if (!/\d/.test(txt)) continue;
    // find alle beløb i cellen (nuværende + evt. overstreget før-pris)
    const nums = txt.match(/[\d][\d.,]*[.,]\d{2}/g);
    if (!nums || !nums.length) continue;
    if (!/[A-Z]{3}|kr|DKK|CNY|EUR|USD/i.test(txt)) continue;
    const toNum = s => {
      // "8,290.00" -> 8290.00 ; "8.290,00" -> 8290.00
      const lastDot = s.lastIndexOf('.'), lastCom = s.lastIndexOf(',');
      if (lastCom > lastDot) return parseFloat(s.replace(/\./g, '').replace(',', '.'));
      return parseFloat(s.replace(/,/g, ''));
    };
    const c = centre(el);
    prices.push({
      x: c.x, y: c.y,
      price: toNum(nums[0]),
      was: nums.length > 1 ? toNum(nums[1]) : null,
      text: txt.replace(/\s+/g, ' '),
    });
  }
  if (!prices.length) return { error: 'ingen priser fundet', prices: [] };

  const minPx = Math.min(...prices.map(p => p.x));
  const minPy = Math.min(...prices.map(p => p.y));

  // --- dato-overskrifter ---
  const colHeads = [], rowHeads = [];
  for (const el of leaves) {
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 40) continue;
    const m = txt.match(DATE_RE);
    if (!m) continue;
    if (/[\d][\d.,]*[.,]\d{2}/.test(txt)) continue;   // det er en pris, ikke en dato
    const c = centre(el);
    const entry = { x: c.x, y: c.y, month: +m[1], day: +m[2], text: txt.replace(/\s+/g, ' ') };
    if (c.y < minPy - 5) colHeads.push(entry);        // står over priserne
    else if (c.x < minPx - 5) rowHeads.push(entry);   // står til venstre for priserne
  }

  const nearest = (list, key, val) => {
    let best = null, bestD = Infinity;
    for (const h of list) {
      const d = Math.abs(h[key] - val);
      if (d < bestD) { bestD = d; best = h; }
    }
    return bestD < 200 ? best : null;
  };

  const cells = [];
  for (const p of prices) {
    const col = nearest(colHeads, 'x', p.x);
    const row = nearest(rowHeads, 'y', p.y);
    if (!col || !row) continue;
    cells.push({
      out_month: row.month, out_day: row.day,
      ret_month: col.month, ret_day: col.day,
      price: p.price, was: p.was,
    });
  }
  return { cells, n_prices: prices.length, n_cols: colHeads.length, n_rows: rowHeads.length };
}
"""


def scrape():
    """Åbn siden og returnér (matrix-celler, sti til screenshot, diagnostik)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="da-DK",
            timezone_id="Europe/Copenhagen",
            viewport={"width": 1400, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.goto(AIRCHINA_URL, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")

        # Vent til priserne faktisk er tegnet (kalenderen loades med JS)
        try:
            page.wait_for_function(
                "() => /[A-Z]{3}\\s*[\\d.,]+[.,]\\d{2}/.test(document.body.innerText)",
                timeout=PAGE_TIMEOUT,
            )
        except Exception:
            pass  # vi tager screenshot alligevel, så fejlen kan ses
        page.wait_for_timeout(3000)

        DATA.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOT_FILE), full_page=True)

        result = page.evaluate(EXTRACT_JS)
        browser.close()
        return result


def pick(cells, out_date, ret_date):
    """Find cellen der matcher de faste datoer."""
    o = datetime.strptime(out_date, "%Y-%m-%d")
    r = datetime.strptime(ret_date, "%Y-%m-%d")
    for c in cells:
        if (c["out_month"], c["out_day"]) == (o.month, o.day) and \
           (c["ret_month"], c["ret_day"]) == (r.month, r.day):
            return c
    return None


def save_history(cells, mine):
    """Gem hele matrixen som JSON + dagens headline-pris i CSV."""
    stamp = datetime.now(timezone.utc)
    day = stamp.strftime("%Y-%m-%d")

    (DATA / "matrix").mkdir(parents=True, exist_ok=True)
    (DATA / "matrix" / f"{day}.json").write_text(
        json.dumps(cells, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    new = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["checked_at", "outbound", "return", "price", "was", "currency", "cells_seen"])
        w.writerow([
            stamp.strftime("%Y-%m-%d %H:%M UTC"),
            OUTBOUND_DATE, RETURN_DATE,
            mine["price"] if mine else "",
            (mine or {}).get("was") or "",
            CURRENCY, len(cells),
        ])


def previous_price():
    if not CSV_FILE.exists():
        return None
    try:
        rows = [r for r in csv.DictReader(CSV_FILE.open(encoding="utf-8")) if r.get("price")]
        return float(rows[-1]["price"]) if rows else None
    except (KeyError, ValueError, IndexError):
        return None


def build_message(mine, prev, cells, diag):
    head = (
        f"✈️ <b>Air China — CPH → CAN</b>\n"
        f"Ud: {OUTBOUND_DATE}  •  Hjem: {RETURN_DATE}\n\n"
    )
    if not mine:
        return (
            head
            + "⚠️ Kunne ikke aflæse prisen for dine datoer i dag.\n"
            + f"<i>Aflæst: {len(cells)} celler "
            + f"({diag.get('n_prices', 0)} priser, {diag.get('n_rows', 0)}×{diag.get('n_cols', 0)} datoer).</i>\n"
            + "Se screenshottet — måske er siden ændret, eller søgningen udløbet."
        )

    price = mine["price"]
    body = f"💰 <b>{price:,.0f} {CURRENCY}</b>".replace(",", ".")
    if mine.get("was") and mine["was"] > price:
        body += f"  <s>{mine['was']:,.0f}</s>".replace(",", ".")

    if prev is None:
        trend = "🆕 første måling"
    elif price < prev:
        trend = f"📉 <b>{prev - price:,.0f} {CURRENCY} billigere</b> end i går".replace(",", ".")
    elif price > prev:
        trend = f"📈 {price - prev:,.0f} {CURRENCY} dyrere end i går".replace(",", ".")
    else:
        trend = "➡️ uændret siden i går"

    alert = ""
    if price <= PRICE_ALERT_THRESHOLD:
        alert = f"\n\n🔥 <b>GODT KØB!</b> Under din grænse på {PRICE_ALERT_THRESHOLD:,.0f} {CURRENCY}.".replace(",", ".")

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
        "AIRCHINA_URL": AIRCHINA_URL,
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


if __name__ == "__main__":
    main()
