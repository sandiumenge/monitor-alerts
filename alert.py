"""Marketplace alerts: light Marktplaats check of every kind in kinds/ (monitors, TVs, …) + their rules ->
phone push (ntfy, one topic per kind) and Windows toast.

  python -I alert.py run          one check (what the scheduled task runs): newest page of each kind per center,
                                  rules, full-description check, AI second opinion, push
  python -I alert.py test         evaluate the rules of every kind against stored listings (no network, no push)
  python -I alert.py toast        send a sample notification to check that toasts work
  python -I alert.py ping         send one sample phone push per kind (built by the real formatter)
  python -I alert.py seed         mark every current match as already notified (no pushes)
  python -I alert.py log          last 30 notified listings

What each kind searches, its rules and how it is valued and shown live in kinds/<name>.json (+ kinds/<name>.py);
alerts.json holds the settings shared by all kinds (AI budget, Utrecht factor, damage handling, quiet hours).

Anti-ban: 2 calls per kind per run (one per center), 3-6 s apart; quiet hours skip the night; any HTTP error
(403/429/5xx) pauses all checks for an hour (doubling up to 8 h while errors persist).
"""
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import hunt  # noqa: E402
import ai  # noqa: E402
import kinds  # noqa: E402
import notify  # noqa: E402
import prices  # noqa: E402

ROOT = Path(__file__).resolve().parent
RULES_P = ROOT / "alerts.json"
STATE_P = hunt.DATA / "alert_state.json"
LOG_P = hunt.DATA / "alerts.log"
KINDS = kinds.all_kinds()  # {name: Kind}


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def log(msg):
    with open(LOG_P, "a", encoding="utf-8") as f:
        f.write(f"{now()} {msg}\n")


def conf():
    cfg = json.loads(RULES_P.read_text(encoding="utf-8"))
    topic_f = hunt.DATA / "ntfy_topic.txt"  # kept out of git; in GitHub Actions it comes from the NTFY_TOPIC secret
    cfg["ntfy_topic"] = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or \
        (topic_f.read_text(encoding="utf-8").strip() if topic_f.exists() else "")
    # one topic per kind (own thread in the ntfy app): "monitores-<id>" -> "tv-<id>", … unless NTFY_TOPIC_<NAME> is set
    first = next(iter(KINDS.values()), None)
    cfg["topics"] = {k.name: os.environ.get(f"NTFY_TOPIC_{k.name.upper()}") or k.topic(cfg["ntfy_topic"], k is first)
                     for k in KINDS.values()}
    global WARN_DAMAGE
    WARN_DAMAGE = cfg.get("damage", "warn") == "warn"
    return cfg


PRICE_REF = {}  # prices.py reference + listings seen in the cloud; set in cmd_run / cmd_test
WARN_DAMAGE = True  # alerts.json "damage": "warn" = damaged listings still notify, flagged ⚠️; "skip" = never


# ---------------------------------------------------------------- push layout
POINTS = prices.POINTS  # personal points (ALERT_POINTS secret / data/points.json): distances and trip cost
GREEN, ORANGE, RED = kinds.base.GREEN, kinds.base.ORANGE, kinds.base.RED
PRIMARY = json.loads(RULES_P.read_text(encoding="utf-8")).get("primary_center", "Amsterdam")


def fmt_km(k):
    return f"{k:.1f}" if k < 10 else f"{k:.0f}"


def where(r):
    """'🟠 Zaandam · 9.6 km' or '🟢 Amsterdam · 1.4 km from work 💼': distance to the nearest personal point only
    (🟢 < 3 km, 🟠 < 6 km, 🔴 further; shorter around Utrecht: dist_dot);
    Marktplaats' distance to the search center when the listing has no coordinates."""
    city = r.get("city") or "?"
    k, p = prices.nearest(r)
    if p is None:
        return f"{dist_dot(k, r)} {city} · {k} km"
    return f"{dist_dot(k, r)} {city} · {fmt_km(k)} km{' from work 💼' if p.get('work') else ''}"


