#!/usr/bin/env python3
"""
Ireland SME No-Website Scanner
-----------------------------------
Daily agent that finds Irish SMEs with no website using
OpenStreetMap data via the free Overpass API.
No API key required.
"""

import re
import json
import time
from datetime import datetime, date
from pathlib import Path
from urllib.parse import quote_plus

import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────

SCAN_DATE    = date.today().isoformat()
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Republic of Ireland bounding box (excludes most of NI)
IRELAND_BBOX = "51.4,-10.7,55.4,-5.9"

# Max results per category query
MAX_PER_QUERY = 300

# (display label, overpass tag filter)
BUSINESS_CATEGORIES = [
    ("Hospitality",   '"tourism"~"hotel|guest_house|hostel|bed_and_breakfast|motel"'),
    ("Food & Drink",  '"amenity"~"restaurant|cafe|pub|bar|fast_food|food_court|ice_cream"'),
    ("Retail",        '"shop"'),
    ("Professional",  '"office"~"company|consulting|accountant|estate_agent|travel_agent|insurance|financial|architect|solicitor"'),
    ("Beauty",        '"amenity"~"hairdresser|beauty_salon|barber|nail_salon|tanning_salon"'),
    ("Health",        '"amenity"~"dentist|optician|veterinary|physiotherapist|pharmacy"'),
    ("Trades",        '"craft"'),
    ("Leisure",       '"leisure"~"fitness_centre|sports_centre|gym|dance|yoga|studio"'),
    ("Automotive",    '"shop"~"car|car_repair|tyres|motorcycle|car_parts|fuel"'),
    ("Food Shops",    '"shop"~"bakery|butcher|deli|greengrocer|convenience|supermarket|farm"'),
    ("Accommodation", '"amenity"~"hotel"'),
    ("Education",     '"amenity"~"driving_school|music_school|language_school"'),
    ("Tourism",       '"tourism"~"attraction|museum|gallery|viewpoint|artwork"'),
]

# Priority score: how urgently this type of business needs a website (1–10)
CATEGORY_PRIORITY = {
    "Hospitality":   10,
    "Food & Drink":   9,
    "Professional":   9,
    "Retail":         8,
    "Beauty":         8,
    "Health":         8,
    "Food Shops":     8,
    "Tourism":        9,
    "Leisure":        7,
    "Trades":         7,
    "Automotive":     6,
    "Accommodation":  10,
    "Education":      8,
}

# ── OVERPASS QUERY ────────────────────────────────────────────────────────────

