#!/usr/bin/env python3
"""
Ireland SME Website Opportunity Scanner
-----------------------------------------
Finds small Irish businesses that have no website (or a poor one)
and are contactable — ranked by how sellable a website is to them.

Sources  : OpenStreetMap via Overpass API (free, no key needed)
Checks   : DNS domain lookup to verify no website exists
Filters  : Removes big chains, businesses with no contact info
Output   : results/latest.md  — ranked prospect list with phone/email
"""

import re
import json
import time
import socket
import concurrent.futures
from datetime import datetime, date
from pathlib import Path
from urllib.parse import quote_plus

import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────

SCAN_DATE    = date.today().isoformat()
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
IRELAND_BBOX = "51.4,-10.7,55.4,-5.9"
MAX_PER_QUERY = 300

# ── KNOWN CHAINS TO EXCLUDE ───────────────────────────────────────────────────

KNOWN_CHAINS = {
    # Supermarkets
    "tesco", "lidl", "aldi", "supervalu", "centra", "spar", "eurospar",
    "dunnes", "marks and spencer", "m&s", "costcutter", "londis",
    # Fast food
    "mcdonalds", "mcdonald's", "burger king", "kfc", "subway", "supermacs",
    "dominos", "dominoes", "papa johns", "five guys", "nandos", "abrakebabra",
    "thunders", "thunder road",
    # Coffee
    "starbucks", "costa coffee", "insomnia", "butlers chocolate",
    # Pharmacy
    "boots", "lloyds pharmacy", "hickeys pharmacy", "carraig pharmacy",
    "well pharmacy", "life pharmacy",
    # Petrol / convenience
    "applegreen", "circle k", "texaco", "shell", "maxol", "esso", "topaz",
    # Banks
    "aib", "bank of ireland", "permanent tsb", "ulster bank", "kbc",
    "an post", "credit union",
    # Hotels (big chains)
    "marriott", "hilton", "radisson", "holiday inn", "ibis", "novotel",
    "maldron", "clayton hotel", "jurys inn", "citywest", "sandymount",
    # Clothing / retail chains
    "penneys", "primark", "h&m", "zara", "next", "tk maxx", "tkmaxx",
    "river island", "topshop", "new look", "debenhams",
    # Other big names
    "ikea", "harvey norman", "currys", "pc world",
    "eir", "vodafone", "three store", "sky store",
    "paddy power", "ladbrokes", "betfred", "boyle sports",
    "specsavers", "vision express",
}

# ── BUSINESS CATEGORIES ───────────────────────────────────────────────────────

BUSINESS_CATEGORIES = [
    ("Hospitality",   '"tourism"~"hotel|guest_house|hostel|bed_and_breakfast|motel"'),
    ("Food & Drink",  '"amenity"~"restaurant|cafe|pub|bar|fast_food|ice_cream"'),
    ("Retail",        '"shop"~"clothes|shoes|books|gifts|hardware|electronics|florist|jewellery|toys|sports|garden|outdoor|pet|art|craft|antique|vintage|second_hand"'),
    ("Professional",  '"office"~"company|consulting|accountant|estate_agent|travel_agent|insurance|architect|solicitor|financial"'),
    ("Beauty",        '"amenity"~"hairdresser|beauty_salon|barber|nail_salon|tanning_salon"'),
    ("Health",        '"amenity"~"dentist|optician|veterinary|physiotherapist"'),
    ("Trades",        '"craft"'),
    ("Leisure",       '"leisure"~"fitness_centre|sports_centre|gym|dance|yoga"'),
    ("Food Shops",    '"shop"~"bakery|butcher|deli|greengrocer|farm|organic|health_food"'),
    ("Tourism",       '"tourism"~"attraction|museum|gallery"'),
    ("Automotive",    '"shop"~"car_repair|tyres|motorcycle|car_parts"'),
]

# How much a website would help this type of business (1–10)
WEBSITE_NEED = {
    "Hospitality":  10,
    "Tourism":      10,
    "Food & Drink":  9,
    "Professional":  9,
    "Beauty":        8,
    "Health":        8,
    "Retail":        8,
    "Food Shops":    8,
    "Leisure":       8,
    "Trades":        7,
    "Automotive":    6,
}

# Poor/free website platforms — still worth approaching
POOR_WEBSITE_PLATFORMS = [
    "wix.com", "weebly.com", "squarespace.com", "yolasite.com",
    "jimdo.com", "webnode.com", "site123.com", "godaddy.com/website",
    "facebook.com", "instagram.com", "linktr.ee",
]

# ── OVERPASS ──────────────────────────────────────────────────────────────────