def dist_dot(km, r=None):
    """🟢 < 3 km, 🟠 < 6 km, 🔴 further around Amsterdam; around another center the limits shrink with its search
    radius (Utrecht 6 km / Amsterdam 15 km = ×0.4 → 🟢 < 1.2 km, 🟠 < 2.4 km)."""
    s = 1.0
    near = ((r or {}).get("near") or "").split(",")
    primary = PRIMARY
    if near[0] and primary not in near:
        s = hunt.radius_m(near[0]) / hunt.radius_m(primary)
    return GREEN if km < 3 * s else ORANGE if km < 6 * s else RED


def price_dot(p, fair_v):
    """🟢 at most 60 % of the estimated value, 🟠 up to 90 %, 🔴 above."""
    if p is None or not fair_v:
        return ORANGE
    q = p / fair_v
    return GREEN if q <= 0.6 else ORANGE if q <= 0.9 else RED


LINE = 36  # characters that fit on one line of the ntfy notification on the phone
TITLE_W = 35  # columns after the icon the app adds; the title is bold, so fewer fit: measured on the phone,
#               '…QLED ·' (37 columns) still fit but the ⛔ after it (39) wrapped to a second line


def width(s):
    """Columns a string takes on the phone: emoji and symbols like ⚠️ ⛔ count double, the invisible emoji
    variation selector counts zero."""
    return sum(0 if c in "️‍" else 2 if ord(c) >= 0x1F000 or c in "⚠⛔❓✅⭐⚡" else 1 for c in s)


def headline(f, K, r, room):
    """Short title part from the kind's headline_bits (brand + the facts that matter most), dropping the least
    important ones to fit `room` columns ('LG 55" 4K NanoCell Smart', 'MSI 34" ultrawide USB‑C')."""
    bits = [(x, k) for x, k in K.headline_bits(f, r) if x]
    while width(" ".join(x for x, _ in bits)) > room and any(k for _, k in bits):
        worst = max(k for _, k in bits)
        bits = [(x, k) for x, k in bits if k != worst]
    return " ".join(x for x, _ in bits)[:room]


def spec_line(f, K, r):
    """'🟢 34" · 3440×1440 (UWQHD) · 100Hz · VA · USB‑C 65W · curved' / '🟢 55" · 4K · OLED · Smart · 2018'
    (may wrap to a second line on the phone)."""
    return K.quality_dot(f, r) + " " + " · ".join(x for x in K.spec_bits(f, r) if x)


def price_line(r, K, sev):
    """'🟢 Worth ~€241 · New €400 (−88%)', always one phone line: the estimated second-hand value ('~': our own
    estimate, not a real price), then the real new price (models.json / the AI / the new-today estimate) and how far
    the asking price is below it. The discount is dropped if the line would still not fit."""
    p = r.get("price")
    new = r.get("new_price") or prices.new_of(r, K)
    f = prices.fair(r, K, sev)
    parts = [f"Worth ~€{f:.0f}"] if f else []
    if new:
        d = round((1 - p / new) * 100) if p else None
        parts.append(f"New €{new:.0f}" + (f" (−{d}%)" if d and d > 0 else ""))
    if not parts:
        return ""
    out = price_dot(prices.eff_price(r), f) + " " + " · ".join(parts)
    return out if width(out) <= LINE else out.split(" (−")[0]


def bids(r):
    """Only when somebody already bid (rare: listings are checked within 10 minutes of being posted)."""
    if not (r.get("n_bids") and r.get("top_bid")):
        return ""
    n = r["n_bids"]
    return f"{n} bid{'s' if n > 1 else ''} already · highest €{r['top_bid']:.0f}"


