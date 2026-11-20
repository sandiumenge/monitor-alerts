"""Monitor alerts: light Marktplaats check + rules from alerts.json -> Windows toast (and optional phone push).

  python -I alert.py run          one check (what the scheduled task runs): newest page of "Monitoren" per center
                                  (2 API calls), upsert into monitors.db, notify listings that match a rule
  python -I alert.py test         evaluate the rules against every active listing in the DB (no network, no toast)
  python -I alert.py toast        send a sample notification to check that toasts work
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
import notify  # noqa: E402

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
    return cfg


# ---------------------------------------------------------------- rules
def match(rule, r):
    text = f"{r.get('title') or ''} \n {r.get('desc') or ''} \n {r.get('model') or ''}"
    p = r.get("price")
    if "max_price" in rule:
        if p is None:
            if not rule.get("allow_no_price"):
                return False
        elif p > rule["max_price"]:
            return False
    inch = r.get("inch")
    if "min_inch" in rule and not (inch and inch >= rule["min_inch"]):
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
    if rule.get("no_damage") and set(filter(None, (r.get("damage") or "").split(", "))) - IGNORED_DAMAGE:
        return False
    if "max_km" in rule and (r.get("km") or 0) > rule["max_km"]:
        return False
    return True


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
    return next((ru for ru in cfg["rules"] if match(ru, r)), None)


def describe(rule, r):
    price = f"€{r['price']:.0f}" if r.get("price") else "sin precio"
    title = f"🔔 {rule['name']} · {price}"
    spec = " ".join(x for x in [f"{r['inch']:g}\"" if r.get("inch") else "", r.get("res") or "",
                                f"{r['hz']}Hz" if r.get("hz") else "",
                                f"USB‑C {r['usbc']}" if re.search(r"\d+W", r.get("usbc") or "") else ""] if x)
    disc = f" · −{r['discount']}%" if r.get("discount") else ""
    body = f"{r.get('model') or (r.get('title') or '')[:60]}\n{spec}{disc} · {r.get('city') or '?'} {r.get('km') or 0} km"
    return title, body


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
        j = hunt.mp_get(dict(base, postcode=pc))
        for x in j.get("listings") or []:
            r = hunt.mp_row(x)
            rows[r["id"]] = r
    out = []
    for r in rows.values():
        if db.keep(r):
            r = dict(r)
            r["posted"] = r.pop("date")
            out.append(r)
    return out


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
        n_new = len([r for r in pool if r["id"] not in s.get("seen_ids", [])])
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
    shown = 0
    for ru, r in hits:
        title, body = describe(ru, r)
        if shown < cfg.get("max_toasts", 4):
            notify.toast(title, body, r["url"])
            shown += 1
        notify.ntfy(cfg.get("ntfy_topic"), title, body, r["url"])
        s["alerted"][r["id"]] = {"at": now(), "rule": ru["name"], "price": r.get("price")}
        log(f"ALERT [{ru['name']}] {r['id']} €{r.get('price')} {r.get('model') or r['title']!r} {r.get('city')} {r['url']}")
    if len(hits) > shown:
        notify.toast(f"🔔 +{len(hits) - shown} monitores más", "python -I alert.py log", "")
    if hits:
        notify.sound()
    save_state(s)
    print(f"{now()}: {len(rows)} newest listings ({n_new} new in DB), {len(hits)} alerts")


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


def cmd_toast(args):
    t, b, u = "🔔 Prueba · €50", "Iiyama XUB3493WQSU-B1\n34\" UWQHD 75Hz · Utrecht 3 km", "https://www.marktplaats.nl"
    ok = notify.toast(t, b, u)
    notify.sound()
    print("toast sent" if ok else "toast FAILED")
    topic = conf().get("ntfy_topic")
    if topic:
        print("phone push sent" if notify.ntfy(topic, t, b, u) else "phone push FAILED")


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
        {"run": cmd_run, "test": cmd_test, "toast": cmd_toast, "seed": cmd_seed, "log": cmd_log}[cmd](sys.argv[2:])
    except Exception:  # noqa: BLE001  (scheduled runs have no console: keep the traceback)
        import traceback
        log("CRASH " + traceback.format_exc().replace("\n", " | "))
        raise