def fetch_category(label: str, tag_filter: str) -> list:
    query = f"""
[out:json][timeout:90][bbox:{IRELAND_BBOX}];
(
  node[{tag_filter}]["name"];
  way[{tag_filter}]["name"];
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
    tags = el.get("tags", {})
    name = tags.get("name", "").strip()
    if not name or len(name) < 3:
        return None

    # Filter out big chains immediately
    if _is_chain(name):
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
    address = ", ".join(p for p in addr_parts if p).strip(", ") or ""

    # Contact info
    phone = (
        tags.get("phone") or tags.get("contact:phone")
        or tags.get("mobile") or tags.get("contact:mobile") or ""
    ).strip()

    email = (
        tags.get("email") or tags.get("contact:email") or ""
    ).strip()

    # Existing website (from OSM tag — we'll verify it later)
    existing_website = (
        tags.get("website") or tags.get("contact:website") or ""
    ).strip()

    facebook = (
        tags.get("contact:facebook") or tags.get("facebook") or ""
    ).strip()

    maps_query = quote_plus(f"{name} {address} Ireland")
    maps_url   = f"https://www.google.com/maps/search/?api=1&query={maps_query}"

    return {
        "name":             name,
        "category":         category,
        "address":          address,
        "phone":            phone,
        "email":            email,
        "facebook":         facebook,
        "existing_website": existing_website,
        "maps_url":         maps_url,
        "website_need":     WEBSITE_NEED.get(category, 5),
        "osm_id":           f"{el['type']}/{el.get('id')}",
        "lat":              round(lat, 5),
        "lon":              round(lon, 5),
    }


def _is_chain(name: str) -> bool:
    n = name.lower().strip()
    for chain in KNOWN_CHAINS:
        if chain in n:
            return True
    # Filter out obvious large companies
    for keyword in [" plc", " group", " holdings", " international ltd",
                    " ireland ltd", "global ", "nationwide "]:
        if keyword in n:
            return True
    return False


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"['''\-&\s]", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s[:28]

# ── WEBSITE CHECK ─────────────────────────────────────────────────────────────

def check_website(biz: dict) -> str:
    """
    Returns one of:
      'none'   — no website found at all
      'poor'   — has a website but on a free/social platform
      'has'    — has a real website (skip this business)
    """
    existing = biz.get("existing_website", "")

    # If OSM says they have a website, check if it's poor quality
    if existing:
        for platform in POOR_WEBSITE_PLATFORMS:
            if platform in existing.lower():
                return "poor"
        return "has"   # real website, skip

    # No OSM website — do a DNS check on likely domains
    slug = _slug(biz["name"])
    if len(slug) < 3:
        return "none"

    for domain in [f"{slug}.ie", f"{slug}.com"]:
        try:
            socket.setdefaulttimeout(2)
            socket.gethostbyname(domain)
            return "has"   # domain resolves → likely has a website
        except Exception:
            pass

    return "none"


def check_websites_parallel(businesses: list) -> list:
    """Run website checks in parallel threads for speed."""
    print(f"\n  Checking websites for {len(businesses)} businesses...")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        future_to_biz = {pool.submit(check_website, b): b for b in businesses}
        done = 0
        for future in concurrent.futures.as_completed(future_to_biz):
            biz    = future_to_biz[future]
            status = future.result()
            biz["website_status"] = status
            if status in ("none", "poor"):
                results.append(biz)
            done += 1
            if done % 100 == 0:
                print(f"    Checked {done}/{len(businesses)}...")

    print(f"  → {len(results)} businesses with no/poor website")
    return results


# ── SCORING ───────────────────────────────────────────────────────────────────

def score_prospect(biz: dict) -> int:
    score = 0

    # Contact reachability
    if biz["phone"]:  score += 40
    if biz["email"]:  score += 30
    if biz["facebook"]: score += 10   # active online but no real site

    # Website status
    if biz["website_status"] == "none": score += 25
    if biz["website_status"] == "poor": score += 35  # easier sell

    # Category need
    score += biz["website_need"] * 4

    return score


# ── SCAN ─────────────────────────────────────────────────────────────────────

def run_scan() -> list:
    print(f"\n{'='*60}")
    print(f"  Ireland SME Website Opportunity Scanner")
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

        print(f"           → {new} SMEs collected")
        time.sleep(3)

    print(f"\n  Collected {len(all_businesses)} SMEs total")

    # Only keep businesses we can actually contact
    contactable = [b for b in all_businesses if b["phone"] or b["email"]]
    print(f"  Contactable (have phone or email): {len(contactable)}")

    # Check which ones have no/poor website
    prospects = check_websites_parallel(contactable)

    # Score and sort
    for b in prospects:
        b["score"] = score_prospect(b)
    prospects.sort(key=lambda b: b["score"], reverse=True)

    print(f"\n  Final prospects: {len(prospects)}")
    return prospects


# ── OUTPUT ────────────────────────────────────────────────────────────────────

def save_results(prospects: list):
    out = Path("results")
    out.mkdir(exist_ok=True)

    payload = {
        "scan_date":  SCAN_DATE,
        "scanned_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total":      len(prospects),
        "prospects":  prospects,
    }

    _write_json(out / f"{SCAN_DATE}.json", payload)
    _write_json(out / "latest.json", payload)
    (out / "latest.md").write_text(_build_markdown(prospects), encoding="utf-8")

    print(f"\n  Saved → results/{SCAN_DATE}.json")
    print(f"  Saved → results/latest.json")
    print(f"  Saved → results/latest.md")


def _write_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _build_markdown(prospects: list) -> str:
    today_fmt  = datetime.now().strftime("%B %d, %Y")
    no_website = [p for p in prospects if p["website_status"] == "none"]
    poor_site  = [p for p in prospects if p["website_status"] == "poor"]
    has_phone  = sum(1 for p in prospects if p["phone"])
    has_email  = sum(1 for p in prospects if p["email"])

    by_cat = {}
    for p in prospects:
        by_cat.setdefault(p["category"], []).append(p)

    lines = [
        "# 🇮🇪 Ireland SME Website Prospects",
        "",
        f"**Date:** {today_fmt}  ",
        f"**Total prospects:** {len(prospects)}  ",
        f"**No website at all:** {len(no_website)}  ",
        f"**Poor/free website:** {len(poor_site)}  ",
        f"**Have phone:** {has_phone}  ",
        f"**Have email:** {has_email}",
        "",
        "> Ranked by how sellable a website is to them. All have been verified "
        "as having no website or a poor one, and have a phone or email for outreach.",
        "> Score = contact quality + website need + category priority.",
        "",
        "---",
        "",
        "## 📊 Summary by Category",
        "",
        "| Category | Prospects | Website Need |",
        "|----------|-----------|--------------|",
    ]

    for cat, items in sorted(by_cat.items(), key=lambda x: -WEBSITE_NEED.get(x[0], 5)):
        need  = WEBSITE_NEED.get(cat, 5)
        badge = "🔴" if need >= 9 else "🟠" if need >= 7 else "🟡"
        lines.append(f"| {badge} {cat} | {len(items)} | {need}/10 |")

    lines += ["", "---", "", "## 🏆 Top Prospects (ranked by score)", ""]

    # Top 20 overall
    lines += [
        "| # | Business | Category | Address | Phone | Email | Status | Score |",
        "|---|----------|----------|---------|-------|-------|--------|-------|",
    ]
    for i, p in enumerate(prospects[:20], 1):
        status = "❌ No website" if p["website_status"] == "none" else "⚠️ Poor site"
        name   = p["name"][:30].replace("|", "-")
        addr   = p["address"][:25].replace("|", "-") or "Ireland"
        ph     = p["phone"] or "—"
        em     = p["email"] or "—"
        lines.append(f"| {i} | [{name}]({p['maps_url']}) | {p['category']} | {addr} | {ph} | {em} | {status} | {p['score']} |")

    lines += ["", "---", ""]

    # Full list by category
    lines += ["## 📋 Full List by Category", ""]

    for cat, items in sorted(by_cat.items(), key=lambda x: -WEBSITE_NEED.get(x[0], 5)):
        need  = WEBSITE_NEED.get(cat, 5)
        badge = "🔴" if need >= 9 else "🟠" if need >= 7 else "🟡"
        lines += [
            f"### {badge} {cat} ({len(items)} prospects)",
            "",
            "| Business | Address | Phone | Email | Facebook | Status | Maps |",
            "|----------|---------|-------|-------|----------|--------|------|",
        ]
        for p in items:
            name   = p["name"][:35].replace("|", "-")
            addr   = (p["address"] or "Ireland")[:28].replace("|", "-")
            ph     = p["phone"] or "—"
            em     = p["email"] or "—"
            fb     = f"[FB]({p['facebook']})" if p["facebook"] else "—"
            status = "❌ No site" if p["website_status"] == "none" else "⚠️ Poor site"
            lines.append(
                f"| {name} | {addr} | {ph} | {em} | {fb} | {status} | [Maps]({p['maps_url']}) |"
            )
        lines += ["", "---", ""]

    lines.append("_Data from OpenStreetMap (ODbL). Scanned via GitHub Actions._")
    return "\n".join(lines)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    found = run_scan()
    save_results(found)