def damage_line(r, K, sev):
    """'⚠️ Light damage: dead pixels ⚠️' / '⛔ Badly broken: cracked screen ⛔', always one phone line."""
    en = K.damage_en(r, sev, db.NEG) or "check the listing"
    sym, lab = ("⛔", "Badly broken") if sev == "heavy" else ("⚠️", "Light damage")
    room = LINE - width(f"{sym} {lab}:  {sym}")  # one phone line, so the closing symbol never wraps alone
    while width(en) > room and ", " in en:  # drop the least important faults first
        en = en.rsplit(", ", 1)[0]
    if width(en) > room:
        en = en[:room - 1].rstrip(" ,") + "…"
    return f"{sym} {lab}: {en} {sym}"


def finish(r, tag, K):
    """Title 'P93 · €150 · LG 55" 4K NanoCell Smart [❓]' (the app adds the kind's icon; damage ⚠️/⛔ goes only
    on the line right under it), then one line each: damage (only if any), bids (only if any), specs, price,
    distance, and the AI / not-sure notes."""
    K = kinds.get(K)
    sev = K.severity(r)
    f = K.facts(r)
    p = r.get("price")
    price = f"€{p:.0f}" if p else "free" if r.get("ptype") == "FREE" else "no price"
    head = f"{prices.tag(PRICE_REF, r, K, sev) or 'P?'} · {price} · "
    room = TITLE_W - width(head) - (3 if tag else 0)  # ' ❓'
    title = head + headline(f, K, r, room)
    dmg = damage_line(r, K, sev) if sev else ""
    if tag:
        title += " ❓"
    a = ai.line(r)
    if tag and not r.get("ai"):
        a = K.UNSURE
    elif tag:
        a = (a + "\n" if a else "") + "❓ The AI is not fully sure: have a look"
    lines = [dmg, bids(r), spec_line(f, K, r), price_line(r, K, sev), where(r), a]
    return title, "\n".join(x for x in lines if x)


# ---------------------------------------------------------------- checks before a push
def heavy_gate(cfg, r, K):
    """Heavily damaged listings only notify when they are still an exceptional deal at 10 % of the fair value."""
    if K.severity(r) != "heavy":
        return True
    pc = prices.percentile(PRICE_REF, r, K, "heavy")
    return pc is None or pc >= cfg.get("heavy_min_percentile", 70)


def gate(cfg, s, hits, K):
    out = []
    for ru, r, tag in hits:
        if heavy_gate(cfg, r, K):
            out.append((ru, r, tag))
        else:
            s["alerted"][r["id"]] = {"at": now(), "skipped": "heavy damage, below heavy_min_percentile", "rule": ru["name"]}
            log(f"SKIP heavy P<{cfg.get('heavy_min_percentile', 70)} [{ru['name']}] {r['id']} {r.get('title')!r}")
            print(f"  SKIP heavy (P too low) [{ru['name']}] {r.get('title')!r}")
    return out


LOWBALL_MAX = 2.0  # look at "bieden vanaf" listings asking up to 2 × a rule's price limit
LOWBALL_EST = 1.5  # realistic price = min(1.5 × highest bid, or 1.5 × minimum bid if none, asking price)


