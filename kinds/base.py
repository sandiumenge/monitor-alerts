"""Generic kind: everything alert.py needs to search, filter, value and present one kind of product.

A kind's JSON (kinds/<name>.json) holds the data; override methods in kinds/<name>.py only where the product needs
its own logic. Keys used here:

  label, plural        "TV" / "TVs" (heartbeat and run summary)
  icon                 ntfy tag shown before every push title (desktop_computer, tv, bike, …)
  topic                push topic prefix: the NTFY_TOPIC secret "monitores-<id>" becomes "<topic>-<id>"
  ai_product           how the AI prompt names the product ("computer monitor")
  order                run order (AI budget and toasts are shared, first kinds first)
  enabled              false = skipped
  search               {"l1": category, "l2": subcategory, "max_price": €, "queries": ["…"] (optional)}
  min_inch, none, …    kind-wide filters (see prefilter in the kind's .py)
  rules                [{name, max_price, …}]: the first rule a listing matches fires (see match)
  value                {"age_rate": 0.92, "age_floor": 0.25, "year_default": 2019, "new_price": €}
"""
import re
import time
import random

import hunt

GREEN, ORANGE, RED = "🟢", "🟠", "🔴"
NEG_DEFECTS = re.compile(r"(ninguno|ninguna|no |sin |none|nada|nothing|n/a)", re.I)
_requests = [0]


def pace():
    """Space every Marktplaats request of a run 3–6 s apart (none before the first)."""
    if _requests[0]:
        time.sleep(random.uniform(3, 6))
    _requests[0] += 1


