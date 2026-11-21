"""Local monitor database (SQLite, data/monitors.db). Answer questions from here first; sweep only to refresh.

Usage (python -I db.py <cmd>):
  sync [--full]                 Marktplaats sweep -> upsert. Default (quick, ~25 calls): stops at listings older than
                                last_run. --full (~100 calls, weekly at most): every active listing, all dates, and
                                listings that disappeared are marked inactive (sold/removed). Also writes data/mp_<today>.json (rows posted since
                                state.json last_run) so `hunt.py rank` keeps working without a second sweep.
  details [--max 40] [--ids a,b] [--refresh-days N]
                                fetch listing pages (bids, views, favourites, full description) for active MP
                                listings >= min_inch that have no details yet (or older than N days). 2.5 s apart.
  fb <file.json>                import Facebook rows (fb_collect.js output or data/fb_<date>.json)
  details-json <file.json>      import already-fetched page details (uw_details format)
  enrich                        recompute model/size/panel/damage/extras for every row (after editing models.json)
  note <id> <text>              set a free-text comment on a listing
  q [filters]                   query. Filters: --uw  --min-inch 28  --max-inch N  --max-price N  --usbc
                                --panel IPS|VA|OLED  --min-hz N  --res UWQHD  --text REGEX  --since YYYY-MM-DD (posted)
                                --new (first seen today)  --no-damage  --inactive  --all-sizes  --src mp|fb
                                --sort deal|price|date|bid|inch  --limit 40  --json
  stats                         counts
"""
import datetime as dt
import html
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hunt  # noqa: E402

DB_P = hunt.DATA / "monitors.db"
TODAY = hunt.TODAY.isoformat()
COLS = ["id", "src", "url", "title", "desc", "price", "ptype", "city", "km", "posted", "cat", "attrs",
        "inch", "inch_src", "model", "res", "panel", "hz", "ultrawide", "usbc", "new_price", "disc_model", "discount",
        "top_bid", "n_bids", "views", "favs", "damage", "extras", "notes",
        "first_seen", "last_seen", "active", "details_at"]


def con():
    c = sqlite3.connect(DB_P)
    c.row_factory = sqlite3.Row
    c.execute(f"CREATE TABLE IF NOT EXISTS listings ({', '.join(c_ + (' PRIMARY KEY' if c_ == 'id' else '') for c_ in COLS)})")
    return c


# ---------------------------------------------------------------- enrichment
RES_TXT = [(r"3440\s?[x×*]\s?1440|\buwqhd\b", "UWQHD"), (r"5120\s?[x×*]\s?2160|5k2k", "5K2K"),
           (r"5120\s?[x×*]\s?1440", "DQHD 32:9"), (r"3840\s?[x×*]\s?1080", "DFHD 32:9"),
           (r"2560\s?[x×*]\s?1080|\buwfhd\b|\bwfhd\b", "UWFHD"), (r"3840\s?[x×*]\s?2160|\b4k\b|\buhd\b", "4K"),
           (r"2560\s?[x×*]\s?1440|\bwqhd\b|\bqhd\b|1440p|\b2k\b", "QHD"), (r"1920\s?[x×*]\s?1080|full ?hd|\bfhd\b|1080p", "FHD")]