def lowball(cfg, s, pool, hit_ids, K, limit=3):
    """Listings asking more than the rule allows but taking bids, whose realistic price still fits it:
    min(1.5 × highest bid (or minimum bid when nobody bid), asking price) strictly under the limit. E.g. €110 limit:
    "Vraagprijs €175, bieden vanaf €60" → ~€90 notifies; "bieden vanaf €100" → ~€150 does not. The minimum bid is only
    on the listing page, so at most `limit` new candidates per run are opened (a few seconds apart), each once."""
    def rule_of(r):
        return K.first_rule(cfg, r)
    done = s.setdefault("lowball_checked", {})
    cand = [r for r in pool if r.get("ptype") == "MIN_BID" and r.get("price") and r["id"] not in s["alerted"]
            and r["id"] not in done and r["id"] not in hit_ids and not rule_of(r)
            and rule_of(dict(r, price=r["price"] / LOWBALL_MAX))]
    out = []
    for i, r in enumerate(cand[:limit]):
        time.sleep(random.uniform(3, 5))
        done[r["id"]] = now()
        try:
            d = db.fetch_details(r["url"])
        except Exception as e:  # noqa: BLE001
            if "404" in str(e) or "410" in str(e):
                continue
            print(f"  lowball check stopped: {str(e)[:80]}")  # 403/429/timeouts: no more page loads this run
            break
        mb = d.get("min_bid")
        if not mb or mb >= r["price"]:
            continue
        # what it will realistically sell for: 1.5 × the highest bid (or the minimum bid if nobody bid), at most the ask
        est = min(LOWBALL_EST * (d.get("top_bid") or mb), r["price"])
        r2 = dict(r, desc=d["desc"] or r.get("desc"), n_bids=d["n_bids"], top_bid=d["top_bid"], views=d["views"],
                  imgs=r.get("imgs") or d.get("imgs"), ask=r["price"], min_bid=mb, price=est)
        r2 = K.parse(r2)
        ru = rule_of(r2)
        if ru and est < ru.get("max_price", float("inf")) * K.price_factor(cfg, r2):  # strictly under the limit
            out.append((ru, r2))
            print(f"  LOWBALL [{ru['name']}] pide €{r['price']:.0f}, puja mín €{mb:.0f}, estimado €{est:.0f}: {r['title'][:60]}")
        else:
            print(f"  lowball no: pide €{r['price']:.0f}, puja mín €{mb:.0f}, estimado €{est:.0f} · {r['title'][:50]}")
    for k in [k for k, v in done.items() if v < (dt.datetime.now() - dt.timedelta(days=30)).strftime("%Y-%m-%d")]:
        done.pop(k)
    return out


def verify_hits(cfg, s, hits, K):
    """Before notifying, read the full listing: the search API only returns the first ~250 characters of the
    description, and defects ("valt donkerder uit", "krassen") are often further down. A hit whose full text
    no longer passes its rules is marked as handled without a push. A failed fetch keeps the hit (better a push
    than a miss); a 404/410 means it is already gone."""
    out = []
    for i, (ru, r) in enumerate(hits):
        if i:
            time.sleep(random.uniform(2, 4))
        try:
            d = db.fetch_details(r["url"])
        except Exception as e:  # noqa: BLE001
            if "404" in str(e) or "410" in str(e):
                s["alerted"][r["id"]] = {"at": now(), "skipped": "gone"}
                continue
            out.append((ru, r))
            continue
        r = K.parse(dict(r, desc=d["desc"] or r.get("desc"), n_bids=d["n_bids"], top_bid=d["top_bid"],
                         views=d["views"], imgs=r.get("imgs") or d.get("imgs")))
        ru2 = K.first_rule(cfg, r)
        if ru2 or (WARN_DAMAGE and K.severity(r) != "heavy"):  # warn mode: keep it, the push shows the damage
            out.append((ru2 or ru, r))
        else:
            s["alerted"][r["id"]] = {"at": now(), "skipped": "full description fails the rules", "rule": ru["name"]}
            log(f"SKIP [{ru['name']}] {r['id']} {r.get('title')!r}: full description fails ({r.get('damage')})")
            print(f"  SKIP [{ru['name']}] {r.get('title')!r}: {r.get('damage')} · {r['url']}")
    return out


# ---------------------------------------------------------------- AI second opinion (ai.py, DeepSeek)
def ai_budget(cfg, s):
    a = cfg.get("ai") or {}
    if not a.get("enabled", True) or not ai.key():
        return 0
    day = dt.date.today().isoformat()
    d = s.setdefault("ai_day", {})
    if d.get("date") != day:
        d.clear()
        d.update(date=day, n=0)
    return max(0, min(a.get("max_per_run", 6), a.get("max_per_day", 150) - d["n"]))