class Kind:
    COND = {None: 1.0, "light": 0.8, "heavy": 0.1}  # condition factor of the fair value
    IGNORED_DAMAGE = set()  # damage labels that do not count as damage (e.g. "no power cable")
    DAMAGE_EN = []  # [(compiled regex, short English text, "light"/"heavy")] for the push damage line
    UNSURE = "❓ Key facts unknown and could not be checked"
    AI_COPY = ("year",)  # AI answer fields copied into the listing when it does not state them
    AI_NEW_PRICE_ALWAYS = True  # the AI's new price replaces the listing's own estimate

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.label = cfg.get("label", name.title())
        self.plural = cfg.get("plural", self.label + "s")
        self.icon = cfg.get("icon")
        self.topic_prefix = cfg.get("topic", name)
        self.ai_product = cfg.get("ai_product", self.label)
        self.value = cfg.get("value") or {}

    def __repr__(self):
        return f"<kind {self.name}>"

    # ---------------------------------------------------------------- push topic
    def topic(self, base, default_kind=False):
        """Push topic from the NTFY_TOPIC secret: 'monitores-<id>' → '<topic>-<id>'; any other secret is used as is
        for the first kind and with '-<name>' for the others."""
        if not base:
            return ""
        if base.startswith("monitores-"):
            return f"{self.topic_prefix}-{base[len('monitores-'):]}"
        return base if default_kind else f"{base}-{self.name}"

    # ---------------------------------------------------------------- fetch
    def search_params(self):
        s = self.cfg.get("search") or {}
        base = {"l1CategoryId": s["l1"], "l2CategoryIds": s["l2"], "limit": 100, "offset": 0,
                "sortBy": "SORT_INDEX", "sortOrder": "DECREASING", "viewOptions": "list-view"}
        if s.get("max_price"):
            base["attributeRanges[]"] = f"PriceCents:null:{s['max_price'] * 100}"
        return [dict(base, query=q) for q in s["queries"]] if s.get("queries") else [base]

    def fetch(self):
        """Newest page per center (and per query): raw rows, merged when found around both centers."""
        rows = {}
        for params in self.search_params():
            for city, pc in hunt.CFG["centers"].items():
                pace()
                j = hunt.mp_get(dict(params, postcode=pc, distanceMeters=hunt.radius_m(city)))
                for x in j.get("listings") or []:
                    r = hunt.mp_row(x)
                    r["near"] = city
                    if r["id"] in rows:
                        old = rows[r["id"]]
                        if city not in old["near"].split(","):
                            r["near"] = f"{old['near']},{city}"
                        else:
                            r["near"] = old["near"]
                        r["km"] = min(old["km"], r["km"])
                    rows[r["id"]] = r
        return [self.prepare(r) for r in rows.values() if self.keep(r)]

    def keep(self, r):
        return True

    def prepare(self, r):
        return r

    def parse(self, r):
        """Facts from the title/description (also after the full description was fetched)."""
        return r

    def pool(self, raw, parsed, cloud, s):
        """(listings to check, new ones since the previous run, number new). Default: the newest page only."""
        key = f"{self.name}_seen_ids"
        prev = set(s.get(key, []))
        new = [r for r in parsed if r["id"] not in prev] if prev else []
        s[key] = [r["id"] for r in parsed]
        return parsed, new, len(new)

    # ---------------------------------------------------------------- rules
    def prefilter(self, cfg, r, warn):
        """Kind-wide filter before the rules (accessories, wanted ads, …)."""
        t = f"{r.get('title') or ''} {r.get('desc') or ''}"
        return not (self.cfg.get("none") and re.search(self.cfg["none"], t, re.I))

    def first_rule(self, cfg, r):
        warn = cfg.get("damage", "warn") == "warn"
        if not self.prefilter(cfg, r, warn):
            return None
        f = self.price_factor(cfg, r)
        return next((ru for ru in self.cfg.get("rules", []) if self.match(ru, r, f, warn)), None)

    def maybe_rule(self, cfg, r):
        """A rule the listing would match if a fact it does not state turned out favourable (asked to the AI)."""
        return None

    def price_factor(self, cfg, r):
        """Listings only found around the other center (Utrecht) and heavily damaged ones must be bigger steals."""
        f = 1.0
        primary = cfg.get("primary_center")
        if primary and r.get("near") and primary not in r["near"]:
            f *= cfg.get("other_center_price_factor", 1.0)
        if self.severity(r) == "heavy":
            f *= cfg.get("heavy_damage_price_factor", 0.5)
        return f

    def special(self, key, value, r):
        """Rule keys a kind handles itself: return True/False, or None if the key is not special."""
        return None

    def match(self, rule, r, price_factor=1.0, warn=True):
        """Generic rule keys: max_price (+ allow_no_price), min_<f> / max_<f> / min_<f>_if_known (numbers),
        <f>: [list] (value must be in it), not_<f>: [list], <f>: true (must be truthy), min_discount, models / any
        (regex that must appear), none (regex that must not), no_damage, max_km. Anything else: special()."""
        text = f"{r.get('title') or ''} \n {r.get('desc') or ''} \n {r.get('model') or ''}"
        for k, v in rule.items():
            if k in ("name", "allow_no_price") or k.startswith("_"):
                continue
            sp = self.special(k, v, r)
            if sp is not None:
                if not sp:
                    return False
                continue
            if k == "max_price":
                p = r.get("price")
                if (p is None and not rule.get("allow_no_price")) or (p is not None and p > v * price_factor):
                    return False
            elif k == "min_discount":
                if not (r.get("discount") is not None and r["discount"] >= v):
                    return False
            elif k in ("models", "any"):
                if not re.search(v, text, re.I):
                    return False
            elif k == "none":
                if re.search(v, text, re.I):
                    return False
            elif k == "no_damage":
                if v and not warn and set(filter(None, (r.get("damage") or "").split(", "))) - self.IGNORED_DAMAGE:
                    return False
            elif k == "max_km":
                if (r.get("km") or 0) > v:
                    return False
            elif k.startswith("min_") and k.endswith("_if_known"):
                x = r.get(k[4:-9])
                if x and x < v:
                    return False
            elif k.startswith("min_"):
                x = r.get(k[4:])
                if not (x and x >= v):
                    return False
            elif k.startswith("max_"):
                x = r.get(k[4:])
                if not (x and x <= v):
                    return False
            elif k.startswith("not_") and isinstance(v, list):
                if r.get(k[4:]) in v:
                    return False
            elif isinstance(v, list):
                if r.get(k) not in v:
                    return False
            elif v is True:
                if not r.get(k):
                    return False
            else:
                raise ValueError(f"{self.name}: unknown rule key {k!r} in rule {rule.get('name')!r}")
        return True

    # ---------------------------------------------------------------- damage
    def damaged(self, r):
        d = r.get("damage")
        return bool(d) and not (isinstance(d, str) and set(filter(None, d.split(", "))) <= self.IGNORED_DAMAGE)

    def severity(self, r):
        """None, 'light' or 'heavy'. The AI's verdict (it also sees the photos) wins when it has one."""
        a = (r.get("ai") or {}).get("damage")
        if a in ("light", "heavy"):
            return a
        return self.classify_damage(r) if self.damaged(r) else None

    def classify_damage(self, r):
        return "heavy"

    def damage_en(self, r, sev, neg):
        """English damage text: the AI's summary when it has one, else the non-negated faults found in the listing,
        translated via DAMAGE_EN (only light ones for light damage). neg = negation regex ("geen krassen")."""
        d = ((r.get("ai") or {}).get("defects") or "").strip()
        if d and not NEG_DEFECTS.match(d):
            return d[:70] + ("…" if len(d) > 70 else "")
        text = f"{r.get('title') or ''}. {r.get('desc') or ''}"
        found = []
        for rx, en, lvl in self.DAMAGE_EN:
            if sev == "light" and lvl != "light":
                continue
            for m in rx.finditer(text):
                if not neg.search(text[max(0, m.start() - 35):m.start()]):
                    if en not in found:
                        found.append(en)
                    break
        if sev == "heavy":  # lead with the serious faults
            heavy = {e for _, e, lv in self.DAMAGE_EN if lv == "heavy"}
            found.sort(key=lambda e: e not in heavy)
        return ", ".join(found[:3])

    # ---------------------------------------------------------------- value model (prices.py)
    def new_price(self, r):
        """What an equivalent product costs new today."""
        return r.get("new_price") or self.value.get("new_price")

    def year(self, r):
        y = r.get("year") or (r.get("ai") or {}).get("year")
        import prices
        if isinstance(y, (int, float)) and 1995 < y <= prices.NOW:
            return int(y)
        return self.value.get("year_default", prices.NOW - 4)

    @property
    def age_rate(self):
        return self.value.get("age_rate", 0.92)

    @property
    def age_floor(self):
        return self.value.get("age_floor", 0.25)

    def usable_ref(self, r):
        """Whether a listing counts for the percentile reference (real product, with a price, not damaged)."""
        p = r.get("price")
        return bool(p and p >= 1 and not r.get("accessory") and (r.get("ptype") or "") not in ("SEE_DESCRIPTION", "RESERVED")
                    and not self.damaged(r))

    def sample_files(self):
        return sorted(p for p in hunt.DATA.glob(f"{self.name}_*.json") if p.stem[len(self.name) + 1:][:1].isdigit())

    def ref_rows(self):
        """Listings for `prices.py build`: the last sample/sweep file data/<name>_<date>.json."""
        f = self.sample_files()
        return [self.parse(r) for r in hunt.load(f[-1], [])] if f else []

    def test_rows(self):
        """Listings for `alert.py test` (no network)."""
        return self.ref_rows()

    # ---------------------------------------------------------------- push layout (alert.finish)
    def facts(self, r):
        a = r.get("ai") or {}
        return {"brand": (r.get("brand") or "").title(), "year": a.get("year") or r.get("year"), "guess": False}

    def headline_bits(self, f, r):
        """[(text, priority)] for the title after 'P93 · €150 · '; higher priority numbers are dropped first."""
        return [(f["brand"] or self.label, 0)]

    def spec_bits(self, f, r):
        return [str(f["year"] or "")]

    def quality_dot(self, f, r):
        return ORANGE

    def summary_cols(self, r):
        """[(header, value)] for the GitHub run summary table."""
        return []

    def sample(self):
        """A made-up listing for `alert.py ping` (None = no test push for this kind)."""
        return None

    # ---------------------------------------------------------------- AI (ai.py)
    def ai_system(self):
        fields = self.cfg.get("ai_fields") or '"year": int|null'
        return ("You check second-hand listings from Marktplaats (Netherlands) for a buyer. Identify the exact product "
                "model from the title, description and photos. If it is known or can be narrowed down, search the web "
                "for its specs and its current new price in the Netherlands/EU (or its last price when sold new). "
                "Answer with ONE JSON object and nothing else, with these keys:\n"
                '{"model": str|null, ' + fields + ', "new_price_eur": number|null, "confidence": "alta"|"media"|"baja", '
                '"defects": str|null, "damage": "none"|"light"|"heavy", "note": str}\n'
                "defects = any damage or fault the seller mentions or the photos show, in English, short; null when "
                "there is none. " + self.cfg.get("ai_damage", "damage = light for small faults a buyer can live with, "
                                                                "heavy when it needs repair or does not work. ") +
                "note = one short sentence in English for the buyer (max 90 characters): what it is and whether the "
                "price is good.")

    def ai_apply_extra(self, r, a):
        return r