UW_TXT = re.compile(r"ultra\s?-?wide|21[:/]9|32[:/]9|3440|uwqhd|uwfhd|2560\s?[x×*]\s?1080|5120\s?[x×*]\s?(1440|2160)", re.I)
PANEL_TXT = re.compile(r"\b(qd-?oled|oled|nano ?ips|ips|va|tn)\b", re.I)
HZ_TXT = re.compile(r"(\d{2,3})\s?hz\b", re.I)
NEG = re.compile(r"\b(geen|zonder|no|not|nooit|niet|vrij van|free of|zero|0)\b[^.!?\n]{0,25}$", re.I)
DAMAGE = [  # (regex, label, check negation before it)
    (r"gebroken|\bbarst\w*|scheur\w*|cracked|broken", "roto/grieta", True),
    (r"kapot|defect|werkt niet|doet het niet|not working", "defecto", True),
    (r"beschadig\w*|\bschade\b|damage[d]?", "daños", True),
    (r"\bkras(sen|je|jes)?\b|scratch\w*", "rayas", True),
    (r"dode? pixels?|pixelfout\w*|dead pixels?|stuck pixels?", "píxeles muertos", True),
    (r"\bvlek(ken|je)?\b|\bstain", "manchas", True),
    (r"\bstre(e|)p(en)?\b|lijn(en)? (in|op|door) (het )?(beeld|scherm)|lines? on (the )?screen", "líneas", True),
    (r"lichtlek\w*|backlight ?bleed\w*|light ?bleed\w*|\bbleeding", "fugas de luz", True),
    (r"flikker\w*|flicker\w*", "parpadeo", True),
    (r"\bdeuk(je|jes)?\b|\bdent\b|\bdeukje", "abolladura", True),
    (r"inbrand\w*|burn-?in", "quemado", True),
    (r"donker\w* (baan|band|balk|plek|rand|strook|gedeelte|schaduw)|valt .{0,30}donker|donkerder|backlight|"
     r"dark (area|band|spot|patch|corner)", "zona oscura", True),
    (r"slaat .{0,25}(groen|roze|paars|rood|blauw)|(groen|roze|paars)e? (waas|zweem|gloed|tint)|kleurafwijking|"
     r"paneelslijtage|color (tint|shift)", "tono de color", True),
    (r"(zonder|geen|exclusief|excl\.?) (voet|standaard|stand)\b|voet (ontbreekt|mist)|without stand|no stand", "sin pie", False),
    (r"(zonder|geen|exclusief|excl\.?) (stroom|voedings)?(kabel|adapter|lader|voeding)|adapter ontbreekt|no power (cable|adapter)", "sin cable/fuente", False),
]
EXTRAS = [
    (r"originele (doos|verpakking)|\bin (de )?doos\b|met doos|\bdoos\b|original box", "caja"),
    (r"\bbon\b|factuur|aankoopbewijs|receipt|invoice", "factura"),
    (r"garantie|warranty", "garantía"),
    (r"monitor ?arm|ergo ?arm|ergo ?stand|\barm\b", "brazo"),
    (r"(hdmi|displayport|dp|usb-?c|stroom|thunderbolt)[- ]?kabel|kabels? (inbegrepen|erbij|meegeleverd|incl)|incl(\.|usief)? (alle )?kabel|met (alle )?(benodigde |bijbehorende )?kabels|cables? included", "cables", True),
    (r"in hoogte verstelbaar|hoogte ?verstelbaar|height[- ]adjust\w*|\bpivot\b|kantelbaar en", "pie regulable"),
    (r"\bvesa\b", "VESA"), (r"\bkvm\b", "KVM"), (r"rj-?45|ethernet|netwerkpoort", "Ethernet"),
    (r"speakers?\b|luidspreker", "altavoces"), (r"afstandsbediening|remote control", "mando"),
    (r"zo goed als nieuw|als nieuw|nieuwstaat|perfecte staat|nauwelijks gebruikt|like new|mint", "como nuevo"),
    (r"ongeopend|sealed|nooit gebruikt|nieuw in (de )?doos|brand new", "sin usar"),
]


def flags(text, table, neg_check=True):
    out = []
    for item in table:
        rx, label = item[0], item[1]
        check = item[2] if len(item) > 2 else False
        for m in re.finditer(rx, text, re.I):
            if check and neg_check and NEG.search(text[max(0, m.start() - 40):m.start()]):
                continue
            if label not in out:
                out.append(label)
            break
    return out


