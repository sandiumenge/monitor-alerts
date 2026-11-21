"""Monitor alerts: light Marktplaats check + rules from alerts.json -> Windows toast (and optional phone push).

  python -I alert.py run          one check (what the scheduled task runs): newest page of "Monitoren" per center
                                  (2 API calls), upsert into monitors.db, notify listings that match a rule
  python -I alert.py test         evaluate the rules against every active listing in the DB (no network, no toast)
  python -I alert.py toast        send a sample notification to check that toasts work
  python -I alert.py ping         send a sample phone push only
  python -I alert.py seed         mark every current match as already notified (no toasts)
  python -I alert.py log          last 30 notified listings

Anti-ban: 2 calls per run, 3-6 s apart; quiet hours skip the night; any HTTP error (403/429/5xx) pauses
all checks for an hour (doubling up to 8 h while errors persist).
"""
import datetime as dt
import json
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import hunt  # noqa: E402
import ai  # noqa: E402
import notify  # noqa: E402
import tv  # noqa: E402

ROOT = Path(__file__).resolve().parent
RULES_P = ROOT / "alerts.json"
STATE_P = hunt.DATA / "alert_state.json"
LOG_P = hunt.DATA / "alerts.log"
IGNORED_DAMAGE = {"sin cable/fuente"}


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def log(msg):
    with open(LOG_P, "a", encoding="utf-8") as f:
        f.write(f"{now()} {msg}\n")


def conf():
    cfg = json.loads(RULES_P.read_text(encoding="utf-8"))
    topic_f = hunt.DATA / "ntfy_topic.txt"  # kept out of git; in GitHub Actions it comes from the NTFY_TOPIC secret
    cfg["ntfy_topic"] = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or         (topic_f.read_text(encoding="utf-8").strip() if topic_f.exists() else "")
    global WARN_DAMAGE
    WARN_DAMAGE = cfg.get("damage", "warn") == "warn"
    return cfg


WARN_DAMAGE = True  # alerts.json "damage": "warn" = damaged listings still notify, flagged ⚠️; "skip" = never


def defect_note(r):
    """Short quote of the first non-negated defect phrase in the title/description, or ''."""
    text = f"{r.get('title') or ''}. {r.get('desc') or ''}"
    rxs = [tv.DAMAGE_RX] + [re.compile(x[0], re.I) for x in db.DAMAGE if len(x) > 2 and x[2]]
    for rx in rxs:
        for m in rx.finditer(text):
            if db.NEG.search(text[max(0, m.start() - 35):m.start()]):
                continue
            a = max(text.rfind(".", 0, m.start()) + 1, m.start() - 50)
            if a and not text[a - 1].isspace() and text[a - 1] != ".":
                a = text.find(" ", a, m.start()) + 1 or a  # don't start mid-word
            b = text.find(".", m.end())
            b = min(b if b != -1 else len(text), m.end() + 60)
            return text[a:b].strip()
    return ""


# ---------------------------------------------------------------- rules
def match(rule, r, price_factor=1.0):
    text = f"{r.get('title') or ''} \n {r.get('desc') or ''} \n {r.get('model') or ''}"
    p = r.get("price")
    if "max_price" in rule:
        if p is None:
            if not rule.get("allow_no_price"):
                return False
        elif p > rule["max_price"] * price_factor:
            return False
    inch = r.get("inch")
    if "min_inch" in rule and not (inch and inch >= rule["min_inch"]):
        return False
    if "min_inch_if_known" in rule and inch and inch < rule["min_inch_if_known"]:
        return False
    if "max_inch" in rule and not (inch and inch <= rule["max_inch"]):
        return False
    if "res" in rule and r.get("res") not in rule["res"]:
        return False
    if "not_res" in rule and r.get("res") in rule["not_res"]:
        return False
    if "panel" in rule and (r.get("panel") or "") not in rule["panel"]:
        return False
    if "min_hz" in rule and not (r.get("hz") and r["hz"] >= rule["min_hz"]):
        return False
    if rule.get("usbc_charge") and not re.search(r"\d+W|yes", r.get("usbc") or ""):
        return False
    if "min_discount" in rule and not (r.get("discount") is not None and r["discount"] >= rule["min_discount"]):
        return False
    if "models" in rule and not re.search(rule["models"], text, re.I):
        return False
    if "any" in rule and not re.search(rule["any"], text, re.I):
        return False
    if "none" in rule and re.search(rule["none"], text, re.I):
        return False
    if rule.get("no_damage") and not WARN_DAMAGE and set(filter(None, (r.get("damage") or "").split(", "))) - IGNORED_DAMAGE:
        return False
    if "max_km" in rule and (r.get("km") or 0) > rule["max_km"]:
        return False
    if "not_panel" in rule and r.get("panel") in rule["not_panel"]:
        return False
    if rule.get("smart") and not r.get("smart"):
        return False
    return True


