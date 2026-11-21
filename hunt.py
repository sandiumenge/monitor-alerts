"""Monitor hunt pipeline: Marktplaats API sweep, Facebook import, extraction, ranking.

Usage (run from the monitor-hunt folder, python -I hunt.py <cmd>):
  mp      [--since YYYY-MM-DD]   sweep Marktplaats (category + queries) -> data/mp_<today>.json
  fbimport <file.json>           import rows returned by fb_collect.js -> data/fb_<today>.json
  rank    [--all]                extract size/model, filter, rank -> report.md, data/unknown_<today>.json
  learn   <file.json>            merge researched models (from the Haiku agent) into models.json
  seen                           mark everything in today's report as seen (state.json)
"""
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

if sys.stdout:  # None under pythonw (scheduled alert task)
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def radius_m(city):
    """Search radius around a center in meters (config radius_by_center, else radius_km)."""
    return int(CFG.get("radius_by_center", {}).get(city, CFG["radius_km"]) * 1000)
TODAY = dt.date.today()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def load(p, default):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(p, obj):
    try:
        txt = json.dumps(obj, ensure_ascii=False, indent=1)
        txt.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates (broken emoji) from the API: escape them
        txt = json.dumps(obj, ensure_ascii=True, indent=1)
    Path(p).write_text(txt, encoding="utf-8")


def clean(v):
    """Drop lone surrogates (broken emoji) that cannot be encoded as UTF-8."""
    return v.encode("utf-16", "surrogatepass").decode("utf-16", "replace") if isinstance(v, str) else v


STATE_P = ROOT / "state.json"
STATE = load(STATE_P, {"last_run": None, "seen": []})

# ---------------------------------------------------------------- Marktplaats
NL_MONTHS = {"jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "aug": 8,
             "sep": 9, "okt": 10, "nov": 11, "dec": 12}