def enrich(r):
    r = dict(r)
    attrs = r.get("attrs")
    hr = {"id": r["id"], "title": r["title"] or "", "desc": r.get("desc") or "",
          "attrs": json.loads(attrs) if isinstance(attrs, str) and attrs else (attrs or {})}
    inch, how = hunt.extract_inch(hr)
    mod = hunt.match_model(hr) or {}
    text = hr["title"] + " \n " + hr["desc"]
    if mod and not (mod.get("panel") and mod.get("hz")):  # research overrides lack panel/hz: borrow from models.json
        base = next((m for m in hunt.MODELS if re.search(m["re"], text, re.I)), {})
        mod = {**{k: base.get(k) for k in ("panel", "hz")}, **{k: v for k, v in mod.items() if v is not None}}
    if mod.get("inch") and how != "title":  # a recognised model beats cm ranges / description guesses
        inch, how = mod["inch"], "model"
    res = mod.get("res")
    if not res:
        res = next((lab for rx, lab in RES_TXT if re.search(rx, text, re.I)), None)
    panel = mod.get("panel")
    if not panel:
        m = PANEL_TXT.search(text)
        panel = m.group(1).upper().replace(" ", "") if m else None
    hz = mod.get("hz")
    if not hz:
        hzs = [int(x) for x in HZ_TXT.findall(text) if 48 <= int(x) <= 360]
        hz = max(hzs) if hzs else None
    uw = bool(res and re.search(r"UW|5K2K|32:9", res)) or bool(UW_TXT.search(text)) or \
        bool(inch and 33.5 <= inch <= 49.5 and res not in ("4K", "QHD", "FHD"))
    r.update(inch=inch, inch_src=how, model=mod.get("name"), res=res, panel=panel, hz=hz, ultrawide=int(uw),
             usbc=mod.get("usbc") if mod else None, new_price=mod.get("new"), disc_model=int(bool(mod.get("disc"))))
    r["discount"] = round(100 * (1 - r["price"] / r["new_price"])) if r.get("price") and r.get("new_price") else None
    r["damage"] = ", ".join(flags(text, DAMAGE)) or None
    r["extras"] = ", ".join(flags(text, EXTRAS)) or None
    if not r.get("notes") and mod.get("notes"):
        r["notes"] = mod["notes"]
    return r


def keep(r):
    t = r["title"] or ""
    if hunt.EXCL.search(t) or (hunt.TV_RE.search(t) and not re.search("monitor", t, re.I)):
        return False
    if r["src"] == "mp" and r.get("cat") != 336 and not hunt.MON_RE.search(t):
        return False
    return True


def clean(v):
    """Drop lone surrogates (broken emoji) that SQLite cannot store."""
    return v.encode("utf-16", "surrogatepass").decode("utf-16", "replace") if isinstance(v, str) else v


def write(c, r):
    c.execute(f"INSERT OR REPLACE INTO listings ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
              [clean(r.get(k)) for k in COLS])


def upsert(c, rows, fields_from_new=None):
    n_new = 0
    for r in rows:
        old = c.execute("SELECT * FROM listings WHERE id=?", (r["id"],)).fetchone()
        if old:
            merged = dict(old)
            for k, v in r.items():
                if k in COLS and (fields_from_new is None or k in fields_from_new):
                    if k == "desc" and old["details_at"] and v and len(v) < len(old["desc"] or ""):
                        continue  # keep the full description fetched from the page
                    merged[k] = v
        else:
            merged = {k: None for k in COLS}
            merged.update({k: v for k, v in r.items() if k in COLS})
            merged["first_seen"] = TODAY
            n_new += 1
        merged["last_seen"] = TODAY
        merged["active"] = 1
        if isinstance(merged.get("attrs"), dict):
            merged["attrs"] = json.dumps(merged["attrs"], ensure_ascii=False)
        merged = enrich(merged)
        write(c, merged)
    c.commit()
    return n_new