def tv_first_rule(cfg, r):
    """TV rules (alerts.json 'tv'): ≥ min_inch, never damaged / parts / wanted ads."""
    t = cfg.get("tv") or {}
    if (r.get("damage") and not WARN_DAMAGE) or r.get("accessory") or not (r.get("inch") and r["inch"] >= t.get("min_inch", 50)):
        return None
    if t.get("none") and re.search(t["none"], f"{r.get('title')} {r.get('desc') or ''}", re.I):
        return None
    factor = 1.0
    primary = cfg.get("primary_center")
    if primary and r.get("near") and primary not in r["near"]:
        factor = cfg.get("other_center_price_factor", 1.0)
    return next((ru for ru in t.get("rules", []) if match(ru, r, factor)), None)


def globally_ok(cfg, r):
    """Applies to every rule: drop listings with a known size under min_inch and accessory-only titles."""
    if r.get("inch") and r["inch"] < cfg.get("min_inch", 0):
        return False
    t = r.get("title") or ""
    if cfg.get("exclude_title") and re.search(cfg["exclude_title"], t, re.I) and \
            not re.search(cfg.get("exclude_title_unless", "$^"), t, re.I):
        return False
    return True


def first_rule(cfg, r):
    if not globally_ok(cfg, r):
        return None
    factor = 1.0
    primary = cfg.get("primary_center")
    if primary and r.get("near") and primary not in r["near"]:  # only found around the other center: must be a steal
        factor = cfg.get("other_center_price_factor", 1.0)
    return next((ru for ru in cfg["rules"] if match(ru, r, factor)), None)


def describe(rule, r, tag=""):
    price = f"€{r['price']:.0f}" if r.get("price") else "sin precio"
    title = f"{tag or '🔔'} {rule['name']} · {price}"
    spec = " ".join(x for x in [f"{r['inch']:g}\"" if r.get("inch") else "", r.get("res") or "",
                                f"{r['hz']}Hz" if r.get("hz") else "",
                                f"USB‑C {r['usbc']}" if re.search(r"\d+W", r.get("usbc") or "") else ""] if x)
    disc = f" · −{r['discount']}%" if r.get("discount") else ""
    body = f"{r.get('model') or (r.get('title') or '')[:60]}\n{spec}{disc} · {r.get('city') or '?'} {r.get('km') or 0} km{bids(r)}"
    return warn(title, with_ai(body, r, tag), r)


def with_ai(body, r, tag):
    a = ai.line(r)
    if tag and not a:
        a = "❓ Falta un dato clave (resolución/tamaño) y no se pudo comprobar"
    elif tag:
        a += "\n❓ La IA no lo confirma del todo: míralo"
    return f"{body}\n{a}" if a else body


def warn(title, body, r):
    d = r.get("damage")
    if not d or (isinstance(d, str) and set(filter(None, d.split(", "))) <= IGNORED_DAMAGE):
        return title, body
    note = defect_note(r) or (d if isinstance(d, str) else "posible defecto o no funciona")
    return (f"⚠️ {title}", f"{body}\n⚠️ «{note[:110]}»") if note else (title, body)


def describe_tv(rule, r, tag=""):
    price = f"€{r['price']:.0f}" if r.get("price") else "sin precio"
    spec = " ".join(x for x in [f"{r['inch']}\"", (r.get("res") or "") + ("?" if r.get("res_guess") else ""), r.get("panel") or "",
                                "Smart" if r.get("smart") else "", str(r["year"]) if r.get("year") else ""] if x)
    return warn(f"📺{tag} {rule['name']} · {price}",
                with_ai(f"{(r.get('title') or '')[:60]}\n{spec} · {r.get('city') or '?'} {r.get('km') or 0} km{bids(r)}", r, tag), r)


