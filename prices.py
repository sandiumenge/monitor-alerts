"""Value percentile (P0–P99): how good a deal a listing is compared with every TV (≥50") or monitor (≥31") seen.

Fair value = what an equivalent product costs new today × age × condition:
    TV new today   330 € × (inch/50)^2.3 (4K LED, 50" ≈ €330, 55" ≈ €410, 65" ≈ €600)
                   × panel (OLED 2.5, QLED 1.4, NanoCell 1.3, plasma 0.3; QLED of a budget brand 1.0) × 8K 1.8
                   × brand (budget 0.55: Salora, Blaupunkt, Thomson…; Hisense/TCL/Toshiba… 0.8)
                   × 0.9 if not 4K/8K × 0.9 if not Smart
    monitor new    models.json price when the model is known, else by resolution (MON_NEW)
    age            0.90^years for TVs, 0.93^years for monitors (floor 0.25); unknown year: year_of() fallback
    condition      1 / 0.8 lightly damaged / 0.1 heavily damaged
Cost = effective price (asking price, or the highest bid if higher) + a public-transport round trip:
    2 × (€1.16 + €0.217 × 1.3 × km) fare + 2 × (10 min + 3 min/km) at €10/h   (2 km ≈ €9, 7 km ≈ €17, 15 km ≈ €29)
Score = 1 − cost / fair + W · log10(fair). With W = 0.05 each ×10 in fair value is worth 5 points, so a €100 TV
for €10 scores a bit below a €1000 one for €100. P98 = a better score than 98 % of all listings seen.

The listing scored uses its distance to the nearest personal point (ALERT_POINTS); the reference uses Marktplaats'
distance to the public search center, so no personal distance is ever stored. Damaged, accessory and price-less
listings are left out of the reference.

Reference: prices_ref.json (built locally from the database and the last TV sweep) + every listing the cloud run
sees (state "value_ref").

  python -I prices.py build      rebuild prices_ref.json from data/monitors.db and the last data/tv_*.json
  python -I prices.py show       reference sizes and score quantiles
"""
import datetime as dt
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF_P = ROOT / "prices_ref.json"
W = 0.05
MIN_N = 30


BUDGET = re.compile(r"\b(salora|blaupunkt|thomson|telefunken|kiano|medion|akai|strong|hyundai|jvc|sharp|nokia|"
                    r"polaroid|denver|finlux|techwood|continental|bush|orion|lenco|dyon|manta|hkc|qilive|silvercrest)\b", re.I)
MID = re.compile(r"\b(hisense|tcl|toshiba|grundig|haier|xiaomi)\b", re.I)
NANO = re.compile(r"nano ?cell|\b\d{2}nano\d", re.I)
NOW = dt.date.today().year
AGE = {"tv": 0.90, "monitor": 0.93}
AGE_FLOOR = 0.25
COND = {None: 1.0, "light": 0.8, "heavy": 0.1}


def tv_new(r):
    """What an equivalent TV costs new today (not what this one cost when it was new)."""
    inch = r.get("inch") or 0
    if inch < 50:
        return None
    a = r.get("ai") or {}
    name = f"{r.get('brand') or ''} {r.get('title') or ''} {a.get('model') or ''}"
    panel, res = r.get("panel"), r.get("res")
    if NANO.search(name) and panel in (None, "QLED", "LED", "LCD"):
        panel = "NANO"
    budget = bool(BUDGET.search(name))
    k = {"OLED": 2.5, "QLED": 1.0 if budget else 1.4, "NANO": 1.3, "PLASMA": 0.3}.get(panel, 1.0)
    if res == "8K":
        k *= 1.8
    k *= 0.55 if budget else 0.8 if MID.search(name) else 1.0
    if panel != "PLASMA" and res not in ("4K", "8K"):
        k *= 0.9
    if not r.get("smart"):
        k *= 0.9
    return round(330 * (inch / 50) ** 2.3 * k)


def year_of(r, kind):
    """Model year; when unknown, a typical year for that kind of product on Marktplaats today."""
    y = r.get("year") or (r.get("ai") or {}).get("year")
    if isinstance(y, (int, float)) and 1995 < y <= NOW:
        return int(y)
    if kind != "tv":
        return 2019
    p, res = r.get("panel"), r.get("res")
    return 2011 if p == "PLASMA" else 2020 if p == "OLED" else 2018 if res in ("4K", "8K") else 2013 if res else 2015


def fair(r, kind, sev=None):
    """Fair second-hand value: new-today equivalent × age × condition (sev: None / 'light' / 'heavy')."""
    n = new_of(r, kind)
    if not n:
        return None
    age = max(0, NOW - year_of(r, kind))
    return n * max(AGE_FLOOR, AGE[kind] ** age) * COND.get(sev, 1.0)


def trip(km):
    """Public-transport round trip to pick it up (fare + time at €10/h), from the straight-line distance in km."""
    km = km or 0
    return 2 * (1.16 + 0.217 * 1.3 * km) + 2 * (10 + 3 * km) / 60 * 10


def load_points():
    """Personal reference points [{"lat", "lon", "work"?}], kept out of the public repo: env ALERT_POINTS
    (GitHub secret) or data/points.json (gitignored)."""
    f = ROOT / "data" / "points.json"
    raw = os.environ.get("ALERT_POINTS", "").strip() or (f.read_text(encoding="utf-8") if f.exists() else "")
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []


