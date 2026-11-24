"""Computer monitors ≥ 31": parsing in db.enrich (+ models.json), local SQLite database when run on Windows."""
import re
import sqlite3

import db
from .base import GREEN, ORANGE, RED
from .screen import ScreenKind, inch_s

MON_BRANDS = re.compile(r"\b(dell|alienware|lg|samsung|asus|acer|aoc|benq|hp|iiyama|philips|msi|lenovo|viewsonic|"
                        r"gigabyte|eizo|xiaomi|huawei|koorui|fujitsu|nec|medion|sony|apple|corsair|cooler master|"
                        r"lc-?power|hannspree|sharp|dough|gamemax|pixio|razer|mobiuz|titan army)\b", re.I)
RES_PX = {"UWQHD": "3440×1440", "UWFHD": "2560×1080", "QHD": "2560×1440", "4K": "3840×2160", "5K2K": "5120×2160",
          "FHD": "1920×1080", "DQHD": "5120×1440", "DFHD": "3840×1080", "HD": "1366×768"}
MON_NEW = {"5K2K": 900, "UWQHD": 400, "DQHD": 900, "DFHD": 700, "4K": 380, "QHD": 260, "UWFHD": 280}
GUESS_INCH = {"UWQHD": 34, "5K2K": 34, "4K": 32, "QHD": 32}
# too small to be worth an AI call: "AOC27", "HP E27", "24 inch", model codes W2234S / P2252HS / RG240Y, portables
SMALL_HINT = re.compile(r"(?<!\d)(1[5-9]|2[0-9])(?!\d)|[a-z](1[5-9]|2[0-7])\d{1,2}[a-z]{0,2}\b|"
                        r"portable|draagba|zenscreen|\bcm\b|beugel|carelink|cisco", re.I)
NOT_MONITOR = re.compile(r"\b(kabel|cable|adapter|schoen\w*|stand(aard)?|houder|beugel|arm|lamp|lightbar)\b|"
                         r"bureaustandaard|monitorstandaard|monitorarm", re.I)