def bids(r):
    return f" · {r['n_bids']} pujas (máx €{r['top_bid']:.0f})" if r.get("n_bids") and r.get("top_bid") else ""


def verify_hits(cfg, s, hits, tv_kind):
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
        r = dict(r, desc=d["desc"] or r.get("desc"), n_bids=d["n_bids"], top_bid=d["top_bid"], views=d["views"],
                 imgs=r.get("imgs") or d.get("imgs"))
        if tv_kind:
            r = tv.parse(r)
            ru2 = tv_first_rule(cfg, r)
        else:
            r = db.enrich(r)
            ru2 = first_rule(cfg, r)
        if ru2 or WARN_DAMAGE:  # warn mode: never drop a hit, the push carries the ⚠️ note instead
            out.append((ru2 or ru, r))
        else:
            s["alerted"][r["id"]] = {"at": now(), "skipped": "full description fails the rules", "rule": ru["name"]}
            log(f"SKIP [{ru['name']}] {r['id']} {r.get('title')!r}: full description fails ({r.get('damage')})")
            print(f"  SKIP [{ru['name']}] {r.get('title')!r}: {r.get('damage')} · {r['url']}")
    return out


# ---------------------------------------------------------------- AI second opinion (ai.py, DeepSeek)
GUESS_INCH = {"UWQHD": 34, "5K2K": 34, "4K": 32, "QHD": 32}


def maybe_rule(cfg, r, tv_kind):
    """A rule the listing would match if a fact it does not state turned out favourable (TV resolution or Smart,
    monitor size or resolution). These are the 'not sure' listings the AI looks up."""
    if tv_kind:
        if r.get("panel") == "PLASMA" or "plasma" in (r.get("title") or "").lower():
            return None  # plasmas are never 4K
        v = dict(r)
        if not r.get("res") or r.get("res_guess"):
            v["res"] = "4K"
        if not r.get("smart") and not re.search(r"geen smart|niet smart", f"{r.get('title')} {r.get('desc')}", re.I):
            v["smart"] = True
        return tv_first_rule(cfg, v) if v != r else None
    if r.get("inch") and r["inch"] >= cfg.get("min_inch", 0) and not r.get("res"):
        for res in GUESS_INCH:
            if ru := first_rule(cfg, dict(r, res=res)):
                return ru
    if r.get("res") in GUESS_INCH and not r.get("inch"):
        return first_rule(cfg, dict(r, inch=GUESS_INCH[r["res"]]))
    return None


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


def ai_pass(cfg, s, hits, unsure, tv_kind):
    """Ask the AI about every hit and every 'not sure' listing (hits first, within the budget). Returns the list of
    (rule, listing, tag) to notify: tag '' = rule confirmed, '❓' = still not sure (notified anyway, the user prefers
    one push too many over a miss). 'Not sure' listings that the AI shows do not fit are dropped quietly."""
    kind = "tv" if tv_kind else "monitor"
    rule_of = (lambda r: tv_first_rule(cfg, r)) if tv_kind else (lambda r: first_rule(cfg, r))
    todo = [("hit", ru, r) for ru, r in hits] + [("unsure", ru, r) for ru, r in unsure]
    n = ai_budget(cfg, s)
    asked = todo[:n]
    answers = []
    if asked:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            answers = list(ex.map(lambda x: ai.ask(x[2], kind), asked))
        s["ai_day"]["n"] += len(asked)
        s.setdefault("today", {})["ai"] = s.get("today", {}).get("ai", 0) + len(asked)
    out = []
    for i, (what, ru, r) in enumerate(todo):
        a = answers[i] if i < len(answers) else None
        if what == "unsure" and i >= len(asked):
            continue  # over budget: stays unchecked, retried next run
        r2 = ai.apply(r, a, kind)
        if a:
            u = a.get("usage") or {}
            print(f"  AI {r['id']} {what}: {a.get('model')} {a.get('res')} new €{a.get('new_price_eur')} "
                  f"({a.get('confidence')}) · tokens in {u.get('prompt_tokens')} out {u.get('completion_tokens')}")
        ru2 = rule_of(r2)
        if what == "hit":
            out.append((ru2 or ru, r2, "" if ru2 or not a else "❓"))
            continue
        s.setdefault("ai_checked", {})[r["id"]] = {"at": now(), "ok": bool(ru2), "ai": bool(a)}
        if ru2:
            out.append((ru2, r2, ""))
        elif not a or maybe_rule(cfg, r2, tv_kind):  # AI failed or still could not tell
            out.append((ru, r2, "❓"))
        else:
            log(f"AI-NO [{ru['name']}] {r['id']} {r.get('title')!r}: {a.get('model')} {a.get('res')} {a.get('inch')}\"")
    return out


