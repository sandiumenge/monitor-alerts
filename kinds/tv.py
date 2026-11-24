"""Large TVs ≥ 50": parsing in tv.py (size, resolution, panel, Smart, model year)."""
import re

import tv as tvparse
from .base import GREEN, ORANGE, RED
from .screen import NANO, ScreenKind, inch_s

BUDGET = re.compile(r"\b(salora|blaupunkt|thomson|telefunken|kiano|medion|akai|strong|hyundai|jvc|sharp|nokia|"
                    r"polaroid|denver|finlux|techwood|continental|bush|orion|lenco|dyon|manta|hkc|qilive|silvercrest)\b", re.I)
MID = re.compile(r"\b(hisense|tcl|toshiba|grundig|haier|xiaomi)\b", re.I)


class Kind(ScreenKind):
    BRAND_RX = re.compile(rf"\b({tvparse.BRANDS})\b", re.I)

    def parse(self, r):
        return tvparse.parse(r)

    # ---------------------------------------------------------------- rules
    def prefilter(self, cfg, r, warn):
        """≥ min_inch; never parts / accessories / wanted ads ("gezocht")."""
        if (r.get("damage") and not warn) or r.get("accessory") or not (r.get("inch") and r["inch"] >= self.cfg.get("min_inch", 50)):
            return False
        return not (self.cfg.get("none") and re.search(self.cfg["none"], f"{r.get('title')} {r.get('desc') or ''}", re.I))

    def maybe_rule(self, cfg, r):
        """Resolution or Smart not stated: would it match if it were a 4K Smart TV? (plasmas never are)."""
        if r.get("panel") == "PLASMA" or "plasma" in (r.get("title") or "").lower():
            return None
        v = dict(r)
        if not r.get("res") or r.get("res_guess"):
            v["res"] = "4K"
        if not r.get("smart") and not re.search(r"geen smart|niet smart", f"{r.get('title')} {r.get('desc')}", re.I):
            v["smart"] = True
        return self.first_rule(cfg, v) if v != r else None

    # ---------------------------------------------------------------- value
    def new_price(self, r):
        """What an equivalent TV costs new today: 330 € × (inch/50)^2.3 (4K LED; 55" ≈ €410, 65" ≈ €600)
        × panel (OLED 2.5, QLED 1.4, NanoCell 1.3, plasma 0.3; QLED of a budget brand 1.0) × 8K 1.8
        × brand (budget 0.55; Hisense/TCL/Toshiba… 0.8) × 0.9 if not 4K/8K × 0.9 if not Smart."""
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

    def year(self, r):
        """Unknown year: plasma 2011, OLED 2020, 4K 2018, other known resolution 2013, nothing known 2015."""
        y = r.get("year") or (r.get("ai") or {}).get("year")
        import prices
        if isinstance(y, (int, float)) and 1995 < y <= prices.NOW:
            return int(y)
        p, res = r.get("panel"), r.get("res")
        return 2011 if p == "PLASMA" else 2020 if p == "OLED" else 2018 if res in ("4K", "8K") else 2013 if res else 2015

    def usable_ref(self, r):
        p = r.get("price")
        if not p or p < 1 or r.get("accessory") or (r.get("ptype") or "") in ("SEE_DESCRIPTION", "RESERVED"):
            return False
        return not r.get("damage")

    # ---------------------------------------------------------------- push layout
    def headline_bits(self, f, r):
        res = (f["res"] or "") + ("?" if f["res"] and f["guess"] else "")
        panel = "NanoCell" if f["nano"] else {"OLED": "OLED", "QLED": "QLED", "PLASMA": "Plasma"}.get(f["panel"], "")
        top = panel in ("OLED", "QLED", "NanoCell") and res.startswith("4K")  # these imply 4K: keep the panel
        return [(f["brand"] or "TV", 0), (inch_s(f), 0), (res, 2 if top else 1), (panel, 1 if top else 2),
                ("Smart" if f["smart"] else "", 3)]

    def spec_bits(self, f, r):
        q = "?" if f["guess"] else ""
        return [inch_s(f), (f["res"] or "res ?") + (q if f["res"] else ""),
                "NanoCell" if f["nano"] else f["panel"] or "", "Smart" if f["smart"] else "",
                f"{f['hz']}Hz" if f["hz"] and f["hz"] > 60 else "", str(f["year"] or "")]

    def quality_dot(self, f, r):
        """4K/8K +2, OLED +2, QLED/NanoCell +1 (not for budget brands), ≥65" +2, ≥55" +1, 2020 or newer +1 →
        🟢 ≥4 (55" OLED, 65" 4K, recent QLED), 🟠 2–3 (plain 4K), 🔴 ≤1 (Full HD, HD, plasma, unknown)."""
        if f["panel"] == "PLASMA":
            return RED
        res = f["res"] or ""
        budget = bool(BUDGET.search(f"{f['brand']} {r.get('title') or ''}"))
        pts = 2 * (res in ("4K", "8K")) + 2 * (f["panel"] == "OLED") + \
            ((f["nano"] or f["panel"] == "QLED") and not budget) + \
            (2 if (f["inch"] or 0) >= 65 else 1 if (f["inch"] or 0) >= 55 else 0) + \
            (isinstance(f["year"], int) and f["year"] >= 2020)
        return GREEN if pts >= 4 else ORANGE if pts >= 2 else RED

    def summary_cols(self, r):
        return super().summary_cols(r) + [("Panel", r.get("panel") or "?")]

    def sample(self):
        return {"title": "Samsung 65 inch QLED", "price": 20, "ptype": "FIXED", "inch": 65, "res": "4K", "panel": "QLED",
                "smart": True, "year": 2019, "city": "Amsterdam", "km": 1.6,
                "desc": "Scherm gebarsten na val, strepen in beeld", "damage": "roto/grieta"}