def fetch_category(label: str, tag_filter: str) -> list:
    """Query Overpass for businesses of one category that have no website tag."""
    query = f"""
[out:json][timeout:90][bbox:{IRELAND_BBOX}];
(
  node[{tag_filter}]["name"][!"website"][!"contact:website"];
  way[{tag_filter}]["name"][!"website"][!"contact:website"];
);
out center {MAX_PER_QUERY};
"""
    try:
        resp = requests.post(
            OVERPASS_URL,
            data={"data": query},
            timeout=120,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json().get("elements", [])
    except Exception as exc:
        print(f"    [WARN] Overpass error for {label}: {exc}")
        return []


def parse_element(el: dict, category: str) -> dict | None:
    """Convert an OSM element into a clean business dict."""
    tags = el.get("tags", {})
    name = tags.get("name", "").strip()
    if not name:
        return None

    # Coordinates
    if el["type"] == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        center = el.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")

    if not lat or not lon:
        return None

    # Address
    addr_parts = [
        tags.get("addr:housenumber", ""),
        tags.get("addr:street", ""),
        tags.get("addr:city", "")
        or tags.get("addr:town", "")
        or tags.get("addr:village", ""),
        tags.get("addr:county", ""),
    ]
    address = ", ".join(p for p in addr_parts if p).strip(", ") or "Ireland"

    # Contact
    phone = (
        tags.get("phone")
        or tags.get("contact:phone")
        or tags.get("mobile")
        or tags.get("contact:mobile")
        or ""
    ).strip()

    email = (
        tags.get("email")
        or tags.get("contact:email")
        or ""
    ).strip()

    facebook = (
        tags.get("contact:facebook")
        or tags.get("facebook")
        or ""
    ).strip()

    # Google Maps search link
    maps_query = quote_plus(f"{name} {address}")
    maps_url   = f"https://www.google.com/maps/search/?api=1&query={maps_query}"

    # Likely domain names (useful for outreach)
    slug = _slug(name)
    likely_domains = [f"{slug}.ie", f"{slug}.com"] if slug else []

    return {
        "name":           name,
        "category":       category,
        "address":        address,
        "phone":          phone,
        "email":          email,
        "facebook":       facebook,
        "lat":            round(lat, 5),
        "lon":            round(lon, 5),
        "maps_url":       maps_url,
        "likely_domains": likely_domains,
        "priority":       CATEGORY_PRIORITY.get(category, 5),
        "osm_id":         f"{el['type']}/{el.get('id')}",
    }


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"['''\-&]", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s[:28]


# ── SCAN ─────────────────────────────────────────────────────────────────────

def run_scan() -> list:
    print(f"\n{'='*60}")
    print(f"  Ireland SME No-Website Scanner")
    print(f"  Scan date  : {SCAN_DATE}")
    print(f"  Categories : {len(BUSINESS_CATEGORIES)}")
    print(f"{'='*60}\n")

    all_businesses: list = []
    seen_ids: set        = set()

    for label, tag_filter in BUSINESS_CATEGORIES:
        print(f"  Scanning: {label}...")
        elements = fetch_category(label, tag_filter)

        new = 0
        for el in elements:
            osm_id = f"{el['type']}/{el.get('id')}"
            if osm_id in seen_ids:
                continue
            seen_ids.add(osm_id)

            biz = parse_element(el, label)
            if biz:
                all_businesses.append(biz)
                new += 1

        print(f"           → {new} businesses without a website")
        time.sleep(4)   # polite rate limit for Overpass

    all_businesses.sort(key=lambda b: b["priority"], reverse=True)

    print(f"\n  Total: {len(all_businesses)} Irish SMEs found with no website")
    return all_businesses


# ── OUTPUT ────────────────────────────────────────────────────────────────────

def save_results(businesses: list):
    out = Path("results")
    out.mkdir(exist_ok=True)

    payload = {
        "scan_date":   SCAN_DATE,
        "scanned_at":  datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total":       len(businesses),
        "businesses":  businesses,
    }

    _write_json(out / f"{SCAN_DATE}.json", payload)
    _write_json(out / "latest.json", payload)
    (out / "latest.md").write_text(_build_markdown(businesses), encoding="utf-8")

    print(f"\n  Saved → results/{SCAN_DATE}.json")
    print(f"  Saved → results/latest.json")
    print(f"  Saved → results/latest.md")


def _write_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _build_markdown(businesses: list) -> str:
    today_fmt   = datetime.now().strftime("%B %d, %Y")
    by_cat      = {}
    for b in businesses:
        by_cat.setdefault(b["category"], []).append(b)

    has_phone   = sum(1 for b in businesses if b["phone"])
    has_email   = sum(1 for b in businesses if b["email"])
    has_facebook= sum(1 for b in businesses if b["facebook"])

    lines = [
        "# 🇮🇪 Ireland SME No-Website Report",
        "",
        f"**Date:** {today_fmt}  ",
        f"**Total SMEs without a website:** {len(businesses)}  ",
        f"**Have phone number:** {has_phone}  ",
        f"**Have email:** {has_email}  ",
        f"**Have Facebook only:** {has_facebook}",
        "",
        "> Irish SMEs identified as having no website, sourced from OpenStreetMap.",
        "> Priority badge: 🔴 High · 🟠 Medium · 🟡 Lower",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Category | Businesses | Priority |",
        "|----------|-----------|----------|",
    ]

    sorted_cats = sorted(
        by_cat.items(),
        key=lambda x: -CATEGORY_PRIORITY.get(x[0], 5)
    )

    for cat, items in sorted_cats:
        p = CATEGORY_PRIORITY.get(cat, 5)
        badge = "🔴" if p >= 9 else "🟠" if p >= 7 else "🟡"
        lines.append(f"| {badge} {cat} | {len(items)} | {p}/10 |")

    lines += ["", "---", ""]

    for cat, items in sorted_cats:
        p     = CATEGORY_PRIORITY.get(cat, 5)
        badge = "🔴" if p >= 9 else "🟠" if p >= 7 else "🟡"

        lines += [
            f"## {badge} {cat}",
            f"_{len(items)} businesses — Priority {p}/10_",
            "",
            "| Business | Address | Phone | Email | Facebook | Maps |",
            "|----------|---------|-------|-------|----------|------|",
        ]

        for b in items:
            name = b["name"][:38].replace("|", "-")
            addr = b["address"][:32].replace("|", "-")
            ph   = b["phone"]   or "—"
            em   = b["email"]   or "—"
            fb   = f"[FB]({b['facebook']})" if b["facebook"] else "—"
            lines.append(
                f"| {name} | {addr} | {ph} | {em} | {fb} | [Maps]({b['maps_url']}) |"
            )

        lines += ["", "---", ""]

    lines.append(
        "_Data from OpenStreetMap contributors (ODbL). "
        "Scanned daily via GitHub Actions._"
    )
    return "\n".join(lines)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    found = run_scan()
    save_results(found)