def ai_pass(cfg, s, hits, unsure, K):
    """Ask the AI about every hit and every 'not sure' listing (hits first, within the budget). Returns the list of
    (rule, listing, tag) to notify: tag '' = rule confirmed, '❓' = still not sure (notified anyway, the user prefers
    one push too many over a miss). 'Not sure' listings that the AI shows do not fit are dropped quietly."""
    todo = [("hit", ru, r) for ru, r in hits] + [("unsure", ru, r) for ru, r in unsure]
    n = ai_budget(cfg, s)
    asked = todo[:n]
    answers = []
    if asked:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            answers = list(ex.map(lambda x: ai.ask(x[2], K), asked))
        s["ai_day"]["n"] += len(asked)
        s.setdefault("today", {})["ai"] = s.get("today", {}).get("ai", 0) + len(asked)
    out = []
    for i, (what, ru, r) in enumerate(todo):
        a = answers[i] if i < len(answers) else None
        if what == "unsure" and i >= len(asked):
            continue  # over budget: stays unchecked, retried next run
        r2 = ai.apply(r, a, K)
        if a:
            u = a.get("usage") or {}
            print(f"  AI {r['id']} {what}: {a.get('model')} {a.get('res')} new €{a.get('new_price_eur')} "
                  f"({a.get('confidence')}) · tokens in {u.get('prompt_tokens')} out {u.get('completion_tokens')}")
        ru2 = K.first_rule(cfg, r2)
        if what == "hit":
            if not ru2 and K.severity(r2) == "heavy":  # the AI found heavy damage: must cost ≤ 50 % of the limit
                s["alerted"][r["id"]] = {"at": now(), "skipped": "heavy damage over the 50 % limit", "rule": ru["name"]}
                log(f"SKIP heavy [{ru['name']}] {r['id']} {r.get('title')!r}: {(a or {}).get('defects')}")
                continue
            out.append((ru2 or ru, r2, "" if ru2 or not a else "❓"))
            continue
        s.setdefault("ai_checked", {})[r["id"]] = {"at": now(), "ok": bool(ru2), "ai": bool(a)}
        if ru2:
            out.append((ru2, r2, ""))
        elif not a or K.maybe_rule(cfg, r2):  # AI failed or still could not tell
            out.append((ru, r2, "❓"))
        else:
            log(f"AI-NO [{ru['name']}] {r['id']} {r.get('title')!r}: {a.get('model')} {a.get('res')} {a.get('inch')}\"")
    return out


# ---------------------------------------------------------------- state
def load_state():
    s = hunt.load(STATE_P, {"alerted": {}, "pause_until": None, "backoff_h": 1, "seeded": False})
    # before kinds/ the monitor counters had no prefix (TVs already used tv_…)
    if "seen_ids" in s:
        s.setdefault("monitor_seen_ids", s.pop("seen_ids"))
    d = s.get("today") or {}
    for a, b in (("new", "monitor_new"), ("alerts", "monitor_alerts")):
        if a in d:
            d.setdefault(b, d.pop(a))
    return s


def save_state(s):
    hunt.save(STATE_P, s)


def seed(s, cfg, pools):
    """pools: [(Kind, listings)]."""
    n = 0
    for K, rows in pools:
        for r in rows:
            ru = K.first_rule(cfg, r)
            if ru and r["id"] not in s["alerted"]:
                s["alerted"][r["id"]] = {"at": now(), "kind": K.name, "rule": ru["name"], "price": r.get("price"),
                                         "seeded": True}
                n += 1
    s["seeded"] = True
    return n