class Kind(ScreenKind):
    BRAND_RX = MON_BRANDS
    AI_NEW_PRICE_ALWAYS = False  # a model from models.json keeps its researched new price

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.wanted = re.compile(next((ru["models"] for ru in cfg.get("rules", []) if ru.get("models")), "$^"), re.I)

    # ---------------------------------------------------------------- fetch
    def keep(self, r):
        return db.keep(r)

    def prepare(self, r):
        r = dict(r)
        r["posted"] = r.pop("date")
        return r

    def parse(self, r):
        return db.enrich(dict({k: None for k in db.COLS}, **r))

    def pool(self, raw, parsed, cloud, s):
        if cloud:  # no database in the cloud: rules run on the newest listings only
            return super().pool(raw, parsed, cloud, s)
        c = db.con()  # locally: every active listing, so matches found by `db.py sync` or a price drop also notify
        n_new = db.upsert(c, raw)
        return [dict(r) for r in c.execute("SELECT * FROM listings WHERE active=1").fetchall()], [], n_new

    # ---------------------------------------------------------------- rules
    def prefilter(self, cfg, r, warn):
        """Drop listings with a known size under min_inch and accessory-only titles."""
        if r.get("inch") and r["inch"] < self.cfg.get("min_inch", 0):
            return False
        t = r.get("title") or ""
        if self.cfg.get("exclude_title") and re.search(self.cfg["exclude_title"], t, re.I) and \
                not re.search(self.cfg.get("exclude_title_unless", "$^"), t, re.I):
            return False
        return True

    def special(self, key, value, r):
        if key == "usbc_charge":
            return not value or bool(re.search(r"\d+W|yes", r.get("usbc") or ""))
        return None

    def maybe_rule(self, cfg, r):
        """Size or resolution unknown: would it match with a typical size for its resolution (or vice versa)?"""
        if r.get("inch") and r["inch"] >= self.cfg.get("min_inch", 0) and not r.get("res"):
            for res in GUESS_INCH:
                if ru := self.first_rule(cfg, dict(r, res=res)):
                    return ru
        if r.get("res") in GUESS_INCH and not r.get("inch"):
            return self.first_rule(cfg, dict(r, inch=GUESS_INCH[r["res"]]))
        if not r.get("inch") and not r.get("res") and not SMALL_HINT.search(r.get("title") or ""):
            # neither size nor resolution in the text ("LG curved monitor, perfecte staat"): only the photos tell
            for res, inch in GUESS_INCH.items():
                if ru := self.first_rule(cfg, dict(r, inch=inch, res=res)):
                    return ru
        return None

    # ---------------------------------------------------------------- value
    def new_price(self, r):
        """models.json price when the model is known, else by resolution (MON_NEW)."""
        inch = r.get("inch") or 0
        if inch < 31:
            return None
        if r.get("new_price"):
            return r["new_price"]
        res = (r.get("res") or "").split()[0] if r.get("res") else ""
        n = MON_NEW.get(res, 170)
        return round(n * (1.6 if inch >= 37 and res.startswith(("UW", "5K", "D")) else 1.0))

    def usable_ref(self, r):
        p = r.get("price")
        if not p or p < 1 or r.get("accessory") or (r.get("ptype") or "") in ("SEE_DESCRIPTION", "RESERVED"):
            return False
        t = r.get("title") or ""
        ex = self.cfg.get("exclude_title")
        if NOT_MONITOR.search(t) or (ex and re.search(ex, t, re.I) and not re.search(self.cfg.get("exclude_title_unless", "$^"), t, re.I)):
            return False  # stands, arms, cables, "past ook" ads: not a monitor
        d = r.get("damage")
        return not (d and set(str(d).split(", ")) - self.IGNORED_DAMAGE)

    def ref_rows(self):
        c = sqlite3.connect(db.DB_P)
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM listings WHERE src='mp' AND inch>=31")]

    def test_rows(self):
        return [dict(r) for r in db.con().execute("SELECT * FROM listings WHERE active=1").fetchall()]

    # ---------------------------------------------------------------- push layout
    def headline_bits(self, f, r):
        res = (f["res"] or "") + ("?" if f["res"] and f["guess"] else "")
        return [(f["brand"] or "Monitor", 0), (inch_s(f), 0), ("ultrawide" if f["uw"] else res, 1),
                ("USB‑C" if f["usbc_w"] else "", 2), ("curved" if f["curved"] else "", 3)]

    def spec_bits(self, f, r):
        q = "?" if f["guess"] else ""
        res = f["res"] or ""
        return [inch_s(f), f"{RES_PX[res]} ({res}){q}" if res in RES_PX else res + q if res else "res ?",
                f"{f['hz']}Hz" if f["hz"] else "", f["panel"] or "",
                f"USB‑C {f['usbc_w']}W" if f["usbc_w"] else "", "curved" if f["curved"] else "", str(f["year"] or "")]

    def quality_dot(self, f, r):
        """UWQHD/5K2K/DQHD/4K +2, QHD +1, ≥100Hz +1, USB‑C +1 → 🟢 ≥3, 🟠 2, 🔴 ≤1; a wanted model is always 🟢."""
        if self.wanted.search(f"{r.get('title') or ''} {r.get('model') or ''} {(r.get('ai') or {}).get('model') or ''}"):
            return GREEN
        res = f["res"] or ""
        pts = (2 if res in ("UWQHD", "5K2K", "DQHD", "4K") else 1 if res == "QHD" else 0) + \
            ((f["hz"] or 0) >= 100) + bool(f["usbc_w"])
        return GREEN if pts >= 3 else ORANGE if pts >= 2 else RED

    def sample(self):
        return {"title": "iiyama 34 inch ultrawide", "price": 75, "ptype": "FIXED", "inch": 34, "res": "UWQHD",
                "panel": "VA", "hz": 75, "usbc": "USB-C 65W", "ultrawide": True, "city": "Amsterdam", "km": 3.2,
                "desc": "2 dode pixels rechtsboven, verder perfect", "damage": "dode pixels"}

    # ---------------------------------------------------------------- AI
    def ai_apply_extra(self, r, a):
        if a.get("model") and not r.get("model"):
            r["model"] = a["model"]
        if a.get("usbc_w") and not re.search(r"\d+W", r.get("usbc") or ""):
            r["usbc"] = f"{a['usbc_w']}W"
        return r