# ---------------------------------------------------------------- commands
def cmd_sync(args):
    full = "--full" in args
    last = hunt.STATE.get("last_run")
    since = (dt.date.fromisoformat(last) - dt.timedelta(days=1)).isoformat() if last and not full else "0000"
    base = {"distanceMeters": hunt.CFG["radius_km"] * 1000, "limit": 100, "sortBy": "SORT_INDEX",
            "sortOrder": "DECREASING", "viewOptions": "list-view",
            "attributeRanges[]": f"PriceCents:null:{hunt.CFG['max_price'] * 100}"}
    searches = [{"l1CategoryId": 322, "l2CategoryIds": 336}]
    searches += [{"query": q, "searchInTitleAndDescription": "true"}
                 for q in hunt.CFG["mp_queries"] + hunt.CFG.get("mp_extra_queries", [])]
    rows, calls, errors = {}, 0, 0
    cache = hunt.DATA / f"mp_active_{TODAY}.json"
    if "--cached" in args and cache.exists():
        rows = {r["id"]: r for r in hunt.load(cache, [])}
        searches = []
    for city, pc in hunt.CFG["centers"].items():
        for s in searches:
            for page in range(15):
                try:
                    j = hunt.mp_get(dict(base, **s, postcode=pc, offset=page * 100, distanceMeters=hunt.radius_m(city)))
                except Exception as e:  # noqa: BLE001
                    print(f"  ! {city} {s} offset {page * 100}: {e}")
                    errors += 1
                    break
                calls += 1
                L = j.get("listings") or []
                for x in L:
                    r = hunt.mp_row(x)
                    rows[r["id"]] = r
                time.sleep(hunt.CFG["mp_delay_s"])
                if len(L) < 100:
                    break
                if not full:  # quick mode: stop once a page is (almost) all older than the last run
                    older = sum(1 for x in L if re.match(r"\d{4}-", hunt.mp_row(x)["date"] or "")
                                and hunt.mp_row(x)["date"] < since)
                    if older >= len(L) - 3:
                        break
    hunt.save(hunt.DATA / f"mp_active_{TODAY}.json", list(rows.values()))
    kept = []
    for r in rows.values():
        if not keep(r):
            continue
        r = dict(r)
        r["posted"] = r.pop("date")
        kept.append(r)
    c = con()
    n_new = upsert(c, kept)
    gone = 0
    if full and errors == 0:  # only a full sweep can tell which listings disappeared
        ids = {r["id"] for r in kept}
        for (i,) in c.execute("SELECT id FROM listings WHERE src='mp' AND active=1").fetchall():
            if i not in ids:
                c.execute("UPDATE listings SET active=0 WHERE id=?", (i,))
                gone += 1
        c.commit()
    since = hunt.STATE.get("last_run") or (hunt.TODAY - dt.timedelta(days=14)).isoformat()
    recent = [dict(r, date=r["posted"]) for r in kept if re.match(r"\d{4}-", r["posted"] or "") and r["posted"] >= since]
    hunt.save(hunt.DATA / f"mp_{TODAY}.json", recent)
    print(f"sync: {len(rows)} results, {len(kept)} monitor listings ({n_new} new, {gone} gone/sold), "
          f"{calls} calls, {errors} errors; {len(recent)} posted since {since} -> data/mp_{TODAY}.json")


def fetch_details(url):
    req = urllib.request.Request(url, headers={"User-Agent": hunt.UA, "Accept-Language": "nl-NL,nl;q=0.9"})
    h = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    i = h.index("window.__CONFIG__ = ") + 20
    e = h.index("</script>", i)
    L = json.loads(h[i:e].strip().rstrip(";"))["listing"]
    bids = (L.get("bidsInfo") or {}).get("bids") or []
    st = L.get("stats") or {}
    raw = L.get("description") or ""
    if not raw:  # since Nov 2026 the description is only in the rendered HTML, not in __CONFIG__
        m = re.search(r'<div[^>]*class="[^"]*Description[^"]*"[^>]*>(.*?)</div>', h, re.S)
        raw = m.group(1) if m else ""
    desc = re.sub(r"<[^>]+>", " ", html.unescape(raw))
    desc = re.sub(r"^\s*Beschrijving\s*", "", re.sub(r"\s+", " ", desc)).strip()
    return {"desc": desc, "n_bids": len(bids), "top_bid": max((b.get("value", 0) for b in bids), default=0) / 100 or None,
            "views": st.get("viewCount"), "favs": st.get("favoritedCount"), "title": L.get("title"),
            "price": ((L.get("priceInfo") or {}).get("priceCents") or 0) / 100 or None,
            "imgs": ["https:" + u.replace("$_#", "$_83") if u.startswith("//") else u.replace("$_#", "$_83")
                     for u in ((L.get("gallery") or {}).get("imageUrls") or [])[:3]]}