# ---------------------------------------------------------------- commands
def check(cfg, s, K, pool):
    """Rules → full description → 'not sure' listings → bid listings → AI → heavy-damage gate, for one kind."""
    rules = K.cfg.get("rules", [])
    hits = [(ru, r) for r in pool if r["id"] not in s["alerted"] and (ru := K.first_rule(cfg, r))]
    hits.sort(key=lambda h: rules.index(h[0]))
    hits = verify_hits(cfg, s, hits, K)
    checked = s.setdefault("ai_checked", {})
    hit_ids = {x["id"] for _, x in hits}
    unsure = [(ru, r) for r in pool if r["id"] not in s["alerted"] and r["id"] not in checked
              and r["id"] not in hit_ids and not K.first_rule(cfg, r) and (ru := K.maybe_rule(cfg, r))]
    n_ai = ai_budget(cfg, s) - len(hits)
    unsure = verify_hits(cfg, s, unsure[:n_ai], K) if n_ai > 0 else []
    keep_u = []
    for ru, r in unsure:  # the full description already settles it ("24 inch"): no AI call needed
        if K.first_rule(cfg, r) or K.maybe_rule(cfg, r):
            keep_u.append((ru, r))
        else:
            checked[r["id"]] = {"at": now(), "ok": False, "ai": False}
    if cfg.get("lowball"):
        hits += lowball(cfg, s, pool, hit_ids | {x["id"] for _, x in keep_u}, K)
    return gate(cfg, s, ai_pass(cfg, s, hits, keep_u, K), K)


def cmd_run(args):
    cfg, s = conf(), load_state()
    ks = kinds.enabled()
    cloud = "--cloud" in args or os.environ.get("GITHUB_ACTIONS") == "true"
    print(f"distance points: {len(POINTS)} ({sum(1 for p in POINTS if p.get('work'))} work)")  # no coordinates logged
    t = dt.datetime.now()
    q0, q1 = cfg.get("quiet_hours", [1, 7])
    if "--force" not in args:
        if not cloud and q0 <= t.hour < q1:  # in the cloud the cron schedule handles the night
            return print("quiet hours")
        if s.get("pause_until") and t.isoformat() < s["pause_until"]:
            return print(f"paused until {s['pause_until']} after an HTTP error")
    try:
        raw = {K.name: K.fetch() for K in ks}
    except Exception as e:  # noqa: BLE001  (403/429/5xx/timeouts: back off, never hammer)
        h = s.get("backoff_h", 1)
        s["pause_until"] = (t + dt.timedelta(hours=h)).isoformat(timespec="minutes")
        s["backoff_h"] = min(h * 2, 8)
        save_state(s)
        log(f"ERROR {e!r}; pausing {h} h")
        return print(f"error: {e!r}; paused {h} h")
    s["pause_until"], s["backoff_h"] = None, 1
    global PRICE_REF
    s.pop("price_hist", None)  # old formats ([price, new] pairs)
    s.pop("value_hist", None)
    hist = s.setdefault("value_ref", {})
    parsed = {K.name: [K.parse(r) for r in raw[K.name]] for K in ks}
    for K in ks:  # every listing seen grows the reference for the percentile
        for r in parsed[K.name]:
            prices.add(hist, r, K)
    for g in hist:  # keep the state small: newest 5000 per kind
        if len(hist[g]) > 5000:
            hist[g] = dict(list(hist[g].items())[-5000:])
    PRICE_REF = prices.load(hist)
    runs = []  # (Kind, pool, new listings, number new, notified)
    for K in ks:
        pool, new_rows, n_new = K.pool(raw[K.name], parsed[K.name], cloud, s)
        runs.append([K, pool, new_rows, n_new, []])
    if not s.get("seeded"):
        n = seed(s, cfg, [(K, pool) for K, pool, *_ in runs])
        save_state(s)
        log(f"seeded {n} existing matches (no notification)")
        return print(f"first run: {n} current matches marked as seen; from now on only new ones notify")
    shown = 0
    for run in runs:
        K, pool = run[0], run[1]
        hits = run[4] = check(cfg, s, K, pool)
        topic = cfg["topics"].get(K.name)
        for ru, r, tag in hits:
            title, body = finish(r, tag, K)
            if shown < cfg.get("max_toasts", 4):
                notify.toast(title, body, r["url"])
                shown += 1
            notify.ntfy(topic, title, body, r["url"], tag=K.icon)
            s["alerted"][r["id"]] = {"at": now(), "kind": K.name, "rule": ru["name"], "price": r.get("price"),
                                     "unsure": bool(tag)}
            log(f"ALERT {K.name}{' ' + tag if tag else ''} [{ru['name']}] {r['id']} €{r.get('price')} "
                f"{r.get('model') or r['title']!r} {r.get('city')} {r['url']}")
    n_hits = sum(len(x[4]) for x in runs)
    if n_hits > shown:
        notify.toast(f"🔔 +{n_hits - shown} more alerts", "python -I alert.py log", "")
    if n_hits:
        notify.sound()
    if cloud:
        day = dt.date.today().isoformat()
        d = s.setdefault("today", {})
        if d.get("date") != day:
            d.clear()
            d.update(date=day, checks=0)
        d["checks"] = d.get("checks", 0) + 1
        for K, pool, new_rows, n_new, hits in runs:
            d[f"{K.name}_new"] = d.get(f"{K.name}_new", 0) + n_new
            d[f"{K.name}_alerts"] = d.get(f"{K.name}_alerts", 0) + len(hits)
        if t.hour >= cfg.get("heartbeat_hour", 21) and s.get("heartbeat") != day and cfg.get("heartbeat", True) and ks:
            per = " · ".join(f"{K.plural}: {d.get(K.name + '_new', 0)} new, {d.get(K.name + '_alerts', 0)} alerts"
                             for K in ks)
            notify.ntfy(cfg["topics"].get(ks[0].name), "✅ " + " and ".join(K.label for K in ks) + " alerts running",
                        f"Today: {d['checks']} checks · {per} · AI: {d.get('ai', 0)} lookups.", "", priority=2, tag=None)
            s["heartbeat"] = day
        summary(cfg, runs, s)
    save_state(s)
    print(f"{now()}: " + " · ".join(f"{K.plural}: {len(pool)} newest ({n_new} new), {len(hits)} alerts"
                                    for K, pool, _, n_new, hits in runs))
    for K, *_, hits in runs:
        for ru, r, _ in hits:
            print(f"  ALERT {K.name} [{ru['name']}] €{r.get('price')} {(r.get('model') or r.get('title') or '')[:60]} · "
                  f"{r.get('city')} · {r['url']}")