def mp_date(s):
    s = (s or "").strip().lower()
    if s.startswith("vandaag"):
        return TODAY
    if s.startswith("gisteren"):
        return TODAY - dt.timedelta(days=1)
    if s.startswith("eergisteren"):
        return TODAY - dt.timedelta(days=2)
    m = re.match(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?\s+'?(\d{2,4})", s)
    if m:
        y = int(m.group(3))
        return dt.date(y + 2000 if y < 100 else y, NL_MONTHS.get(m.group(2), 1), int(m.group(1)))
    return None


def mp_get(params):
    url = "https://www.marktplaats.nl/lrp/api/search?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def mp_row(x):
    pi = x.get("priceInfo") or {}
    cents = pi.get("priceCents") or 0
    attrs = {a.get("key"): a.get("value") for a in (x.get("extendedAttributes") or []) + (x.get("attributes") or [])}
    loc = x.get("location") or {}
    d = mp_date(x.get("date"))
    vip = x.get("vipUrl") or f"/v/computers-en-software/monitoren/{x['itemId']}"
    return {
        "src": "mp", "id": x["itemId"], "title": clean(x.get("title", "")),
        "desc": clean(x.get("categorySpecificDescription") or x.get("description") or ""),
        "price": cents / 100 if cents else None, "ptype": pi.get("priceType"),
        "city": clean(loc.get("cityName", "")), "km": round((loc.get("distanceMeters") or 0) / 1000),
        "lat": loc.get("latitude"), "lon": loc.get("longitude"),
        "date": d.isoformat() if d else x.get("date"), "url": "https://www.marktplaats.nl" + vip,
        "attrs": attrs, "cat": x.get("categoryId"),
        "imgs": [p["largeUrl"] for p in (x.get("pictures") or []) if p.get("largeUrl")][:3],  # for the AI check
    }


def cmd_mp(args):
    since = None
    if "--since" in args:
        since = dt.date.fromisoformat(args[args.index("--since") + 1])
    elif STATE.get("last_run"):
        since = dt.date.fromisoformat(STATE["last_run"])
    else:
        since = TODAY - dt.timedelta(days=14)
    rows, calls = {}, 0
    base = {"distanceMeters": CFG["radius_km"] * 1000, "limit": 100, "sortBy": "SORT_INDEX",
            "sortOrder": "DECREASING", "viewOptions": "list-view",
            "attributeRanges[]": f"PriceCents:null:{CFG['max_price'] * 100}"}
    searches = [{"l1CategoryId": 322, "l2CategoryIds": 336}]
    searches += [{"query": q, "searchInTitleAndDescription": "true"} for q in CFG["mp_queries"]]
    for city, pc in CFG["centers"].items():
        for s in searches:
            for page in range(20):
                p = dict(base, **s, postcode=pc, offset=page * 100, distanceMeters=radius_m(city))
                try:
                    j = mp_get(p)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! {city} {s} offset {page*100}: {e}")
                    break
                calls += 1
                L = j.get("listings") or []
                older = 0
                for x in L:
                    r = mp_row(x)
                    d = r["date"]
                    if isinstance(d, str) and re.match(r"\d{4}-", d) and d < since.isoformat():
                        older += 1
                        continue
                    rows[r["id"]] = r
                time.sleep(CFG["mp_delay_s"])
                if len(L) < 100 or older >= len(L) - 3:  # past the date window (allow a few pinned ads)
                    break
    out = DATA / f"mp_{TODAY}.json"
    save(out, list(rows.values()))
    print(f"MP: {len(rows)} listings since {since} ({calls} API calls) -> {out.name}")


# ---------------------------------------------------------------- Facebook
def cmd_fbimport(args):
    src = load(args[0], [])
    rows = []
    for x in src:
        rows.append({"src": "fb", "id": "fb" + str(x["id"]), "title": x.get("title", ""), "desc": x.get("desc", ""),
                     "price": x.get("price"), "ptype": "FB", "city": x.get("city", ""), "km": None,
                     "date": x.get("date") or "", "url": f"https://www.facebook.com/marketplace/item/{x['id']}/",
                     "attrs": {}, "cat": None})
    out = DATA / f"fb_{TODAY}.json"
    save(out, rows)
    print(f"FB: {len(rows)} rows -> {out.name}")


# ---------------------------------------------------------------- extraction
SIZE_RE = re.compile(r"(?<![\d.,x×])(\d{1,2}(?:[.,]\d)?)\s*(?:\"|”|″|''|-?\s?inch\b|inches\b|inc\b|in\b|duim\b|zoll\b)", re.I)
CM_RE = re.compile(r"(?<![\d.,])(\d{2,3})\s*cm\b", re.I)
SUFFIX_BAD = re.compile(r"(hz|ms|gb|tb|mm|cm|fps|nits?|w)$", re.I)
BRAND_RULES = [
    (r"\bdell\b|ultrasharp|alienware", r"\b(?:U|P|S|SE|E|G|AW|C)(\d{2})\d{2}[A-Z]{0,4}\b"),
    (r"\blg\b|ultragear|ultrawide", r"\b(\d{2})(?:UM|WP|WL|UC|GN|GL|GP|UL|UN|UK|UD|MP|MK|BN|BL|BK|BQ|WN|WQ|WR|WK|SR|GS|GR|GX|EP|EN|GQ|UP|UQ|UR|US|MR|MN|MS|ML|BA|BR|BP)\w*"),
    (r"samsung|odyssey", r"\bL?[CSUFG](\d{2})[A-Z]{1,3}\d{2,3}\w*"),
    (r"\baoc\b|agon", r"\b(?:AG|CU|CQ|Q|U|C|G)?(\d{2})(?:\d{2}|\d[A-Z]|G\d|V\d|P\d|B\d|E\d)\w*"),
    (r"philips|evnia", r"\b(\d{2})(?:\d[A-Z]|[A-Z]\d)\w*"),
    (r"iiyama|prolite|g-master", r"\b(?:X|XB|XUB|GB|GCB|XCB|B|E|G|XU|T|XUB)(\d{2})\d{2}\w*"),
    (r"benq|zowie|mobiuz", r"\b(?:EW|GW|PD|BL|EX|XL|SW|GL|EL|PV|VZ|RL)(\d{2})\d{2}\w*"),
    (r"\basus\b|\brog\b|\btuf\b|proart", r"\b(?:VG|VP|PA|PB|PG|XG|VA|VZ|BE|MX|VE|VS|PQ|VN|VX)(\d{2})\w*"),
    (r"\bacer\b|nitro|predator", r"\b(?:XV|XB|KG|VG|EI|ED|ET|CB|XZ|SA|XR|XF|EB|EK|BE|Z|X|B|K|R|V)(\d{2})\d\w*"),
    (r"\bmsi\b|optix", r"\b(?:MAG|MPG|MEG|MP|PS|G)\s?(\d{2})\d\w*"),
    (r"gigabyte|aorus", r"\b(?:G|M|FO|FI|AD|KD|CO|MO|GS)(\d{2})[A-Z]\w*"),
    (r"lenovo|thinkvision|legion", r"\b(?:P|T|L|S|G|Q|E|Y|R)(\d{2})[a-z]?-\d{2}\w*"),
    (r"\bhp\b|omen|elitedisplay", r"\b(?:Z|E|P|M|X|U|S)?(\d{2})(?:[a-z]{1,2})\b|\b(?:E|P|Z)(\d{2})\d[a-z]?\b"),
]
EXCL = re.compile(r"monitor ?arm|monitorarm|monitorbeugel|monitorhouder|monitorstandaard|desk ?mount|monitor ?mount|elektronica ?set|neomounts|gasveer|monitorsteun|monitorvoet|bureauklem|beugel|^\W*(arm|beugel|houder|standaard|voet)\b|"
                  r"baby|bloeddruk|hartslag|studio ?monitor|monitor ?speaker|luidspreker|in-?ear|\biem\b|krk|yamaha hs|"
                  r"\bups\b|projector|beamer|scherm ?reparatie|reparatie|kapot|defect|onderdelen|flight ?case|koffer|\bhoes\b|"
                  r"accessoire|kabel ?set|verhoger|riser|standaard \(|\bipad\b|tablet|telefoon|iphone|\bdj\b|autoradio|dashcam|"
                  r"bewakingscamera|camera ?systeem|laptop ?standaard|bureau\b", re.I)
TV_RE = re.compile(r"\b(tv|televisie|smart-?tv)\b", re.I)
MON_RE = re.compile(r"monitor|beeldscherm|scherm|display|ultrawide|curved|gaming", re.I)
MODELS = load(ROOT / "models.json", [])


def norm_inch(v):
    v = float(str(v).replace(",", "."))
    return v if 5 <= v <= 57 else None


def extract_inch(r):
    for k, v in (r.get("attrs") or {}).items():
        if k and re.search(r"size|diagonal|inch|diagonaal", k, re.I) and v:
            v = str(v)
            if "cm" in v.lower():  # range like "40 tot 60 cm"
                nums = [int(n) for n in re.findall(r"\d{2,3}", v)]
                if nums and max(nums) / 2.54 < CFG["min_inch"]:
                    return round(max(nums) / 2.54, 1), "attr-cm"
                if nums and min(nums) / 2.54 >= CFG["min_inch"]:
                    return round(min(nums) / 2.54, 1), "attr-cm"
                continue
            m = re.search(r"\d{2}(?:[.,]\d)?", v)
            if m and norm_inch(m.group()):
                return norm_inch(m.group()), "attr"
    for field in ("title", "desc"):
        t = r.get(field) or ""
        for m in SIZE_RE.finditer(t):
            v = norm_inch(m.group(1))
            if v:
                return v, field
        for m in CM_RE.finditer(t):
            cm = int(m.group(1))
            if 50 <= cm <= 125:
                return round(cm / 2.54, 1), field + "-cm"
    mod = match_model(r)
    if mod and mod.get("inch"):
        return mod["inch"], "model"
    text = r["title"] + " " + r.get("desc", "")
    for brand, pat in BRAND_RULES:
        if re.search(brand, text, re.I):
            for m in re.finditer(pat, text, re.I):
                if SUFFIX_BAD.search(m.group(0)):
                    continue
                g = next((x for x in m.groups() if x), None)
                v = norm_inch(g) if g else None
                if v and 17 <= v <= 49:
                    return v, "model-guess"
    return None, None


OVERRIDES = load(DATA / "overrides.json", {})  # listing id -> model dict (from research)


def match_model(r):
    if r["id"] in OVERRIDES:
        return OVERRIDES[r["id"]]
    text = (r["title"] + " " + r.get("desc", "")).lower()
    for m in MODELS:
        if re.search(m["re"], text, re.I):
            return m
    return None


USBC_TXT = {"no": "❌", "video": "➖ solo imagen", "yes": "✅", None: "?"}


def usbc_label(v):
    if v in USBC_TXT:
        return USBC_TXT[v]
    return "✅ " + v


def price_label(r):
    p, t = r.get("price"), r.get("ptype")
    if t == "FREE":
        return "gratis"
    if not p:
        return "puja" if r["src"] == "mp" else "gratis?"
    return f"€{p:.0f}"


def cmd_rank(args):
    show_all = "--all" in args
    seen = set(STATE.get("seen", []))
    rows = []
    for f in (DATA / f"mp_{TODAY}.json", DATA / f"fb_{TODAY}.json"):
        rows += load(f, [])
    town_re = re.compile(r"^(" + CFG["local_towns"] + r")\b", re.I)
    ranked, unknown_size, research, dropped = [], [], [], 0
    dedup = set()
    for r in rows:
        if r["id"] in dedup:
            continue
        dedup.add(r["id"])
        title = r["title"]
        if EXCL.search(title) or (TV_RE.search(title) and not re.search("monitor", title, re.I)):
            dropped += 1
            continue
        if r["src"] == "mp" and r.get("cat") != 336 and not MON_RE.search(title):
            dropped += 1
            continue
        if r["src"] == "fb" and not town_re.search(r["city"] or ""):
            dropped += 1
            continue
        if r.get("price") and r["price"] > CFG["max_price"]:
            dropped += 1
            continue
        if not show_all and r["id"] in seen:
            continue
        inch, how = extract_inch(r)
        r["inch"], r["inch_src"] = inch, how
        mod = match_model(r)
        r["model"] = mod["name"] if mod else None
        r["new"] = mod.get("new") if mod else None
        r["disc_model"] = bool(mod and mod.get("disc"))
        r["usbc"] = usbc_label(mod.get("usbc")) if mod else "?"
        if r["new"] and r.get("price"):
            r["discount"] = round(100 * (1 - r["price"] / r["new"]))
        else:
            r["discount"] = None
        if inch is None:
            if r.get("cat") == 336 or (r["src"] == "fb" and MON_RE.search(title)):
                unknown_size.append(r)
            continue
        if inch < CFG["min_inch"] or inch > CFG["max_inch"]:
            continue
        ranked.append(r)
        if not mod:
            research.append(r)
    ranked.sort(key=lambda r: (r["discount"] is None, -(r["discount"] or 0), r.get("price") or 0))

    def where(r):
        src = "MP" if r["src"] == "mp" else "FB"
        return f"{src} · {r['city']} · {r['date']}"

    lines = [f"# Monitores ≥{CFG['min_inch']}\" — {TODAY}  ({len(ranked)} nuevos, {len(unknown_size)} sin tamaño, {dropped} descartados)", "",
             "| Monitor | Pide | Nuevo | Desc. | USB‑C | Pulg. | Dónde · fecha |", "|---|---|---|---|---|---|---|"]
    for r in ranked:
        name = r["model"] or r["title"][:60]
        new = f"~€{r['new']}{'*' if r['disc_model'] else ''}" if r["new"] else "?"
        disc = f"-{r['discount']}%" if r["discount"] is not None else "—"
        inch = f"{r['inch']:g}" + ("?" if r["inch_src"] == "model-guess" else "")
        lines.append(f"| [{name}]({r['url']}) | {price_label(r)} | {new} | {disc} | {r['usbc']} | {inch} | {where(r)} |")
    if unknown_size:
        lines += ["", "## Sin tamaño en el anuncio", ""]
        for r in unknown_size:
            lines.append(f"- [{r['title'][:70]}]({r['url']}) — {price_label(r)} · {where(r)}")
    (ROOT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    save(DATA / f"unknown_{TODAY}.json",
         [{"id": r["id"], "title": r["title"], "desc": r["desc"][:400], "inch": r.get("inch")} for r in research + unknown_size])
    save(DATA / f"ranked_{TODAY}.json", ranked)
    print(f"ranked {len(ranked)} | unknown size {len(unknown_size)} | need research {len(research) + len(unknown_size)} | dropped {dropped}")
    print("-> report.md, data/unknown_%s.json" % TODAY)


def cmd_learn(args):
    """Merge research rows [{id,re,name,inch,res,new,disc,usbc,notes}] into models.json (by re) and overrides (by id)."""
    new = load(args[0], [])
    have = {m["re"] for m in MODELS}
    keys = ("re", "name", "inch", "res", "new", "disc", "usbc", "notes")
    add = []
    for m in new:
        if not m.get("name"):
            continue
        clean = {k: m[k] for k in keys if m.get(k) is not None}
        if m.get("re") and m["re"] not in have:
            add.append(clean)
            have.add(m["re"])
        if m.get("id"):
            OVERRIDES[m["id"]] = clean
    save(ROOT / "models.json", MODELS + add)
    save(DATA / "overrides.json", OVERRIDES)
    print(f"learned {len(add)} models (total {len(MODELS) + len(add)}), {len(OVERRIDES)} listing overrides")


def cmd_seen(args):
    ranked = load(DATA / f"ranked_{TODAY}.json", [])
    unk = load(DATA / f"unknown_{TODAY}.json", [])
    s = set(STATE.get("seen", [])) | {r["id"] for r in ranked} | {r["id"] for r in unk}
    STATE["seen"] = sorted(s)
    STATE["last_run"] = TODAY.isoformat()
    save(STATE_P, STATE)
    print(f"state: {len(s)} seen, last_run {TODAY}")


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    {"mp": cmd_mp, "fbimport": cmd_fbimport, "rank": cmd_rank, "learn": cmd_learn, "seen": cmd_seen}.get(
        cmd, lambda a: print(__doc__))(rest)
