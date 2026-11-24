"""Shared by monitors and TVs: screen damage (light vs heavy), the AI prompt and screen size formatting."""
import re

import db
import tv as tvparse
from .base import Kind

# light damage = acceptable: a few dead/white pixels, light bleed, a dent
LIGHT_RX = re.compile(
    r"dode? pixels?|pixel ?(fout\w*|defect\w*)|pixelfout\w*|dead pixels?|stuck pixels?|"
    r"\b(paar|enkele|kleine?|lichte?|witte?)\s+(vlek\w*|stip\w*|plek\w*|puntje\w*)|\bwitte? vlek\w*|"
    r"clouding|lichtlek\w*|backlight ?bleed\w*|light ?bleed\w*|ips ?glow|\bdeuk(je|jes)?\b|\bdent\b", re.I)
HEAVY_LABELS = {"roto/grieta", "defecto", "daños", "rayas", "líneas", "parpadeo", "quemado", "zona oscura",
                "tono de color", "manchas"}
DAMAGE_EN = [(re.compile(a, re.I), b, c) for a, b, c in [  # Dutch/English phrases → short English (never quote Dutch)
    (r"dode? pixels?|pixel ?(fout\w*|defect\w*)|pixelfout\w*|dead pixels?|stuck pixels?", "dead pixels", "light"),
    (r"\b(witte?|lichte?)\s+(vlek\w*|stip\w*|plek\w*|puntje\w*)|\bwitte? vlek\w*|white spots?", "white spots", "light"),
    (r"\b(paar|enkele|kleine?)\s+(vlek\w*|stip\w*|plek\w*|puntje\w*)", "small spots", "light"),
    (r"clouding|lichtlek\w*|backlight ?bleed\w*|light ?bleed\w*|ips ?glow", "backlight bleed", "light"),
    (r"\bdeuk(je|jes)?\b|\bdent\b", "a dent", "light"),
    (r"barst\w*|gebarsten|scheur\w*|gebroken|\bcrack\w*|broken screen|scherm (is )?kapot", "cracked screen", "heavy"),
    (r"\bkras\w*|scratch\w*", "scratches", "heavy"),
    (r"\blijn(en|tje|tjes)?\b|streep|strepen|\blines?\b", "lines on screen", "heavy"),
    (r"inbrand\w*|burn.?in|ingebrand", "burn-in", "heavy"),
    (r"flikker\w*|knipper\w*|flicker\w*", "flickering", "heavy"),
    (r"geen beeld|zwart (beeld|scherm)|no picture|black screen", "no picture", "heavy"),
    (r"backlight|achtergrondverlichting", "backlight fault", "heavy"),
    (r"donkere? (vlek\w*|plek\w*|zone|gedeelte)|dark (spot|area)\w*", "dark areas", "heavy"),
    (r"verkleuring|kleurafwijking|roze|paarse? waas|green tint|purple tint", "colour fault", "heavy"),
    (r"\bvlek(ken)?\b|\bstains?\b", "stains on screen", "heavy"),
    (r"\bkapot\b|\bdefect\b|\bstuk\b|werkt niet|doet het niet|not working|voor onderdelen|for parts", "broken / for parts", "heavy"),
]]

SYSTEM = (
    "You check second-hand listings from Marktplaats (Netherlands) for a buyer. Identify the exact product model "
    "from the title, description and photos (labels, stickers, remote, stand, logos). If the model is known or can be "
    "narrowed down, search the web for its specs and for its current new price in the Netherlands/EU (or its last "
    "price when it was sold new, if discontinued). If the exact model cannot be found, still give the MOST LIKELY "
    "resolution, panel, Smart and new price for that brand, size, year and range (e.g. which Ambilight 55\" models "
    "Philips sold that year) and set confidence to media or baja; use null only when there is no basis at all. "
    "Answer with ONE JSON object "
    "and nothing else, with these keys:\n"
    '{"model": str|null, "inch": number|null, "res": "HD"|"FHD"|"QHD"|"UWFHD"|"UWQHD"|"4K"|"5K2K"|"8K"|null, '
    '"panel": "OLED"|"QLED"|"LED"|"LCD"|"PLASMA"|"IPS"|"VA"|"TN"|null, "smart": bool|null, "hz": int|null, '
    '"year": int|null, "usbc_w": int|null, "new_price_eur": number|null, "confidence": "alta"|"media"|"baja", '
    '"defects": str|null, "damage": "none"|"light"|"heavy", "note": str}\n'
    "usbc_w = USB-C power delivery in watts (monitors only). defects = any damage or fault the seller mentions or "
    "the photos show, in English, short; null when there is none. damage = light for small faults a buyer can live "
    "with (a few dead or white pixels, slight backlight bleed, a dent in the casing, missing remote/stand), heavy for "
    "scratches on the screen, cracks, lines, dark or discoloured areas, burn-in, flickering or not working. note = one short sentence in English for the buyer (max 90 characters): "
    "what it is and whether the price is good."
)