# ---------------------------------------------------------------- state
def load_state():
    return hunt.load(STATE_P, {"alerted": {}, "pause_until": None, "backoff_h": 1, "seeded": False})


def save_state(s):
    hunt.save(STATE_P, s)


def active_rows(c):
    return [dict(r) for r in c.execute("SELECT * FROM listings WHERE active=1").fetchall()]


def seed(s, cfg, rows):
    n = 0
    for r in rows:
        ru = first_rule(cfg, r)
        if ru and r["id"] not in s["alerted"]:
            s["alerted"][r["id"]] = {"at": now(), "rule": ru["name"], "price": r.get("price"), "seeded": True}
            n += 1
    s["seeded"] = True
    return n


# ---------------------------------------------------------------- commands
def fetch_newest():
    rows = {}
    base = {"l1CategoryId": 322, "l2CategoryIds": 336, "distanceMeters": hunt.CFG["radius_km"] * 1000,
            "limit": 100, "offset": 0, "sortBy": "SORT_INDEX", "sortOrder": "DECREASING",
            "viewOptions": "list-view", "attributeRanges[]": f"PriceCents:null:{hunt.CFG['max_price'] * 100}"}
    for i, (city, pc) in enumerate(hunt.CFG["centers"].items()):
        if i:
            time.sleep(random.uniform(3, 6))
        j = hunt.mp_get(dict(base, postcode=pc, distanceMeters=hunt.radius_m(city)))
        for x in j.get("listings") or []:
            r = hunt.mp_row(x)
            r["near"] = city
            if r["id"] in rows:  # found around both centers
                old = rows[r["id"]]
                r["near"] = f"{old['near']},{city}"
                r["km"] = min(old["km"], r["km"])
            rows[r["id"]] = r
    out = []
    for r in rows.values():
        if db.keep(r):
            r = dict(r)
            r["posted"] = r.pop("date")
            out.append(r)
    return out


def fetch_tvs(cfg):
    """Newest page of Televisies per center (2 more API calls), parsed by tv.py."""
    rows = {}
    mx = (cfg.get("tv") or {}).get("fetch_max_price", 250)
    for city, pc in hunt.CFG["centers"].items():
        time.sleep(random.uniform(3, 6))
        j = hunt.mp_get({"l1CategoryId": tv.L1, "l2CategoryIds": tv.L2, "limit": 100, "offset": 0, "postcode": pc,
                         "distanceMeters": hunt.radius_m(city), "viewOptions": "list-view",
                         "sortBy": "SORT_INDEX", "sortOrder": "DECREASING", "attributeRanges[]": f"PriceCents:null:{mx * 100}"})
        for x in j.get("listings") or []:
            r = hunt.mp_row(x)
            r["near"] = city
            if r["id"] in rows:
                r["near"] = f"{rows[r['id']]['near']},{city}"
                r["km"] = min(rows[r["id"]]["km"], r["km"])
            rows[r["id"]] = r
    return [tv.parse(r) for r in rows.values()]


