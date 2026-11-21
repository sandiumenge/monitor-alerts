"""Large TVs (>= 50") on Marktplaats around both centers: sweep + attribute parsing.

    python -I tv.py sweep [--max 300]   all TV listings <= max price (both centers) -> data/tv_<date>.json
    python -I tv.py stats [file]        price study by size / resolution / panel / smart

Category: Audio, Tv en Foto (31) > Televisies (1120). Gentle: ~30 calls, mp_delay_s apart.
"""
import datetime as dt
import json
import re
import statistics
import sys
import time

import db
import hunt

L1, L2 = 31, 1120
NOT_WORKING = "Niet werkend"

# --- parsing -----------------------------------------------------------------
INCH_RX = re.compile(r"(?<![\d.,])(\d{2})(?:[.,]\d)?\s*(?:inch|in\b|\"|''|”|″|duim|zoll|-inch)", re.I)
CM_RX = re.compile(r"(?<!\d)(1\d{2}|2[0-2]\d)\s*cm", re.I)
# Model codes that start with the size: UE55TU7000, QE65Q80, GQ55, 55UK6300, OLED55C1, 55PUS7303, KD-55X85, TX-50..., 50A6BG
CODE_RX = re.compile(r"\b(?:UE|QE|GQ|GU|UN|OLED|KD-?|KDL-?|XR-?|TX-?|TH-?|TQ)?(\d{2})(?![xX×]\s?\d)(?=(?:[A-Z]{1,4}\d|(?!CM\b|INCH|IN\b|HZ\b|KG\b|MM\b|CM\s)[A-Z]{2,5}\b))")
RES_TXT = [("8K", r"\b8k\b"), ("4K", r"\b(4k|uhd|ultra ?hd|2160p?|3840)"), ("FHD", r"(full ?hd|fhd|1080p?|1920\s*x\s*1080)"),
           ("HD", r"(hd ?ready|\b720p?\b|1366)")]
SHARP = {"4k": "4K", "8k": "8K", "full-hd": "FHD", "hd-ready": "HD"}
PANEL_TXT = [("OLED", r"\b(w?oled|qd-?oled)\b"), ("QLED", r"\b(qled|neo ?qled|mini ?led|nanocell|qned)\b"),
             ("PLASMA", r"\bplasma\b"), ("LED", r"\bled\b"), ("LCD", r"\blcd\b")]
SMART_RX = re.compile(r"smart|android|google tv|webos|tizen|netflix|youtube|wifi|wi-fi|chromecast|ambilight", re.I)
DAMAGE_RX = re.compile(r"kapot|defect|stuk\b|gebroken|barst|scheur|streep|strepen|lijn(en)? in|vlek|backlight|"
                       r"geen beeld|doet het niet|niet werkend|onderdelen|voor parts|parts|reparatie|beschadig|schade|\bgebrek(en)?\b|blauw\w* scherm|scherm (vervang|kapot)\w*|af en toe|"
                       r"afwijking|gefixt|fixen|repareren|moet gemaakt|inbrand|"
                       r"donker\w* (baan|band|balk|vlek|plek|rand|strook|gedeelte|schaduw)|valt .{0,30}donker|donkerder|"
                       r"kras\w*|scratch\w*|slaat .{0,25}(groen|roze|paars|rood|blauw)|(groen|roze|paars)e? (waas|zweem|gloed)|"
                       r"licht\w* (plek|vlek)\w*|witte vlek|clouding|lichtlek|lekt licht|paneelslijtage|"
                       r"burn.?in|dode pixel|pixel(fout|s? defect)|flikker|knippert|start niet|gaat (steeds )?uit", re.I)
ACCESSORY_RX = re.compile(r"^(?!.*\b(tv|televisie)\b.*\b(met|incl|inclusief)\b).*\b(muurbeugel|beugel|wandbeugel|"
                          r"standaard|voet(en)?|afstandsbediening|remote|meubel|kast|tv-?kast|tv-?meubel|soundbar|"
                          r"kabel|mount|steun|lift|onderdel\w*|printplaat|firmware|moederbord|mainboard|voeding\w*|print|board|bord|t-?con|"
                          r"led ?strips?|doos|verpakking|computer|pc|laptop|projector|beamer|folie|table|tafel)\b", re.I)
BRANDS = "samsung|lg|sony|philips|panasonic|hisense|tcl|sharp|toshiba|grundig|xiaomi|jvc|telefunken|salora|blaupunkt|" \
         "medion|thomson|nokia|loewe|bang|metz|kiano|finlux|techwood|haier|akai|denver|strong|continental"
PREMIUM = {"lg", "sony", "samsung", "philips", "panasonic", "loewe", "bang"}

SAMSUNG_YEAR = {"H": 2014, "J": 2015, "K": 2016, "KU": 2016, "M": 2017, "MU": 2017, "NU": 2018, "RU": 2019, "TU": 2020,
                "AU": 2021, "BU": 2022, "CU": 2023, "DU": 2024, "Q6F": 2018, "Q7F": 2017}
