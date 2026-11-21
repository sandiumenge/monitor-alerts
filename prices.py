"""Value percentile (P0–P99): how good a deal a listing is compared with every TV (≥50") or monitor (≥31") seen.

Score = discount vs. new price + a small bonus for how expensive the product is new:
    score = (1 − price / new) + W · log10(new)
With W = 0.05 each ×10 in new price is worth 5 discount points: €100→€10 (−90 %) scores a bit below
€1000→€100 (−90 %), and the discount still matters most. P98 = a better score than 98 % of all listings seen.

New price: monitors use models.json when the model is known; otherwise (and for every TV) an estimate of what an
equivalent new product costs today, from size and resolution/panel. Damaged, accessory and price-less listings are
left out of the reference.

Reference: prices_ref.json (built locally from the database and the last TV sweep) + every listing the cloud run
sees (state "value_hist").

  python -I prices.py build      rebuild prices_ref.json from data/monitors.db and the last data/tv_*.json
  python -I prices.py show       reference sizes and score quantiles
"""
import json
import math
import re
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF_P = ROOT / "prices_ref.json"
W = 0.05
MIN_N = 30


def tv_new(r):
    inch = r.get("inch") or 0
    if inch < 50:
        return None
    base = 330 * (inch / 50) ** 2.3  # 4K LED today: 50" ≈ €330, 55" ≈ €410, 65" ≈ €600, 75" ≈ €840
    panel, res = r.get("panel"), r.get("res")
    k = 2.5 if panel == "OLED" else 1.4 if panel == "QLED" else 0.5 if panel == "PLASMA" else \
        1.0 if res in ("4K", "8K") else 0.75 if res in ("FHD", "HD") else 0.8
    return round(base * k)


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


def score(price, new):
    return (1 - price / new) + W * math.log10(new)


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
    new = new_of(r, kind)
    if new and usable(r, kind):
        ref.setdefault(kind, {})[r["id"]] = [r["price"], new]


def load(extra=None):
    ref = json.loads(REF_P.read_text(encoding="utf-8")) if REF_P.exists() else {}
    for k, d in (extra or {}).items():
        if k in ("tv", "monitor"):
            ref.setdefault(k, {}).update(d)
    return {k: sorted(score(p, n) for i, (p, n) in v.items()) for k, v in ref.items()}


def percentile(ref, r, kind):
    new, p = new_of(r, kind), r.get("price")
    scores = ref.get(kind) or []
    if not new or not p or len(scores) < MIN_N:
        return None
    s = score(p, new)
    lo = sum(1 for x in scores if x < s) + 0.5 * sum(1 for x in scores if x == s)
    return min(99, round(100 * lo / len(scores)))


def tag(ref, r, kind):
    pc = percentile(ref, r, kind)
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