def cmd_run(args):
    cfg, s = conf(), load_state()
    rules = cfg["rules"]
    cloud = "--cloud" in args or os.environ.get("GITHUB_ACTIONS") == "true"
    t = dt.datetime.now()
    q0, q1 = cfg.get("quiet_hours", [1, 7])
    if "--force" not in args:
        if not cloud and q0 <= t.hour < q1:  # in the cloud the cron schedule handles the night
            return print("quiet hours")
        if s.get("pause_until") and t.isoformat() < s["pause_until"]:
            return print(f"paused until {s['pause_until']} after an HTTP error")
    try:
        rows = fetch_newest()
        tvs = fetch_tvs(cfg) if (cfg.get("tv") or {}).get("enabled") else []
    except Exception as e:  # noqa: BLE001  (403/429/5xx/timeouts: back off, never hammer)
        h = s.get("backoff_h", 1)
        s["pause_until"] = (t + dt.timedelta(hours=h)).isoformat(timespec="minutes")
        s["backoff_h"] = min(h * 2, 8)
        save_state(s)
        log(f"ERROR {e!r}; pausing {h} h")
        return print(f"error: {e!r}; paused {h} h")
    s["pause_until"], s["backoff_h"] = None, 1
    if cloud:  # no database in the cloud: rules run on the newest listings only
        pool = [db.enrich(dict({k: None for k in db.COLS}, **r)) for r in rows]
        prev = set(s.get("seen_ids", []))
        new_rows = [r for r in pool if r["id"] not in prev] if prev else []
        n_new = len(new_rows)
        s["seen_ids"] = [r["id"] for r in pool]
    else:  # every active listing, so matches found by `db.py sync` or a price drop also notify
        c = db.con()
        n_new = db.upsert(c, rows)
        pool = active_rows(c)
    if not s.get("seeded"):
        n = seed(s, cfg, pool)
        save_state(s)
        log(f"seeded {n} existing matches (no notification)")
        return print(f"first run: {n} current matches marked as seen; from now on only new ones notify")
    hits = []
    for r in pool:
        if r["id"] in s["alerted"]:
            continue
        ru = first_rule(cfg, r)
        if ru:
            hits.append((ru, r))
    hits.sort(key=lambda h: rules.index(h[0]))
    hits = verify_hits(cfg, s, hits, tv_kind=False)
    checked = s.setdefault("ai_checked", {})
    hit_ids = {x["id"] for _, x in hits}
    unsure = [(ru, r) for r in pool if r["id"] not in s["alerted"] and r["id"] not in checked
              and r["id"] not in hit_ids and not first_rule(cfg, r) and (ru := maybe_rule(cfg, r, False))]
    n_ai = ai_budget(cfg, s) - len(hits)
    unsure = verify_hits(cfg, s, unsure[:n_ai], tv_kind=False) if n_ai > 0 else []
    hits = ai_pass(cfg, s, hits, unsure, tv_kind=False)
    shown = 0
    for ru, r, tag in hits:
        title, body = describe(ru, r, tag)
        if shown < cfg.get("max_toasts", 4):
            notify.toast(title, body, r["url"])
            shown += 1
        notify.ntfy(cfg.get("ntfy_topic"), title, body, r["url"])
        s["alerted"][r["id"]] = {"at": now(), "rule": ru["name"], "price": r.get("price"), "unsure": bool(tag)}
        log(f"ALERT{' ' + tag if tag else ''} [{ru['name']}] {r['id']} €{r.get('price')} {r.get('model') or r['title']!r} {r.get('city')} {r['url']}")
    # TVs: no database, the newest page per center is the pool; every match not yet notified fires once
    tv_prev = set(s.get("tv_seen_ids", []))
    tv_new = [r for r in tvs if r["id"] not in tv_prev]
    s["tv_seen_ids"] = [r["id"] for r in tvs]
    tv_rules = (cfg.get("tv") or {}).get("rules", [])
    tv_hits = [(ru, r) for r in tvs if r["id"] not in s["alerted"] and (ru := tv_first_rule(cfg, r))]
    tv_hits.sort(key=lambda h: tv_rules.index(h[0]))
    tv_hits = verify_hits(cfg, s, tv_hits, tv_kind=True)
    hit_ids = {x["id"] for _, x in tv_hits}
    tv_unsure = [(ru, r) for r in tvs if r["id"] not in s["alerted"] and r["id"] not in checked
                 and r["id"] not in hit_ids and not tv_first_rule(cfg, r) and (ru := maybe_rule(cfg, r, True))]
    n_ai = ai_budget(cfg, s) - len(tv_hits)
    tv_unsure = verify_hits(cfg, s, tv_unsure[:n_ai], tv_kind=True) if n_ai > 0 else []
    tv_hits = ai_pass(cfg, s, tv_hits, tv_unsure, tv_kind=True)
    for ru, r, tag in tv_hits:
        title, body = describe_tv(ru, r, tag)
        if shown < cfg.get("max_toasts", 4):
            notify.toast(title, body, r["url"])
            shown += 1
        notify.ntfy(cfg.get("ntfy_topic"), title, body, r["url"])
        s["alerted"][r["id"]] = {"at": now(), "rule": ru["name"], "price": r.get("price"), "tv": True, "unsure": bool(tag)}
        log(f"ALERT TV{' ' + tag if tag else ''} [{ru['name']}] {r['id']} €{r.get('price')} {r['title']!r} {r.get('city')} {r['url']}")
    hits_all = hits + tv_hits
    if len(hits_all) > shown:
        notify.toast(f"🔔 +{len(hits_all) - shown} avisos más", "python -I alert.py log", "")
    if hits_all:
        notify.sound()
    if cloud:
        day = dt.date.today().isoformat()
        d = s.setdefault("today", {})
        if d.get("date") != day:
            d.clear()
            d.update(date=day, checks=0, new=0, alerts=0)
        d["checks"] += 1
        d["new"] += n_new
        d["alerts"] += len(hits)
        d["tv_new"] = d.get("tv_new", 0) + len(tv_new)
        d["tv_alerts"] = d.get("tv_alerts", 0) + len(tv_hits)
        if t.hour >= cfg.get("heartbeat_hour", 21) and s.get("heartbeat") != day and cfg.get("heartbeat", True):
            notify.ntfy(cfg.get("ntfy_topic"), "✅ Alertas de monitores y teles activas",
                        f"Hoy: {d['checks']} revisiones · monitores: {d['new']} nuevos, {d['alerts']} alertas · "
                        f"teles: {d['tv_new']} nuevas, {d['tv_alerts']} alertas · IA: {d.get('ai', 0)} consultas.", "", priority=2)
            s["heartbeat"] = day
        summary(cfg, pool, new_rows, hits, s)
        summary_tv(cfg, tvs, tv_new, tv_hits)
    save_state(s)
    print(f"{now()}: {len(rows)} newest listings ({n_new} new), {len(hits)} alerts · "
          f"TVs: {len(tvs)} newest ({len(tv_new)} new), {len(tv_hits)} alerts")
    for ru, r, _ in hits + tv_hits:
        print(f"  ALERT [{ru['name']}] €{r.get('price')} {(r.get('model') or r.get('title') or '')[:60]} · {r.get('city')} · {r['url']}")