def cmd_details(args):
    mx = int(args[args.index("--max") + 1]) if "--max" in args else 40
    c = con()
    if "--ids" in args:
        ids = args[args.index("--ids") + 1].split(",")
        todo = [c.execute("SELECT * FROM listings WHERE id=?", (i,)).fetchone() for i in ids]
        todo = [t for t in todo if t]
    else:
        days = int(args[args.index("--refresh-days") + 1]) if "--refresh-days" in args else None
        cutoff = (hunt.TODAY - dt.timedelta(days=days)).isoformat() if days is not None else None
        q = ("SELECT * FROM listings WHERE src='mp' AND active=1 AND (inch>=? OR (inch IS NULL AND cat=336)) "
             "AND (details_at IS NULL" + (" OR details_at<?" if cutoff else "") + ") ORDER BY posted DESC LIMIT ?")
        todo = c.execute(q, [hunt.CFG["min_inch"]] + ([cutoff] if cutoff else []) + [mx]).fetchall()
    ok = 0
    for t in todo:
        try:
            d = fetch_details(t["url"])
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            print(f"  ! {t['id']} {t['title'][:40]}: {msg}")
            if "404" in msg or "410" in msg:
                c.execute("UPDATE listings SET active=0 WHERE id=?", (t["id"],))
            continue
        r = dict(t)
        r.update({k: v for k, v in d.items() if k not in ("title", "price", "imgs")}, details_at=TODAY)
        r = enrich(r)
        write(c, r)
        ok += 1
        time.sleep(2.5)
    c.commit()
    left = c.execute("SELECT COUNT(*) FROM listings WHERE src='mp' AND active=1 AND (inch>=? OR (inch IS NULL AND cat=336)) "
                     "AND details_at IS NULL", (hunt.CFG["min_inch"],)).fetchone()[0]
    print(f"details: {ok}/{len(todo)} fetched; {left} large listings still without details")


def cmd_details_json(args):
    c = con()
    n = 0
    for d in hunt.load(args[0], []):
        t = c.execute("SELECT * FROM listings WHERE id=?", (d["id"],)).fetchone()
        if not t or d.get("error"):
            continue
        r = dict(t)
        r.update({"desc": d.get("desc") or r["desc"], "n_bids": d.get("bids"), "top_bid": d.get("top_bid") or None,
                  "views": d.get("views"), "favs": d.get("favs"), "details_at": TODAY})
        r = enrich(r)
        write(c, r)
        n += 1
    c.commit()
    print(f"details imported: {n}")


def cmd_fb(args):
    town_re = re.compile(r"^(" + hunt.CFG["local_towns"] + r")\b", re.I)
    rows = []
    for x in hunt.load(args[0], []):
        fid = str(x["id"])
        fid = fid if fid.startswith("fb") else "fb" + fid
        r = {"src": "fb", "id": fid, "title": x.get("title", ""), "desc": x.get("desc", ""), "price": x.get("price"),
             "ptype": "FB", "city": x.get("city", ""), "km": None, "posted": x.get("date") or x.get("posted") or TODAY,
             "url": f"https://www.facebook.com/marketplace/item/{fid[2:]}/", "attrs": {}, "cat": None}
        if keep(r) and town_re.search(r["city"] or "") and hunt.MON_RE.search(r["title"] + " monitor"):
            rows.append(r)
    c = con()
    n = upsert(c, rows, fields_from_new={"title", "price", "city", "url", "src", "ptype"} | {"id"})
    print(f"fb: {len(rows)} rows ({n} new)")


def cmd_enrich(args):
    c = con()
    rows = [dict(r) for r in c.execute("SELECT * FROM listings").fetchall()]
    for r in rows:
        r = enrich(r)
        write(c, r)
    c.commit()
    print(f"enriched {len(rows)}")


def cmd_note(args):
    c = con()
    c.execute("UPDATE listings SET notes=? WHERE id=?", (" ".join(args[1:]), args[0]))
    c.commit()
    print("ok")


USBC = {"no": "❌", "video": "➖", "yes": "✅", None: "?"}


