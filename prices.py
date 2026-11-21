"""Price percentile: how cheap a listing is compared with every similar listing seen so far.

P97 = cheaper than 97 % of the comparable listings (same kind, size band and resolution/panel tier). Damaged,
accessory and price-less listings are left out of the reference. Similar = for TVs size band (50–54, 55–64, 65+)
x tier (OLED/QLED, 4K, rest); for monitors the resolution class (ultrawide, 4K, QHD, UWFHD, rest), all ≥ 31".

The reference lives in prices_ref.json (built locally from the database and the last TV sweep) and grows in the
cloud: alert.py adds every newly seen listing to the state ("price_hist").

  python -I prices.py build      rebuild prices_ref.json from data/monitors.db and the last data/tv_*.json
  python -I prices.py show       group sizes and medians
"""
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF_P = ROOT / "prices_ref.json"
MIN_N = 15  # below this, fall back to the broader group


def tv_group(r):
    inch = r.get("inch") or 0
    if inch < 50:
        return None
    band = "50–54\"" if inch < 55 else "55–64\"" if inch < 65 else "65\"+"
    tier = "OLED/QLED" if r.get("panel") in ("OLED", "QLED") else "4K" if r.get("res") in ("4K", "8K") else "HD/FHD/?"
    return f"tv {band} {tier}"


def mon_group(r):
    if (r.get("inch") or 0) < 31:
        return None
    res = r.get("res") or ""
    cls = ("ultrawide" if res.startswith(("UWQHD", "5K2K", "DFHD", "DQHD")) else "4K" if res.startswith("4K")
           else "QHD" if res.startswith("QHD") else "UWFHD" if res.startswith("UWFHD") else "FHD/?")
    return f"mon {cls}"


def broader(g):
    return " ".join(g.split()[:2]) if g.startswith("tv") else "mon all"


def label(g):
    k, *rest = g.split()
    if k == "tv":
        return f"teles {' '.join(rest)}"
    return "monitores ≥31\"" if rest == ["all"] else f"monitores {rest[0]} ≥31\""


def usable(r, kind):
    p = r.get("price")
    if not p or p < 1 or r.get("accessory") or (r.get("ptype") or "") in ("SEE_DESCRIPTION", "RESERVED"):
        return False
    d = r.get("damage")
    if kind == "tv":
        return not d
    return not (d and set(str(d).split(", ")) - {"sin cable/fuente"})


def group_of(r, kind):
    return tv_group(r) if kind == "tv" else mon_group(r)


def add(ref, r, kind):
    g = group_of(r, kind)
    if g and usable(r, kind):
        for key in (g, broader(g)):
            ref.setdefault(key, {})[r["id"]] = r["price"]


def load(extra=None):
    ref = json.loads(REF_P.read_text(encoding="utf-8")) if REF_P.exists() else {}
    for g, d in (extra or {}).items():
        ref.setdefault(g, {}).update(d)
    return ref


def percentile(ref, r, kind):
    """(P, n, group) or None. P = % of comparable listings priced higher (ties count half)."""
    g, p = group_of(r, kind), r.get("price")
    if not g or not p:
        return None
    for key in (g, broader(g)):
        prices = [v for k, v in (ref.get(key) or {}).items() if k != r.get("id")]
        if len(prices) >= MIN_N:
            hi = sum(1 for x in prices if x > p) + 0.5 * sum(1 for x in prices if x == p)
            return min(99, round(100 * hi / len(prices))), len(prices), key
    return None


def line(ref, r, kind):
    x = percentile(ref, r, kind)
    if not x:
        return ""
    pc, n, g = x
    return f"📊 P{pc} · más barato que el {pc} % de {n} {label(g)} vistos"


def build():
    sys.path.insert(0, str(ROOT))
    import db  # noqa: E402
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
    show(ref)


def show(ref=None):
    ref = ref or load()
    for g in sorted(ref):
        v = list(ref[g].values())
        print(f"{g:28} n={len(v):5}  mediana €{statistics.median(v):.0f}")


if __name__ == "__main__":
    {"build": build, "show": show}[(sys.argv[1:] or ["show"])[0]]()