LG_YEAR = {"UH": 2016, "UJ": 2017, "UK": 2018, "UM": 2019, "UN": 2020, "NANO": None, "UP": 2021, "UQ": 2022, "UR": 2023,
           "UT": 2024, "B6": 2016, "C6": 2016, "B7": 2017, "C7": 2017, "B8": 2018, "C8": 2018, "B9": 2019, "C9": 2019,
           "BX": 2020, "CX": 2020, "B1": 2021, "C1": 2021, "A1": 2021, "B2": 2022, "C2": 2022, "B3": 2023, "C3": 2023}


def has_damage(text):
    """A damage word counts unless negated just before it ("geen krassen", "no scratches")."""
    return any(not db.NEG.search(text[max(0, m.start() - 35):m.start()]) for m in DAMAGE_RX.finditer(text))


def first(rx_list, text):
    return next((k for k, rx in rx_list if re.search(rx, text, re.I)), None)


def inch_of(text):
    m = INCH_RX.search(text)
    if m and 19 <= int(m.group(1)) <= 86:
        return int(m.group(1))
    m = CODE_RX.search(text)
    if m and 32 <= int(m.group(1)) <= 86:
        return int(m.group(1))
    m = CM_RX.search(text)
    if m:
        return round(int(m.group(1)) / 2.54)
    return None


def year_of(text):
    t = text.upper()
    m = re.search(r"\b(?:UE|GU)\d{2}([A-Z]{1,2})\d{4}", t)
    if m and m.group(1) in SAMSUNG_YEAR:
        return SAMSUNG_YEAR[m.group(1)]
    m = re.search(r"\b(?:OLED)?\d{2}(UH|UJ|UK|UM|UN|UP|UQ|UR|UT|B6|C6|B7|C7|B8|C8|B9|C9|BX|CX|B1|C1|A1|B2|C2|B3|C3)", t)
    if m:
        return LG_YEAR.get(m.group(1))
    m = re.search(r"\b\d{2}(?:PUS|OLED|PML|PUD)\d{3}(\d)", t)  # Philips: last digit 2=2017 ... 8=2023
    if m and m.group(1) in "2345678":
        return 2015 + int(m.group(1))
    m = re.search(r"\b20(1[0-9]|2[0-5])\b", text)
    return 2000 + int(m.group(1)) if m else None


def parse(r):
    a = r.get("attrs") or {}
    text = f"{r['title']} {r.get('desc') or ''}"
    r["inch"] = inch_of(r["title"]) or inch_of(text)
    if not r["inch"] and a.get("screenSize") == "100 cm of meer":
        r["inch_src"] = ">=40 (attr)"
    r["res"] = SHARP.get((a.get("sharpness") or "").lower()) or first(RES_TXT, text)
    r["panel"] = first(PANEL_TXT, text) or ((a.get("type") or "").upper() or None)
    r["year"] = year_of(text)
    if r["panel"] == "PLASMA":  # plasmas were never 4K and stopped in 2014: any later year is a purchase date
        r["year"] = r["year"] if r["year"] and r["year"] <= 2014 else None
    elif not r["res"] and r["year"] and r["year"] >= 2017 and (r["inch"] or 0) >= 50:
        r["res"] = "4K"  # practically every 50"+ TV since 2017 is 4K
    r["smart"] = not re.search(r"geen smart|niet smart|no smart|not smart", text, re.I) and bool(SMART_RX.search(text) or "smarttv" in str(a.get("properties", "")).lower())
    hz = re.search(r"(100|120|144)\s*hz", text, re.I)
    r["hz"] = int(hz.group(1)) if hz else (int(a["refreshRate"].split()[0]) if a.get("refreshRate") not in (None, "50 Hz") else None)
    b = re.search(rf"\b({BRANDS})\b", text, re.I) or re.search(rf"\b({BRANDS})\b", a.get("brand", ""), re.I)
    r["brand"] = b.group(1).lower() if b else (a.get("brand") or "").lower() or None
    r["cond"] = a.get("condition")
    r["damage"] = has_damage(text) or r["cond"] == NOT_WORKING
    r["accessory"] = bool(ACCESSORY_RX.search(r["title"]))
    return r


