#!/usr/bin/env python3
"""
Diagnose: hvorfor fejler Air China-aflæsningen i GitHub Actions?

Kører fire trin og rapporterer hvert enkelt, i stedet for at fejle stille:

  1. Kan vi overhovedet nå airchina.dk herfra?
  2. Bliver en almindelig HTTP-klient mødt af bot-beskyttelsen?
  3. Kan Playwright starte en browser?
  4. Slipper en ægte browser igennem til priserne?

Trin 2 og 4 er de afgørende: hvis 2 blokeres men 4 lykkes, er det klienten
der afvises. Hvis begge blokeres, er det IP-adressen. Så ved vi det -
frem for at gætte.

Sender resultatet til Telegram og skriver det i Actions-loggen.
"""

import os
import sys
import traceback
from html import escape

import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BLOCK_MARKERS = ("Pardon Our Interruption", "Imperva", "Incapsula", "distil")

results = []


def log(step, ok, detail):
    mark = "✅" if ok else "❌"
    line = f"{mark} <b>{escape(step)}</b>\n   {escape(detail)}"
    results.append(line)
    print(f"{mark} {step}: {detail}", flush=True)


def blocked(text):
    """Er svaret bot-beskyttelsens blokeringsside?"""
    found = [m for m in BLOCK_MARKERS if m.lower() in text.lower()]
    return found


def step1_reachable():
    try:
        r = requests.get("https://www.airchina.dk/", headers={"User-Agent": UA}, timeout=30)
        hits = blocked(r.text)
        if hits:
            log("1. Nå airchina.dk", False,
                f"HTTP {r.status_code}, men BLOKERET ({', '.join(hits)})")
            return False
        log("1. Nå airchina.dk", True, f"HTTP {r.status_code}, {len(r.text)} tegn, ingen blokering")
        return True
    except Exception as exc:
        log("1. Nå airchina.dk", False, f"{type(exc).__name__}: {exc}")
        return False


def step2_plain_post():
    """Almindelig HTTP-klient mod søge-endpointet."""
    payload = {
        "SITE": "B000CA00", "LANGUAGE": "GB", "COUNTRY_SITE": "DK",
        "BOOKING_FLOW": "REVENUE", "TRIGGER_PAGE": "HOVT",
        "AIR_PARAM_PRICE_DISPLAY": "ADT_TAX_FEE", "langDateFormat": "dd/MM/yyyy",
        "IS_FLEXIBLE": "TRUE", "CAMPAIGN_ID": "DEFAULT", "DIRECT_NON_STOP": "false",
        "FSB1FromSource": "Copenhagen, CPH", "B_LOCATION_1": "CPH",
        "FSB1ToDestination": "Guangzhou, CAN", "E_LOCATION_1": "CAN",
        "TRIP_TYPE": "R", "B_DATE_1": "20/03/2027", "B_ANY_TIME_1": "TRUE",
        "B_DATE_2": "10/04/2027", "B_ANY_TIME_2": "TRUE",
        "NB_ADT": "1", "NB_CHD": "0", "NB_INF": "0", "NB_B15": "0", "NB_STU": "0",
        "CABIN": "E", "PROMO_MODE": "PROMO_CODE", "searchFromPage": "HOVT",
        "isRefxRedirect": "false",
    }
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": "https://www.airchina.dk/"})
        s.get("https://www.airchina.dk/", timeout=30)
        r = s.post("https://www.airchina.dk/CAPortal/dyn/portal/doEnc",
                   data=payload, timeout=60)
        hits = blocked(r.text)
        if hits:
            log("2. HTTP-klient mod søgning", False,
                f"HTTP {r.status_code} — BLOKERET ({', '.join(hits)})")
        else:
            log("2. HTTP-klient mod søgning", True,
                f"HTTP {r.status_code}, {len(r.text)} tegn — sluppet igennem")
        return not hits
    except Exception as exc:
        log("2. HTTP-klient mod søgning", False, f"{type(exc).__name__}: {exc}")
        return False


def step3_browser_starts():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log("3. Playwright installeret", False, f"import fejlede: {exc}")
        return False
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            v = b.version
            b.close()
        log("3. Playwright installeret", True, f"Chromium {v} startede")
        return True
    except Exception as exc:
        log("3. Playwright installeret", False, f"{type(exc).__name__}: {exc}")
        return False


def step4_real_browser():
    """Det afgørende trin: slipper en ægte browser igennem herfra?"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        log("4. Ægte browser mod siden", False, "sprunget over (Playwright mangler)")
        return False
    try:
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            page = br.new_context(user_agent=UA, locale="da-DK").new_page()
            page.goto("https://www.airchina.dk/", timeout=60000,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            text = page.content()
            title = page.title()
            has_form = page.locator("form[name=flight-search-form]").count() > 0
            br.close()
        hits = blocked(text)
        if hits:
            log("4. Ægte browser mod siden", False,
                f"BLOKERET ({', '.join(hits)}) — titel: {title!r}")
            return False
        log("4. Ægte browser mod siden", True,
            f"titel: {title!r}, søgeformular til stede: {has_form}")
        return True
    except Exception as exc:
        log("4. Ægte browser mod siden", False, f"{type(exc).__name__}: {exc}")
        return False


def verdict(r1, r2, r3, r4):
    if r4:
        return ("🟢 <b>Browseren slipper igennem herfra.</b> Bot-beskyttelsen er "
                "ikke årsagen — fejlen ligger et andet sted i kørslen.")
    if r3 and not r4:
        return ("🔴 <b>Browseren blev blokeret herfra.</b> Air Chinas "
                "bot-beskyttelse afviser GitHubs IP-adresser. Kilden kan ikke "
                "køre i skyen — kun lokalt fra din egen forbindelse.")
    if not r3:
        return ("🟠 <b>Browseren kunne ikke starte.</b> Det er et "
                "installationsproblem i Actions, ikke bot-beskyttelse — "
                "og det kan rettes.")
    return "⚪ Uklart resultat — se detaljerne ovenfor."


def main():
    r1 = step1_reachable()
    r2 = step2_plain_post()
    r3 = step3_browser_starts()
    r4 = step4_real_browser()

    msg = ("🔬 <b>Diagnose: Air China fra GitHub Actions</b>\n\n"
           + "\n\n".join(results)
           + "\n\n" + verdict(r1, r2, r3, r4))

    print("\n" + "=" * 60)
    print(verdict(r1, r2, r3, r4))

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=30,
            ).raise_for_status()
        except Exception as exc:
            print(f"kunne ikke sende til Telegram: {exc}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
