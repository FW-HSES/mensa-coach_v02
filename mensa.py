import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://sws2.maxmanager.xyz"
LISTEN_SEITEN = [BASE + "/index.php?mode=bed", BASE + "/"]
API = BASE + "/inc/ajax-php_konnektor.inc.php"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELL = "openai/gpt-oss-120b"
MENSEN = {"flandernstrasse": "FLANDERN", "stadtmitte": "STADTMITTE"}
KATEGORIEN = ["Veganer Renner", "Hauptgericht", "Mensa Special", "All Time Classics"]
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
KOPF = {"User-Agent": "Mozilla/5.0 (compatible; mensa-coach)"}
ZEIT = ZoneInfo("Europe/Berlin")


def sauber(text):
    return " ".join(text.split())


def zahl(text):
    return float(text.replace(",", ".")) if text else None


def hole_nummern():
    html = ""
    for url in LISTEN_SEITEN:
        r = requests.get(url, headers=KOPF, timeout=30)
        html = r.text
        print("Auswahlliste", url, "->", r.status_code, len(html), "Zeichen")
        if re.search("FLANDERN", html, re.I) and re.search("STADTMITTE", html, re.I):
            break
    optionen = re.findall(r'<option[^>]*value\s*=\s*["\']?(\d+)["\']?[^>]*>(.*?)</option>', html, re.I | re.S)
    ids = {}
    for name, such in MENSEN.items():
        for nr, text in optionen:
            if such in text.upper():
                ids[name] = int(nr)
                break
        if name not in ids:
            treffer = re.search(such, html, re.I)
            if treffer:
                davor = html[max(0, treffer.start() - 300):treffer.start()]
                zahlen = re.findall(r'(?:value|loc\w*|id)\s*=\s*["\']?(\d+)', davor, re.I)
                if zahlen:
                    ids[name] = int(zahlen[-1])
        print("Mensa-Nummer", name, "=", ids.get(name))
    if len(ids) < len(MENSEN):
        print("Nummern nicht gefunden. Anfang der Seite:")
        print(html[:1500])
        sys.exit(1)
    return ids


def preis(text):
    t = text.replace("€", " EUR").replace("\xa0", " ")
    for muster in (r"STUDIS\s*\|?\s*(\d+[.,]\d{1,2})\s*EUR", r"EUR\s*(\d+[.,]\d{1,2})\s*/", r"(\d+[.,]\d{1,2})\s*EUR"):
        treffer = re.search(muster, t, re.I)
        if treffer:
            return zahl(treffer.group(1))
    return None


def naehrwert(text, label):
    treffer = re.search(label + r"\s*([0-9]+(?:[.,][0-9]+)?)", text, re.I)
    return zahl(treffer.group(1)) if treffer else None


def kcal(text):
    treffer = re.search(r"Brennwert:\s*[0-9.,]+\s*kj\s*/\s*([0-9.,]+)\s*kcal", text, re.I)
    if not treffer:
        treffer = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*kcal", text, re.I)
    return zahl(treffer.group(1)) if treffer else None


def lies_gericht(zeile, kat):
    kategorie = next((k for k in KATEGORIEN if k.lower() in (kat or "").lower()), None)
    if not kategorie:
        return None
    text = sauber(zeile.get_text(" ", strip=True))
    symbole = " ".join((e.get("title") or e.get("alt") or "") for e in zeile.find_all(["img", "span"])).lower()
    name = ""
    for auswahl in (".mealText span", ".splMealText span", ".col-md-8 span", "span"):
        e = zeile.select_one(auswahl)
        if e and e.get_text(strip=True):
            name = sauber(e.get_text(" ", strip=True))
            break
    vegan = "vegan" in symbole
    return {
        "kategorie": kategorie, "name": name or text[:80], "preis_studierende": preis(text),
        "vegan": vegan, "vegetarisch": vegan or "vegetar" in symbole,
        "kcal_100g": kcal(text), "eiweiss_100g": naehrwert(text, "Eiweiß:"),
    }