def cmd_q(args):
    def opt(name, cast=str, default=None):
        return cast(args[args.index(name) + 1]) if name in args else default

    w, p = [], []
    if "--any-cat" not in args:  # outside the monitor category only keep listings with a recognised model
        w.append("(cat=336 OR src='fb' OR model IS NOT NULL)")
    if "--inactive" not in args:
        w.append("active=1")
    if "--all-sizes" not in args:
        w.append("inch>=?")
        p.append(opt("--min-inch", float, hunt.CFG["min_inch"]))
    if "--all-sizes" not in args:
        w.append("inch<=?"); p.append(opt("--max-inch", float, hunt.CFG["max_inch"]))
    if "--max-price" in args:
        w.append("(price IS NULL OR price<=?)"); p.append(opt("--max-price", float))
    if "--uw" in args:
        w.append("ultrawide=1")
    if "--usbc" in args:
        w.append("usbc IS NOT NULL AND usbc NOT IN ('no','video')")
    if "--panel" in args:
        w.append("panel LIKE ?"); p.append("%" + opt("--panel") + "%")
    if "--min-hz" in args:
        w.append("hz>=?"); p.append(opt("--min-hz", int))
    if "--res" in args:
        w.append("res LIKE ?"); p.append("%" + opt("--res") + "%")
    if "--since" in args:
        w.append("posted>=?"); p.append(opt("--since"))
    if "--new" in args:
        w.append("first_seen=?"); p.append(TODAY)
    if "--no-damage" in args:
        w.append("damage IS NULL")
    if "--src" in args:
        w.append("src=?"); p.append(opt("--src"))
    rows = [dict(r) for r in con().execute("SELECT * FROM listings" + (" WHERE " + " AND ".join(w) if w else ""), p)]
    if "--text" in args:
        rx = re.compile(opt("--text"), re.I)
        rows = [r for r in rows if rx.search((r["title"] or "") + " " + (r["desc"] or "") + " " + (r["model"] or ""))]
    sort = opt("--sort", str, "deal")

    def eff(r):  # what you would realistically pay: asking price, or above the top bid
        return max(r["price"] or 0, (r["top_bid"] or 0))

    keys = {"deal": lambda r: (r["discount"] is None, -(r["discount"] or 0), eff(r)),
            "price": lambda r: (eff(r) == 0, eff(r)), "date": lambda r: r["posted"] or "",
            "bid": lambda r: -(r["top_bid"] or 0), "inch": lambda r: -(r["inch"] or 0)}
    rows.sort(key=keys[sort], reverse=(sort == "date"))
    rows = rows[:opt("--limit", int, 40)]
    if "--json" in args:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    print("| Monitor | Pide | Puja máx | Nuevo | Desc. | USB‑C | Pulg. | Panel | Daños / extras | Dónde · fecha | id |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        name = r["model"] or r["title"][:55]
        pide = "gratis" if r["ptype"] == "FREE" else (f"€{r['price']:.0f}" if r["price"] else "puja")
        bid = f"€{r['top_bid']:.0f} ({r['n_bids']})" if r["top_bid"] else ("—" if r["details_at"] else "?")
        new = f"~€{r['new_price']}{'*' if r['disc_model'] else ''}" if r["new_price"] else "?"
        disc = f"-{r['discount']}%" if r["discount"] is not None else "—"
        u = r["usbc"]
        usb = USBC.get(u, "✅ " + str(u))
        panel = " ".join(str(x) for x in (r["res"], r["panel"], f"{r['hz']}Hz" if r["hz"] else None) if x)
        de = " · ".join(x for x in (("⚠ " + r["damage"]) if r["damage"] else None, r["extras"], r["notes"]) if x)
        inch = f"{r['inch']:g}" if r["inch"] else "?"
        print(f"| [{name}]({r['url']}) | {pide} | {bid} | {new} | {disc} | {usb} | {inch} | {panel} | {de} | "
              f"{r['src'].upper()} · {r['city']} · {r['posted']} | {r['id']} |")
    print(f"\n{len(rows)} rows")


def cmd_stats(args):
    c = con()
    for label, q in [("total", "SELECT COUNT(*) FROM listings"), ("active", "SELECT COUNT(*) FROM listings WHERE active=1"),
                     (">=28in active", "SELECT COUNT(*) FROM listings WHERE active=1 AND inch>=28"),
                     ("ultrawide active", "SELECT COUNT(*) FROM listings WHERE active=1 AND ultrawide=1 AND inch>=28"),
                     ("with details", "SELECT COUNT(*) FROM listings WHERE details_at IS NOT NULL"),
                     ("facebook", "SELECT COUNT(*) FROM listings WHERE src='fb'")]:
        print(f"{label}: {c.execute(q).fetchone()[0]}")


if __name__ == "__main__":
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    {"sync": cmd_sync, "details": cmd_details, "details-json": cmd_details_json, "fb": cmd_fb, "enrich": cmd_enrich,
     "note": cmd_note, "q": cmd_q, "stats": cmd_stats}.get(cmd, lambda a: print(__doc__))(rest)