def summary_tv(cfg, tvs, tv_new, tv_hits):
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if not out or not tvs:
        return
    hit = {r["id"]: ru["name"] for ru, r, *_ in tv_hits}
    L = ["", f"### 📺 Teles · {len(tvs)} revisadas · {len(tv_new)} nuevas · {len(tv_hits)} alertas", ""]
    big = [r for r in tv_new if (r.get("inch") or 0) >= (cfg.get("tv") or {}).get("min_inch", 50)]
    if big:
        L += ["| Anuncio | Precio | Tamaño | Res. | Panel | Dónde | Resultado |", "|---|---|---|---|---|---|---|"]
        for r in big[:40]:
            ru = tv_first_rule(cfg, r)
            res = f"📺 **{hit[r['id']]}**" if r["id"] in hit else (f"ya avisado ({ru['name']})" if ru else
                                                                   "dañada/accesorio" if r.get("damage") or r.get("accessory") else "no cumple reglas")
            price = "€%.0f" % r["price"] if r.get("price") else "—"
            L.append(f"| [{r['title'][:60].replace('|', '/')}]({r['url']}) | {price} | {r['inch']}\" | {r.get('res') or '?'} | "
                     f"{r.get('panel') or '?'} | {r.get('city') or '?'} {r.get('km') or 0} km | {res} |")
    with open(out, "a", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def summary(cfg, pool, new_rows, hits, s):
    """Markdown summary shown on the GitHub Actions run page."""
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if not out:
        return
    hit_ids = {r["id"]: ru["name"] for ru, r, *_ in hits}
    L = [f"### {now()} · {len(pool)} anuncios revisados · {len(new_rows)} nuevos desde la revisión anterior · "
         f"{len(hits)} alertas", ""]
    if new_rows:
        L += ["| Anuncio | Precio | Tamaño | Res. | Dónde | Resultado |", "|---|---|---|---|---|---|"]
        for r in new_rows[:40]:
            ru = first_rule(cfg, r)
            res = f"🔔 **{hit_ids[r['id']]}**" if r["id"] in hit_ids else (f"ya avisado ({ru['name']})" if ru else "no cumple reglas")
            title = (r.get("model") or r.get("title") or "")[:60].replace("|", "/")
            price = "€%.0f" % r["price"] if r.get("price") else "—"
            inch = '%g"' % r["inch"] if r.get("inch") else "?"
            L.append(f"| [{title}]({r['url']}) | {price} | {inch} | {r.get('res') or '?'} | "
                     f"{r.get('city') or '?'} {r.get('km') or 0} km | {res} |")
    match_now = [(first_rule(cfg, r), r) for r in pool]
    match_now = [(ru, r) for ru, r in match_now if ru]
    L += ["", f"**Ahora mismo cumplen alguna regla {len(match_now)} de los {len(pool)} anuncios más nuevos:**", ""]
    L += [f"- [{(r.get('model') or r['title'])[:60]}]({r['url']}) · €{r.get('price') or 0:.0f} · {ru['name']}"
          for ru, r in match_now[:15]]
    d = s.get("today", {})
    L += ["", f"Hoy: {d.get('checks', 0)} revisiones, {d.get('new', 0)} anuncios nuevos vistos, {d.get('alerts', 0)} alertas."]
    with open(out, "a", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def cmd_test(args):
    cfg, c = conf(), db.con()
    rules = cfg["rules"]
    by = {ru["name"]: [] for ru in rules}
    for r in active_rows(c):
        ru = first_rule(cfg, r)
        if ru:
            by[ru["name"]].append(r)
    for name, rs in by.items():
        print(f"\n## {name}: {len(rs)} active listings would match")
        for r in sorted(rs, key=lambda r: r.get("price") or 0)[:6]:
            t, b = describe(next(x for x in rules if x["name"] == name), r)
            print(f"  €{r.get('price') or 0:>5.0f}  {b.replace(chr(10), ' | ')}  ({r['first_seen']})  {r['url']}")
    files = sorted(hunt.DATA.glob("tv_*.json"))
    if files and (cfg.get("tv") or {}).get("enabled"):  # TVs: last `python -I tv.py sweep`
        tvs = [tv.parse(r) for r in hunt.load(files[-1], [])]
        by = {ru["name"]: [] for ru in cfg["tv"]["rules"]}
        for r in tvs:
            ru = tv_first_rule(cfg, r)
            if ru:
                by[ru["name"]].append(r)
        print(f"\n# TVs from {files[-1].name} ({len(tvs)} listings)")
        for name, rs in by.items():
            print(f"\n## 📺 {name}: {len(rs)} would match")
            for r in sorted(rs, key=lambda r: r.get("price") or 0)[:8]:
                t, b = describe_tv(next(x for x in cfg["tv"]["rules"] if x["name"] == name), r)
                print(f"  €{r.get('price') or 0:>5.0f}  {b.replace(chr(10), ' | ')}  ({r.get('date')}, {r.get('near')})  {r['url']}")


def cmd_toast(args):
    t, b, u = "🔔 Prueba · €50", "Iiyama XUB3493WQSU-B1\n34\" UWQHD 75Hz · Utrecht 3 km", "https://www.marktplaats.nl"
    ok = notify.toast(t, b, u)
    notify.sound()
    print("toast sent" if ok else "toast FAILED")
    topic = conf().get("ntfy_topic")
    if topic:
        print("phone push sent" if notify.ntfy(topic, t, b, u) else "phone push FAILED")


def cmd_ping(args):
    cfg = conf()
    ok = notify.ntfy(cfg.get("ntfy_topic"), "🔔 Prueba desde la nube",
                     "Las alertas de monitores funcionan desde GitHub Actions.", "https://www.marktplaats.nl")
    print("phone push sent" if ok else "phone push FAILED (is NTFY_TOPIC set?)")


def cmd_seed(args):
    s, c = load_state(), db.con()
    print(f"{seed(s, conf(), active_rows(c))} matches marked as seen")
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