def lies_tag(nr, datum, sitzung):
    montag = datum - dt.timedelta(days=datum.weekday())
    daten = {
        "func": "make_spl", "locId": str(nr), "date": datum.isoformat(), "lang": "de",
        "startThisWeek": montag.isoformat(),
        "startNextWeek": (montag + dt.timedelta(days=7)).isoformat(),
    }
    kopf = {"Origin": BASE, "Referer": LISTEN_SEITEN[0], "X-Requested-With": "XMLHttpRequest", **KOPF}
    r = sitzung.post(API, data=daten, headers=kopf, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    gerichte, kat = [], None
    for zeile in soup.select(".row"):
        klassen = set(zeile.get("class", []))
        if "gruppenkopf" in klassen:
            kat = sauber(zeile.get_text(" ", strip=True))
        elif "splMeal" in klassen:
            g = lies_gericht(zeile, kat)
            if g:
                gerichte.append(g)
    return gerichte, r.text


def bewerte(gerichte):
    einmalig = {}
    for g in gerichte:
        einmalig.setdefault(g["name"], g)
    eintraege = [{"id": i, "name": n, "kcal_100g": g["kcal_100g"], "eiweiss_100g": g["eiweiss_100g"]}
                 for i, (n, g) in enumerate(einmalig.items())]
    if not eintraege:
        return {}
    kopf = {"Authorization": "Bearer " + os.environ.get("GROQ_API_KEY", "")}
    frage = (
        "Bewerte jedes Mensa-Gericht. Antworte nur mit JSON der Form "
        '{"bewertungen":[{"id":0,"gesundheit":7,"kcal_portion":650,"begruendung":"max. 12 Woerter"}]}. '
        "gesundheit: ganze Zahl 1 (sehr ungesund) bis 10 (sehr gesund: viel Gemuese, wenig Fett, Zucker, Salz). "
        "kcal_portion: geschaetzte Kalorien einer ueblichen Portion. Gerichte: " + json.dumps(eintraege, ensure_ascii=False)
    )
    r = None
    try:
        r = requests.post(GROQ_URL, headers=kopf, timeout=120, json={
            "model": GROQ_MODELL, "temperature": 0.2, "max_completion_tokens": 4000,
            "reasoning_effort": "low", "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": "Du bist Ernaehrungsberater. Antworte nur mit JSON."},
                         {"role": "user", "content": frage}]})
        r.raise_for_status()
        antwort = r.json()
        print("Verbrauchte Tokens:", antwort.get("usage"))
        daten = json.loads(antwort["choices"][0]["message"]["content"])
        return {eintraege[int(b["id"])]["name"]: b for b in daten["bewertungen"]}
    except Exception as fehler:
        print("FEHLER bei Groq:", fehler)
        print("Antwort:", r.text[:800] if r is not None else "keine")
        try:
            modelle = requests.get("https://api.groq.com/openai/v1/models", headers=kopf, timeout=30).json()
            print("Verfuegbare Modelle:", [m["id"] for m in modelle.get("data", [])])
        except Exception as fehler2:
            print("Modellliste nicht lesbar:", fehler2)
        return {}


def main():
    ids = hole_nummern()
    heute = dt.datetime.now(ZEIT).date()
    montag = heute - dt.timedelta(days=heute.weekday())
    if heute.weekday() >= 5:
        montag += dt.timedelta(days=7)
    sitzung = requests.Session()
    ergebnis, alle = {}, []
    for name, nr in ids.items():
        tage = []
        for i in range(5):
            datum = montag + dt.timedelta(days=i)
            try:
                gerichte, roh = lies_tag(nr, datum, sitzung)
            except Exception as fehler:
                print("FEHLER", name, datum, fehler)
                gerichte, roh = [], ""
            print(f"{name} {datum} {WOCHENTAGE[i]}: {len(gerichte)} Gerichte")
            if not gerichte:
                print("Anfang der Antwort:", roh[:500].replace("\n", " "))
            tage.append({"datum": datum.isoformat(), "wochentag": WOCHENTAGE[i], "gerichte": gerichte})
            alle += gerichte
        ergebnis[name] = tage
    bewertungen = bewerte(alle)
    for g in alle:
        b = bewertungen.get(g["name"], {})
        try:
            g["gesundheit"] = max(1, min(10, int(b["gesundheit"])))
            g["kcal_portion"] = int(b["kcal_portion"])
            g["begruendung"] = str(b["begruendung"])
        except (KeyError, TypeError, ValueError):
            g["gesundheit"], g["kcal_portion"], g["begruendung"] = None, None, None
    os.makedirs("data", exist_ok=True)
    stand = dt.datetime.now(ZEIT).replace(microsecond=0).isoformat()
    for name, tage in ergebnis.items():
        if not any(t["gerichte"] for t in tage):
            print("Keine Gerichte fuer", name, "- Datei bleibt unveraendert")
            continue
        with open(f"data/{name}.json", "w", encoding="utf-8") as f:
            json.dump({"stand": stand, "tage": tage}, f, ensure_ascii=False, indent=1)
        print("Geschrieben: data/" + name + ".json")


if __name__ == "__main__":
    main()
