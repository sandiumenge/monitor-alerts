"""Second opinion from DeepSeek (deepseek-flash, with its built-in web search and the listing photos).

Used by alert.py only on listings that are about to be notified, or that might fit a rule but miss a key fact
(a TV with no stated resolution, a monitor with no size). It identifies the exact model, checks the specs and
looks up the new price, so the push can show "nuevo ~€X (−Y %)".

Key: DEEPSEEK_API_KEY env var (GitHub secret) or data/deepseek_key.txt locally (both kept out of git).
Only public listing data (title, description, price, photo URLs) is sent.

  python -I ai.py <marktplaats url>     try it on one listing (prints the parsed answer)
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
ROOT = Path(__file__).resolve().parent

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
    '"defects": str|null, "note": str}\n'
    "usbc_w = USB-C power delivery in watts (monitors only). defects = any damage or fault the seller mentions or "
    "the photos show, in Spanish, short; null when there is none. note = one short sentence in Spanish for the buyer (max 90 characters): "
    "what it is and whether the price is good."
)


def key():
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    f = ROOT / "data" / "deepseek_key.txt"
    return k or (f.read_text(encoding="utf-8").strip() if f.exists() else "")


def ask(r, kind="tv", timeout=100):
    """Returns the parsed dict (plus 'usage'), or None on any failure: the push then goes out without it."""
    k = key()
    if not k:
        return None
    what = "TV" if kind == "tv" else "computer monitor"
    text = (f"Product type: {what}\nTitle: {r.get('title')}\nAsking price: "
            f"{'€%.0f' % r['price'] if r.get('price') else 'not stated / bidding'}\n"
            f"Marktplaats attributes: {json.dumps(r.get('attrs') or {}, ensure_ascii=False)}\n"
            f"Description: {(r.get('desc') or '')[:3000]}")
    content = [{"type": "text", "text": text}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in (r.get("imgs") or [])[:2]]
    body = {"model": MODEL, "thinking": {"type": "disabled"}, "max_tokens": 700, "stream": False,
            "tools": [{"type": "deepseek_web_search"}],
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]}
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {k}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            j = json.loads(resp.read().decode("utf-8").strip())
    except Exception as e:  # noqa: BLE001
        if r.get("images_failed") is None and (r.get("imgs") and "400" in str(e)):
            return ask(dict(r, imgs=[], images_failed=True), kind, timeout)  # a photo URL the API refused
        print(f"  AI error {r.get('id')}: {str(e)[:120]}")
        return None
    if j.get("error"):
        print(f"  AI error {r.get('id')}: {str(j['error'])[:120]}")
        return None
    msg = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    m = re.search(r"\{.*\}", msg, re.S)
    try:
        out = json.loads(m.group(0)) if m else None
    except ValueError:
        out = None
    if not isinstance(out, dict):
        print(f"  AI unparsable answer {r.get('id')}: {msg[:120]!r}")
        return None
    out["usage"] = j.get("usage")
    return out


def apply(r, a, kind):
    """Copy what the AI found into the listing (only fields the listing does not state itself)."""
    if not a:
        return r
    r = dict(r, ai=a)
    for f in ("inch", "res", "panel", "hz", "year", "smart"):
        v = a.get(f)
        if v in (None, ""):
            continue
        if f == "res" and r.get("res_guess"):
            r["res"], r["res_guess"] = v, a.get("confidence") != "alta"  # replace our year-based guess
        elif not r.get(f):
            r[f] = v
    if kind != "tv":
        if a.get("model") and not r.get("model"):
            r["model"] = a["model"]
        if a.get("usbc_w") and not re.search(r"\d+W", r.get("usbc") or ""):
            r["usbc"] = f"{a['usbc_w']}W"
    new = a.get("new_price_eur")
    if isinstance(new, (int, float)) and new > 0 and r.get("price"):
        if kind == "tv" or not r.get("new_price"):
            r["new_price"] = new
            r["discount"] = round((1 - r["price"] / new) * 100)
    return r


def line(r):
    """One push line: '🤖 LG 55UK6300 (2018) · 4K LED · nuevo ~€450 (−82 %) · note'."""
    a = r.get("ai")
    if not a:
        return ""
    spec = " ".join(str(x) for x in [a.get("res") or "", a.get("panel") or "", a.get("year") or ""] if x)
    parts = [a.get("model") or "modelo desconocido", spec]
    if a.get("new_price_eur"):
        d = f" (−{r['discount']} %)" if r.get("discount") is not None and r["discount"] > 0 else ""
        parts.append(f"nuevo ~€{a['new_price_eur']:.0f}{d}")
    if a.get("confidence") and a["confidence"] != "alta":
        parts.append(f"confianza {a['confidence']}")
    s = "🤖 " + " · ".join(p for p in parts if p)
    if a.get("note"):
        s += f"\n🤖 {a['note'][:110]}"
    if a.get("defects") and not r.get("damage") and not re.match(r"(ninguno|ninguna|no |sin |none|nada)", a["defects"], re.I):
        s += f"\n⚠️ IA: {a['defects'][:100]}"
    return s


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    import db  # noqa: E402
    import tv  # noqa: E402
    u = sys.argv[1]
    d = db.fetch_details(u)
    r = {"id": u, "title": d.get("title") or u, "desc": d["desc"], "price": d.get("price"), "imgs": d.get("imgs") or []}
    kind = "tv" if "/televisies/" in u else "monitor"
    r = tv.parse(r) if kind == "tv" else db.enrich(dict({k: None for k in db.COLS}, **r))
    print(f"key: {'yes' if key() else 'MISSING'} · photos: {len(r.get('imgs') or [])}")
    a = ask(r, kind)
    print(json.dumps(a, ensure_ascii=False, indent=1))
    print(line(apply(r, a, kind)))