NANO = re.compile(r"nano ?cell|\b\d{2}nano\d", re.I)
CURVED = re.compile(r"curved|gebogen|\bcurve\b|\bcurvo\b|\bC\d{2}[A-Z]", re.I)
BRAND_NAME = {"lg": "LG", "aoc": "AOC", "hp": "HP", "msi": "MSI", "tcl": "TCL", "jvc": "JVC", "benq": "BenQ",
              "iiyama": "iiyama", "nec": "NEC", "lc-power": "LC-Power", "lcpower": "LC-Power", "bang": "B&O"}


def inch_s(f):
    return f"{f['inch']:g}\"" if isinstance(f["inch"], (int, float)) else ""


class ScreenKind(Kind):
    IGNORED_DAMAGE = {"sin cable/fuente"}
    DAMAGE_EN = DAMAGE_EN
    UNSURE = "❓ Size or resolution unknown and could not be checked"
    AI_COPY = ("inch", "res", "panel", "hz", "year", "smart")
    BRAND_RX = None  # set by monitor / tv

    def facts(self, r):
        """Specs for the push: the AI's answer first (it saw the photos and looked the model up), the listing's own
        parse filling the gaps. guess = resolution not confirmed (inferred from the year / low AI confidence)."""
        a = r.get("ai") or {}

        def g(f):
            return a.get(f) if a.get(f) not in (None, "") else r.get(f)
        text = f"{r.get('title') or ''} {a.get('model') or ''} {r.get('model') or ''}"
        b = self.BRAND_RX.search(text)
        brand = (b.group(1) if b else r.get("brand") or "").lower()
        if brand.startswith(("overig", "other", "onbekend", "merkloos")):  # Marktplaats' "Overige merken" = no brand
            brand = ""
        usbc_w = a.get("usbc_w") or (int(m.group(1)) if (m := re.search(r"(\d+)W", r.get("usbc") or "")) else None)
        return {"brand": BRAND_NAME.get(brand.replace(" ", ""), brand.title()) if brand else "",
                "inch": g("inch"), "res": g("res"), "panel": g("panel"), "hz": g("hz"), "smart": g("smart"),
                "year": g("year"), "usbc_w": usbc_w,
                "guess": bool(r.get("res_guess")) or a.get("confidence") == "baja",
                "nano": bool(NANO.search(text)), "curved": bool(CURVED.search(f"{text} {r.get('desc') or ''}")),
                "uw": bool(r.get("ultrawide")) or (g("res") or "").startswith(("UW", "5K2K", "D"))}

    def classify_damage(self, r):
        """heavy: scratches, cracks, lines, dark areas, colour faults, not working; else light."""
        text = f"{r.get('title') or ''}. {r.get('desc') or ''}"
        rest = LIGHT_RX.sub(" ", text)
        if r.get("cond") == tvparse.NOT_WORKING or tvparse.has_damage(rest) or set(db.flags(rest, db.DAMAGE)) & HEAVY_LABELS:
            return "heavy"
        return "light"

    def ai_system(self):
        return SYSTEM

    def summary_cols(self, r):
        return [("Size", '%g"' % r["inch"] if r.get("inch") else "?"), ("Res.", r.get("res") or "?")]
