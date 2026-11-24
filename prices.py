"""Value percentile (P0–P99): how good a deal a listing is compared with every listing of its kind seen.

Fair value = what an equivalent product costs new today × age × condition, per kind (kinds/<name>.py):
    new today      Kind.new_price (TV: size/panel/brand formula; monitor: models.json or by resolution)
    age            Kind.age_rate ^ years (TV 0.90, monitor 0.93; floor Kind.age_floor); unknown year: Kind.year()
    condition      Kind.COND: 1 / 0.8 lightly damaged / 0.1 heavily damaged
Cost = effective price (asking price, or the highest bid if higher) + a public-transport round trip:
    2 × (€1.16 + €0.217 × 1.3 × km) fare + 2 × (10 min + 3 min/km) at €10/h   (2 km ≈ €9, 7 km ≈ €17, 15 km ≈ €29)
Score = 1 − cost / fair + W · log10(fair). With W = 0.05 each ×10 in fair value is worth 5 points, so a €100 TV
for €10 scores a bit below a €1000 one for €100. P98 = a better score than 98 % of all listings seen.

The listing scored uses its distance to the nearest personal point (ALERT_POINTS); the reference uses Marktplaats'
distance to the public search center, so no personal distance is ever stored. Damaged, accessory and price-less
listings are left out of the reference.

Reference: prices_ref.json (built locally from the database and the last TV sweep) + every listing the cloud run
sees (state "value_ref").

  python -I prices.py build      rebuild prices_ref.json from every kind's ref_rows() (monitors.db, data/<kind>_*.json)
  python -I prices.py show       reference sizes and score quantiles
"""
import datetime as dt
import json
import math
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF_P = ROOT / "prices_ref.json"
W = 0.05
MIN_N = 30


NOW = dt.date.today().year


def K(kind):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import kinds  # noqa: E402  (late: kinds import prices)
    return kinds.get(kind)


def fair(r, kind, sev=None):
    """Fair second-hand value: new-today equivalent × age × condition (sev: None / 'light' / 'heavy')."""
    k = K(kind)
    n = k.new_price(r)
    if not n:
        return None
    age = max(0, NOW - k.year(r))
    return n * max(k.age_floor, k.age_rate ** age) * k.COND.get(sev, 1.0)


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


def new_of(r, kind):
    return K(kind).new_price(r)


def score(price, fair_v, trip_eur):
    return 1 - (price + trip_eur) / fair_v + W * math.log10(fair_v)


def listing_score(r, kind, sev=None):
    f, p = fair(r, kind, sev), eff_price(r)
    if not f or p is None:
        return None
    return score(p, f, trip(nearest(r)[0]))


def add(ref, r, kind):
    """Reference entry [price, fair value, km to the public search center] (never a personal distance)."""
    k = K(kind)
    f = fair(r, k)
    if f and k.usable_ref(r):
        ref.setdefault(k.name, {})[r["id"]] = [eff_price(r), round(f, 1), r.get("km") or 0]


def load(extra=None):
    ref = json.loads(REF_P.read_text(encoding="utf-8")) if REF_P.exists() else {}
    for k, d in (extra or {}).items():
        ref.setdefault(k, {}).update(d)
    return {k: sorted(score(e[0], e[1], trip(e[2])) for e in v.values() if len(e) == 3 and e[1] > 0)
            for k, v in ref.items()}


def percentile(ref, r, kind, sev=None):
    s = listing_score(r, kind, sev)
    scores = ref.get(K(kind).name) or []
    if s is None or len(scores) < MIN_N:
        return None
    lo = sum(1 for x in scores if x < s) + 0.5 * sum(1 for x in scores if x == s)
    return min(99, round(100 * lo / len(scores)))


def tag(ref, r, kind, sev=None):
    pc = percentile(ref, r, kind, sev)
    return f"P{pc}" if pc is not None else ""


def build():
    K("monitor")  # puts the project on sys.path
    ref = {}
    import kinds  # noqa: E402
    for k in kinds.all_kinds().values():
        for r in k.ref_rows():
            add(ref, r, k)
    REF_P.write_text(json.dumps(ref, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    show()


def show():
    for k, v in load().items():
        q = statistics.quantiles(v, n=10)
        print(f"{k:8} n={len(v):5}  P10 {q[0]:.2f}  P50 {q[4]:.2f}  P90 {q[8]:.2f}  max {max(v):.2f}")


if __name__ == "__main__":
    {"build": build, "show": show}[(sys.argv[1:] or ["show"])[0]]()
