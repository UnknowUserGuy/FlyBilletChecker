#!/usr/bin/env python3
"""
Flybilletbot — daglig prisovervågning direkte fra airchina.dk.

Udfylder Air Chinas egen søgeformular i en rigtig (headless) browser,
aflæser hele den fleksible pris-matrix, gemmer den som historik og sender
dagens pris for de faste datoer til Telegram — med et screenshot af
kalenderen vedhæftet.

Formularen kan ikke deep-linkes (availability-siden svarer "Bad Request"
uden en aktiv session), så vi udfylder felterne og lader sidens eget
JavaScript håndtere den krypterede submit til /CAPortal/dyn/portal/doEnc.

Aflæsningen af priserne bruger geometri (hvor tallene står på skærmen)
frem for skrøbelige CSS-selectors, så den overlever mindre ændringer i HTML.
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


# Udfylder søgeformularen. Felterne er dem Air Chinas eget skema bruger:
# IS_FLEXIBLE=TRUE er det, der giver ±3-dages pris-matrixen.
FILL_JS = """
(cfg) => {
  const f = document.forms['flight-search-form'];
  if (!f) return { ok: false, error: 'flight-search-form ikke fundet' };

  const set = (name, value) => {
    const el = f.elements[name];
    if (!el) return false;
    const target = (el.length && el.tagName !== 'SELECT') ? el[0] : el;
    target.value = value;
    target.dispatchEvent(new Event('input',  { bubbles: true }));
    target.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  };

  const applied = {};
  const fields = {
    B_LOCATION_1: cfg.origin,
    FSB1FromSource: cfg.originLabel,
    E_LOCATION_1: cfg.dest,
    FSB1ToDestination: cfg.destLabel,
    B_DATE_1: cfg.out,
    B_DATE_2: cfg.ret,
    TRIP_TYPE: 'R',        // retur
    IS_FLEXIBLE: 'TRUE',   // giver pris-matrixen
    CABIN: 'E',            // economy
    NB_ADT: '1',
    DIRECT_NON_STOP: 'false',
  };
  for (const [k, v] of Object.entries(fields)) applied[k] = set(k, v);
  return { ok: true, applied };
}
"""

# Aflæser matrixen: kobler hver pris til nærmeste dato-overskrift
# ovenover (kolonne = hjemrejse) og til venstre (række = udrejse).
EXTRACT_JS = r"""
() => {
  const DATE_RE = /(\d{1,2})\s*[月\/\-.]\s*(\d{1,2})\s*日?/;

  const leaves = Array.from(document.querySelectorAll('td, th, div, span, a, p'))
    .filter(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 5 || r.height < 5) return false;
      return !Array.from(el.children).some(c => {
        const cr = c.getBoundingClientRect();
        return cr.width > 5 && cr.height > 5;
      });
    });

  const centre = el => {
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  };

  const toNum = s => {
    const lastDot = s.lastIndexOf('.'), lastCom = s.lastIndexOf(',');
    if (lastCom > lastDot) return parseFloat(s.replace(/\./g, '').replace(',', '.'));
    return parseFloat(s.replace(/,/g, ''));
  };

  const prices = [];
  for (const el of leaves) {
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 60 || !/\d/.test(txt)) continue;
    if (!/[A-Z]{3}|kr\b/i.test(txt)) continue;
    const nums = txt.match(/[\d][\d.,]*[.,]\d{2}/g);
    if (!nums || !nums.length) continue;
    const c = centre(el);
    prices.push({
      x: c.x, y: c.y,
      price: toNum(nums[0]),
      was: nums.length > 1 ? toNum(nums[1]) : null,
    });
  }
  if (!prices.length) {
    return { cells: [], n_prices: 0, n_cols: 0, n_rows: 0,
             sample: document.body.innerText.replace(/\s+/g,' ').slice(0, 300) };
  }

  const minPx = Math.min(...prices.map(p => p.x));
  const minPy = Math.min(...prices.map(p => p.y));

  const colHeads = [], rowHeads = [];
  for (const el of leaves) {
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 40) continue;
    if (/[\d][\d.,]*[.,]\d{2}/.test(txt)) continue;   // pris, ikke dato
    const m = txt.match(DATE_RE);
    if (!m) continue;
    const c = centre(el);
    const e = { x: c.x, y: c.y, month: +m[1], day: +m[2] };
    if (c.y < minPy - 5) colHeads.push(e);
    else if (c.x < minPx - 5) rowHeads.push(e);
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
    """Kør søgningen og returnér matrix-celler + diagnostik."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="da-DK",
            timezone_id="Europe/Copenhagen",
            viewport={"width": 1500, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        DATA.mkdir(parents=True, exist_ok=True)

        page.goto(START_URL, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")

        # Cookie-banner: afvis alt ikke-nødvendigt (mest privatlivsvenlige valg)
        try:
            reject = page.get_by_text(re.compile(r"^\s*REJECT ALL\s*$", re.I)).first
            if reject.is_visible(timeout=4000):
                reject.click()
                page.wait_for_timeout(800)
        except Exception:
            pass  # intet banner - fortsæt

        filled = page.evaluate(FILL_JS, {
            "origin": ORIGIN, "originLabel": ORIGIN_LABEL,
            "dest": DEST, "destLabel": DEST_LABEL,
            "out": dk_date(OUTBOUND_DATE), "ret": dk_date(RETURN_DATE),
        })
        if not filled.get("ok"):
            page.screenshot(path=str(SHOT_FILE), full_page=True)
            browser.close()
            return {"cells": [], "error": filled.get("error")}

        # Klik den rigtige søgeknap, så sidens JS laver den krypterede submit
        page.click("button.fssubmit", timeout=PAGE_TIMEOUT)

        # Vent til priserne er tegnet
        try:
            page.wait_for_function(
                "() => /[A-Z]{3}\\s*[\\d.,]+[.,]\\d{2}/.test(document.body.innerText)",
                timeout=PAGE_TIMEOUT,
            )
        except Exception:
            pass  # screenshot tages alligevel, så fejlen kan ses
        page.wait_for_timeout(4000)

        page.screenshot(path=str(SHOT_FILE), full_page=True)
        result = page.evaluate(EXTRACT_JS)
        result["url"] = page.url
        result["filled"] = filled.get("applied")
        browser.close()
        return result


def pick(cells, out_date, ret_date):
    o = datetime.strptime(out_date, "%Y-%m-%d")
    r = datetime.strptime(ret_date, "%Y-%m-%d")
    for c in cells:
        if (c["out_month"], c["out_day"]) == (o.month, o.day) and \
           (c["ret_month"], c["ret_day"]) == (r.month, r.day):
            return c
    return None


def save_history(cells, mine):
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
        f"Ud: {OUTBOUND_DATE}  •  Hjem: {RETURN_DATE}\n\n"
    )
    if not mine:
        return (
            head
            + "⚠️ Kunne ikke aflæse prisen for dine datoer i dag.\n"
            + f"<i>{diag.get('n_prices', 0)} priser, "
            + f"{diag.get('n_rows', 0)}×{diag.get('n_cols', 0)} datoer fundet.</i>\n"
            + (f"<i>{diag['error']}</i>\n" if diag.get("error") else "")
            + "Se screenshottet — måske er siden ændret."
        )

    price = mine["price"]
    body = f"💰 <b>{dk(price)} {CURRENCY}</b>"
    if mine.get("was") and mine["was"] > price:
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