# --- sweep -------------------------------------------------------------------
def sweep(max_eur=300):
    rows, calls = {}, 0
    bands = [(None, 7500), (7500, 15000), (15000, max_eur * 100)]
    for city, pc in hunt.CFG["centers"].items():
        for lo, hi in bands:
            for page in range(15):
                p = {"l1CategoryId": L1, "l2CategoryIds": L2, "limit": 100, "offset": page * 100, "postcode": pc,
                     "distanceMeters": hunt.radius_m(city), "viewOptions": "list-view",
                     "sortBy": "SORT_INDEX", "sortOrder": "DECREASING",
                     "attributeRanges[]": f"PriceCents:{lo if lo is not None else 'null'}:{hi}"}
                j = hunt.mp_get(p)
                calls += 1
                L = j.get("listings") or []
                for x in L:
                    r = hunt.mp_row(x)
                    old = rows.get(r["id"])
                    r["near"] = f"{old['near']},{city}" if old and city not in old["near"] else (old or {}).get("near", city)
                    if old:
                        r["km"] = min(old["km"], r["km"])
                    rows[r["id"]] = r
                time.sleep(hunt.CFG["mp_delay_s"] + 0.5)
                if len(L) < 100:
                    break
    return [parse(r) for r in rows.values()], calls


# --- study -------------------------------------------------------------------
def band(i):
    return None if not i else "≥75" if i >= 75 else "65–74" if i >= 65 else "55–64" if i >= 55 else "50–54" if i >= 50 else "<50"


def stats(rows):
    ok = [r for r in rows if r["inch"] and r["inch"] >= 50 and r.get("price") and not r["damage"] and not r["accessory"]
          and r.get("ptype") in ("FIXED", "MIN_BID", "NEGOTIABLE", "FAST_BID", None)]
    print(f"{len(rows)} TV listings; {len(ok)} usable ≥50\" with a price, not damaged, not accessories")
    print(f"size known for {sum(1 for r in rows if r['inch'])}, ≥50\" (any condition) {sum(1 for r in rows if (r['inch'] or 0) >= 50)}")

    def line(label, rs):
        if not rs:
            return
        p = sorted(r["price"] for r in rs)
        q = statistics.quantiles(p, n=4) if len(p) >= 4 else [p[0], p[len(p) // 2], p[-1]]
        cheap = sum(1 for x in p if x <= 50)
        print(f"  {label:<28} n={len(p):>3}  min €{p[0]:>4.0f}  p25 €{q[0]:>4.0f}  median €{q[1]:>4.0f}  p75 €{q[2]:>4.0f}  ≤€50: {cheap}")

    for key, vals in (("band", ["50–54", "55–64", "65–74", "≥75"]), ("res", ["HD", "FHD", "4K", "8K", None]),
                      ("panel", ["OLED", "QLED", "LED", "LCD", "PLASMA", None])):
        print(f"\nby {key}:")
        for v in vals:
            line(str(v), [r for r in ok if (band(r["inch"]) if key == "band" else r[key]) == v])
    print("\nsize × res:")
    for b in ["50–54", "55–64", "65–74", "≥75"]:
        for v in ["FHD", "4K", None]:
            line(f"{b} {v}", [r for r in ok if band(r["inch"]) == b and r["res"] == v])
    print("\nsmart / not smart (4K 55–64):")
    for s in (True, False):
        line(f"smart={s}", [r for r in ok if r["smart"] == s and r["res"] == "4K" and band(r["inch"]) == "55–64"])
    print("\nby year (any ≥50\"):")
    for y in sorted({r["year"] for r in ok if r["year"]}):
        line(str(y), [r for r in ok if r["year"] == y])
    line("year unknown", [r for r in ok if not r["year"]])
    print("\nby brand:")
    for b in sorted({r["brand"] for r in ok if r["brand"]}, key=lambda b: -sum(1 for r in ok if r["brand"] == b))[:12]:
        line(b, [r for r in ok if r["brand"] == b])
    print("\nposting age:")
    today = dt.date.today()
    for lo, hi in ((0, 2), (3, 7), (8, 14), (15, 30), (31, 9999)):
        line(f"{lo}-{hi} days old", [r for r in ok if r.get("date") and len(r["date"]) == 10
                                     and lo <= (today - dt.date.fromisoformat(r["date"])).days <= hi])
    print("\ncheapest usable ≥50\":")
    for r in sorted(ok, key=lambda r: r["price"])[:30]:
        print(f"  €{r['price']:>4.0f} {r['inch']}\" {r['res'] or '?':<4} {r['panel'] or '?':<6} smart={'y' if r['smart'] else 'n'} "
              f"{r['year'] or '':<5} {r['brand'] or '?':<9} {r['date']} {r['city']} {r['km']}km  {r['title'][:55]}  {r['url']}")


def main(argv):
    cmd = argv[0] if argv else "stats"
    if cmd == "sweep":
        mx = int(argv[argv.index("--max") + 1]) if "--max" in argv else 300
        rows, calls = sweep(mx)
        out = hunt.DATA / f"tv_{dt.date.today().isoformat()}.json"
        hunt.save(out, rows)
        print(f"{len(rows)} TV listings, {calls} calls -> {out}")
    elif cmd == "stats":
        f = argv[1] if len(argv) > 1 else max(hunt.DATA.glob("tv_*.json")).as_posix()
        stats([parse(r) for r in hunt.load(f, [])])


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
