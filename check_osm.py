#!/usr/bin/env python3
"""Giganet Epitesfigyelo - OSM (Overpass) ellenorzes GitHub Actions-bol.

Fuggetlen a felhasznalo helyi gepetol/halozatatol - ezt a szkriptet egy
GitHub Actions utemezett feladat futtatja, megbizhato internetkapcsolattal.

Mukodes:
  1) Lekerdezi az Overpass API-t (tobb tukrot, tobb korben probal).
  2. Ugyanazt a minosegi szurot alkalmazza, mint a weboldal: csak cimmel
     vagy ertelmes nevvel azonositott, tobblakasos epitesi objektumokat
     fogad el.
  3. Osszeveti a korabban mar latott talalatokkal (seen-ids.json, amit a
     workflow commitol vissza a repoba minden futas vegen).
  4. Uj talalat eseten emailt kuld (SMTP-beallitasok a GitHub Secrets-bol).
"""
import json
import os
import re
import smtplib
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from pathlib import Path

QUERY = (
    '[out:json][timeout:25];'
    '(way["building"="construction"]["addr:housenumber"](47.88,21.60,48.04,21.85);'
    'way["building"="construction"]["name"](47.88,21.60,48.04,21.85);'
    ');out center tags;'
)

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

SEEN_IDS_PATH = Path(__file__).parent / "seen-ids.json"
NAME_PLACEHOLDER_RE = re.compile(r"^(construction|építés|building)$", re.IGNORECASE)


def fetch_overpass():
    body = urllib.parse.urlencode({"data": QUERY}).encode("utf-8")
    for endpoint in ENDPOINTS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    endpoint,
                    data=body,
                    headers={"User-Agent": "Epitesfigyelo-OSM-Check/1.0 (GitHub Actions)"},
                )
                with urllib.request.urlopen(req, timeout=25) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if "elements" in payload:
                    print(f"  {endpoint} (probalkozas {attempt + 1}) -> sikeres valasz ({len(payload['elements'])} elem)")
                    return payload
                print(f"  {endpoint} (probalkozas {attempt + 1}) -> valasz jott, de nincs 'elements' mezo")
            except Exception as exc:  # noqa: BLE001 - szandekosan szeles, csak logolunk
                print(f"  {endpoint} (probalkozas {attempt + 1}) -> {exc}")
    return None


def is_meaningful_name(name):
    if not name:
        return False
    return not NAME_PLACEHOLDER_RE.match(name.strip())


def extract_candidates(payload):
    items = []
    for element in payload.get("elements", []):
        tags = element.get("tags") or {}
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
        if lat is None or lon is None:
            continue
        try:
            units = int(tags.get("building:units", 0) or 0)
        except (TypeError, ValueError):
            units = 0
        is_apartment = (
            tags.get("construction") == "apartments"
            or tags.get("building") == "apartments"
            or units > 1
        )
        has_address = bool(tags.get("addr:street") and tags.get("addr:housenumber"))
        has_name = is_meaningful_name(tags.get("name"))
        if not is_apartment:
            continue
        if not has_address and not has_name:
            continue
        if not units and not has_name:
            continue
        street = " ".join(filter(None, [tags.get("addr:street"), tags.get("addr:housenumber")]))
        title = tags.get("name") if has_name else (street or f"OSM építési objektum #{element['id']}")
        items.append(
            {
                "id": f"osm-way-{element['id']}",
                "title": title,
                "street": street or "Utca nincs megadva az OSM-adatban",
                "units": units,
                "link": f"https://www.openstreetmap.org/way/{element['id']}",
            }
        )
    return items


def send_email(new_items):
    smtp_server = os.environ.get("SMTP_SERVER")
    from_address = os.environ.get("FROM_ADDRESS")
    if not smtp_server or not from_address:
        print("SMTP nincs konfiguralva (SMTP_SERVER / FROM_ADDRESS hianyzik a Secrets-ben) - email kihagyva.")
        return

    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    to_address = os.environ.get("TO_ADDRESS") or "halasz.norbert@giganet.hu"

    lines = []
    for item in new_items:
        units_text = f"{item['units']} lakás" if item["units"] else "lakásszám nem közölt"
        lines.append(f"- {item['title']} ({item['street']}) - {units_text} - {item['link']}")

    body = (
        "Új, ellenőrzendő OSM építési objektum(ok) jelentek meg a Nyíregyháza Építésfigyelőben:\n\n"
        + "\n".join(lines)
        + "\n\nForrás: OpenStreetMap / Overpass. Ezt az ellenőrzést egy GitHub Actions "
        "ütemezett feladat futtatta, a felhasználó gépétől és hálózatától függetlenül."
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Giganet Építésfigyelő - {len(new_items)} új találat (Nyíregyháza)"
    msg["From"] = from_address
    msg["To"] = to_address

    with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as server:
        server.starttls()
        if username and password:
            server.login(username, password)
        server.sendmail(from_address, [to_address], msg.as_string())
    print(f"Email elkuldve ({len(new_items)} uj talalat).")


def main():
    payload = fetch_overpass()
    if payload is None:
        print("Egyik Overpass-vegpont sem valaszolt ervenyesen ezen a futason - kihagyva.")
        return

    candidates = extract_candidates(payload)
    seen_ids = set()
    if SEEN_IDS_PATH.exists():
        try:
            seen_ids = set(json.loads(SEEN_IDS_PATH.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            seen_ids = set()

    new_items = [item for item in candidates if item["id"] not in seen_ids]
    print(f"Talalt objektumok: {len(candidates)}, ebbol uj: {len(new_items)}")

    if new_items:
        try:
            send_email(new_items)
        except Exception as exc:  # noqa: BLE001
            print(f"Email kuldes hiba: {exc}")

    all_ids = sorted(set(item["id"] for item in candidates) | seen_ids)
    SEEN_IDS_PATH.write_text(json.dumps(all_ids, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