POINTS = load_points()


def km_to(r, p):
    la1, lo1, la2, lo2 = map(math.radians, (r["lat"], r["lon"], p["lat"], p["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def nearest(r):
    """(km, point) to the nearest personal point, or (Marktplaats' km to the search center, None)."""
    if r.get("lat") is None or r.get("lon") is None or not POINTS:
        return (r.get("km") or 0), None
    return min(((km_to(r, p), p) for p in POINTS), key=lambda x: x[0])


def eff_price(r):
    """What it will really cost: the asking price, or the highest bid when someone already bid more."""
    p = r.get("price")
    if p is None and r.get("ptype") == "FREE":
        p = 0
    if p is None:
        return None
    return max(p, r.get("top_bid") or 0)


MON_NEW = {"5K2K": 900, "UWQHD": 400, "DQHD": 900, "DFHD": 700, "4K": 380, "QHD": 260, "UWFHD": 280}


def mon_new(r):
    inch = r.get("inch") or 0
    if inch < 31:
        return None
    if r.get("new_price"):
        return r["new_price"]
    res = (r.get("res") or "").split()[0] if r.get("res") else ""
    n = MON_NEW.get(res, 170)
    return round(n * (1.6 if inch >= 37 and res.startswith(("UW", "5K", "D")) else 1.0))


def new_of(r, kind):
    return tv_new(r) if kind == "tv" else mon_new(r)


def score(price, fair_v, trip_eur):
    return 1 - (price + trip_eur) / fair_v + W * math.log10(fair_v)


def listing_score(r, kind, sev=None):
    f, p = fair(r, kind, sev), eff_price(r)
    if not f or p is None:
        return None
    return score(p, f, trip(nearest(r)[0]))


def usable(r, kind):
    p = r.get("price")
    if not p or p < 1 or r.get("accessory") or (r.get("ptype") or "") in ("SEE_DESCRIPTION", "RESERVED"):
        return False
    d = r.get("damage")
    if kind == "tv":
        return not d
    t = r.get("title") or ""
    if NOT_MONITOR.search(t) or (EXCL[0] and re.search(EXCL[0], t, re.I) and not re.search(EXCL[1], t, re.I)):
        return False  # stands, arms, cables, "past ook" ads: not a monitor
    return not (d and set(str(d).split(", ")) - {"sin cable/fuente"})


NOT_MONITOR = re.compile(r"\b(kabel|cable|adapter|schoen\w*|stand(aard)?|houder|beugel|arm|lamp|lightbar)\b|"
                         r"bureaustandaard|monitorstandaard|monitorarm", re.I)
_cfg = json.loads((ROOT / "alerts.json").read_text(encoding="utf-8"))
EXCL = (_cfg.get("exclude_title"), _cfg.get("exclude_title_unless", "$^"))


def add(ref, r, kind):
    """Reference entry [price, fair value, km to the public search center] (never a personal distance)."""
    f = fair(r, kind)
    if f and usable(r, kind):
        ref.setdefault(kind, {})[r["id"]] = [eff_price(r), round(f, 1), r.get("km") or 0]


def load(extra=None):
    ref = json.loads(REF_P.read_text(encoding="utf-8")) if REF_P.exists() else {}
    for k, d in (extra or {}).items():
        if k in ("tv", "monitor"):
            ref.setdefault(k, {}).update(d)
    return {k: sorted(score(e[0], e[1], trip(e[2])) for e in v.values() if len(e) == 3 and e[1] > 0)
            for k, v in ref.items()}


def percentile(ref, r, kind, sev=None):
    s = listing_score(r, kind, sev)
    scores = ref.get(kind) or []
    if s is None or len(scores) < MIN_N:
        return None
    lo = sum(1 for x in scores if x < s) + 0.5 * sum(1 for x in scores if x == s)
    return min(99, round(100 * lo / len(scores)))


def tag(ref, r, kind, sev=None):
    pc = percentile(ref, r, kind, sev)
    return f"P{pc}" if pc is not None else ""


def build():
    sys.path.insert(0, str(ROOT))
    import tv  # noqa: E402
    ref = {}
    c = sqlite3.connect(ROOT / "data" / "monitors.db")
    c.row_factory = sqlite3.Row
    for r in c.execute("SELECT * FROM listings WHERE src='mp' AND inch>=31"):
        add(ref, dict(r), "monitor")
    sweeps = sorted(p for p in (ROOT / "data").glob("tv_*.json") if p.stem[3:4].isdigit())
    if sweeps:
        for r in json.loads(sweeps[-1].read_text(encoding="utf-8")):
            add(ref, tv.parse(r), "tv")
    REF_P.write_text(json.dumps(ref, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    show()


def show():
    for k, v in load().items():
        q = statistics.quantiles(v, n=10)
        print(f"{k:8} n={len(v):5}  P10 {q[0]:.2f}  P50 {q[4]:.2f}  P90 {q[8]:.2f}  max {max(v):.2f}")


if __name__ == "__main__":
    {"build": build, "show": show}[(sys.argv[1:] or ["show"])[0]]()