def summary(cfg, runs, s):
    """Markdown summary shown on the GitHub Actions run page: per kind, the new listings and what happened."""
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if not out:
        return
    L = [f"## {now()}", ""]
    for K, pool, new_rows, n_new, hits in runs:
        hit = {r["id"]: ru["name"] for ru, r, *_ in hits}
        L += [f"### {K.plural[:1].upper() + K.plural[1:]} · {len(pool)} revisados · {n_new} nuevos desde la revisión anterior · {len(hits)} alertas", ""]
        shown = [r for r in new_rows if K.prefilter(cfg, r, True) or r["id"] in hit]
        if shown:
            cols = [c for c, _ in K.summary_cols(shown[0])]
            L += ["| Anuncio | Precio | " + "".join(f"{c} | " for c in cols) + "Dónde | Resultado |",
                  "|---|---|" + "---|" * len(cols) + "---|---|"]
            for r in shown[:40]:
                ru = K.first_rule(cfg, r)
                res = f"🔔 **{hit[r['id']]}**" if r["id"] in hit else (f"ya avisado ({ru['name']})" if ru else
                                                                      "dañado/accesorio" if r.get("damage") or r.get("accessory") else "no cumple reglas")
                title = (r.get("model") or r.get("title") or "")[:60].replace("|", "/")
                price = "€%.0f" % r["price"] if r.get("price") else "—"
                L.append(f"| [{title}]({r['url']}) | {price} | " + "".join(f"{v} | " for _, v in K.summary_cols(r)) +
                         f"{r.get('city') or '?'} {r.get('km') or 0} km | {res} |")
        match_now = [(ru, r) for r in pool if (ru := K.first_rule(cfg, r))]
        L += ["", f"**Ahora mismo cumplen alguna regla {len(match_now)} de los {len(pool)} anuncios:**", ""]
        L += [f"- [{(r.get('model') or r['title'])[:60]}]({r['url']}) · €{r.get('price') or 0:.0f} · {ru['name']}"
              for ru, r in match_now[:15]]
        L.append("")
    d = s.get("today", {})
    L += [f"Hoy: {d.get('checks', 0)} revisiones · " +
          " · ".join(f"{K.plural}: {d.get(K.name + '_new', 0)} nuevos, {d.get(K.name + '_alerts', 0)} alertas"
                     for K, *_ in runs) + "."]
    with open(out, "a", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def cmd_test(args):
    cfg = conf()
    global PRICE_REF
    PRICE_REF = prices.load()
    for K in kinds.enabled():
        rows = K.test_rows()
        rules = K.cfg.get("rules", [])
        by = {ru["name"]: [] for ru in rules}
        for r in rows:
            ru = K.first_rule(cfg, r)
            if ru:
                by[ru["name"]].append(r)
        print(f"\n# {K.plural}: {len(rows)} listings")
        for name, rs in by.items():
            print(f"\n## {name}: {len(rs)} would match")
            for r in sorted(rs, key=lambda r: r.get("price") or 0)[:6]:
                t, b = finish(r, "", K)
                seen = ", ".join(str(x) for x in (r.get("first_seen") or r.get("date"), r.get("near")) if x)
                print(f"  €{r.get('price') or 0:>5.0f}  {b.replace(chr(10), ' | ')}  ({seen})  {r['url']}")


def cmd_toast(args):
    t, b, u = "🔔 Prueba · €50", "Iiyama XUB3493WQSU-B1\n34\" UWQHD 75Hz · Utrecht 3 km", "https://www.marktplaats.nl"
    ok = notify.toast(t, b, u)
    notify.sound()
    print("toast sent" if ok else "toast FAILED")
    topic = conf().get("ntfy_topic")
    if topic:
        print("phone push sent" if notify.ntfy(topic, t, b, u) else "phone push FAILED")


def cmd_ping(args):
    """One TEST push per kind built by the real formatter (finish), so it shows exactly what a real alert looks like."""
    cfg = conf()
    ok = True
    for K in kinds.enabled():
        r = K.sample()
        if not r:
            continue
        t, b = finish(r, "", K)
        print(t, b, "", sep="\n")
        ok = notify.ntfy(cfg["topics"].get(K.name), t, b + "\n(TEST sample, not a real listing)",
                         "https://www.marktplaats.nl", tag=K.icon) and ok
    print("phone push sent" if ok else "phone push FAILED (is NTFY_TOPIC set?)")


def cmd_seed(args):
    s, cfg = load_state(), conf()
    print(f"{seed(s, cfg, [(K, K.test_rows()) for K in kinds.enabled()])} matches marked as seen")
    save_state(s)


def cmd_log(args):
    lines = LOG_P.read_text(encoding="utf-8").splitlines() if LOG_P.exists() else []
    print("\n".join(lines[-30:]) or "no alerts yet")


if __name__ == "__main__":
    if sys.stdout:  # None under pythonw (scheduled task)
        sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    try:
        {"run": cmd_run, "test": cmd_test, "toast": cmd_toast, "ping": cmd_ping, "seed": cmd_seed, "log": cmd_log}[cmd](sys.argv[2:])
    except Exception:  # noqa: BLE001  (scheduled runs have no console: keep the traceback)
        import traceback
        log("CRASH " + traceback.format_exc().replace("\n", " | "))
        raise
