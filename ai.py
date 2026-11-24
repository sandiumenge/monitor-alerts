"""Second opinion from DeepSeek (deepseek-flash, with its built-in web search and the listing photos).

Used by alert.py only on listings that are about to be notified, or that might fit a rule but miss a key fact
(a TV with no stated resolution, a monitor with no size). It identifies the exact model, checks the specs and
looks up the new price, so the push can show "New €X (−Y%)". The prompt and which answer fields are copied
into the listing come from the listing's kind (kinds/: Kind.ai_system, AI_COPY, ai_apply_extra).

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

def key():
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    f = ROOT / "data" / "deepseek_key.txt"
    return k or (f.read_text(encoding="utf-8").strip() if f.exists() else "")


def kind_of(kind):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import kinds  # noqa: E402
    return kinds.get(kind)


def ask(r, kind, timeout=100):
    """Returns the parsed dict (plus 'usage'), or None on any failure: the push then goes out without it."""
    k = key()
    if not k:
        return None
    K = kind_of(kind)
    text = (f"Product type: {K.ai_product}\nTitle: {r.get('title')}\nAsking price: "
            f"{'€%.0f' % r['price'] if r.get('price') else 'not stated / bidding'}\n"
            f"Marktplaats attributes: {json.dumps(r.get('attrs') or {}, ensure_ascii=False)}\n"
            f"Description: {(r.get('desc') or '')[:3000]}")
    content = [{"type": "text", "text": text}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in (r.get("imgs") or [])[:2]]
    body = {"model": MODEL, "thinking": {"type": "disabled"}, "max_tokens": 700, "stream": False,
            "tools": [{"type": "deepseek_web_search"}],
            "messages": [{"role": "system", "content": K.ai_system()}, {"role": "user", "content": content}]}
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
    K = kind_of(kind)
    r = dict(r, ai=a)
    for f in K.AI_COPY:
        v = a.get(f)
        if v in (None, ""):
            continue
        if f == "res" and r.get("res_guess"):
            r["res"], r["res_guess"] = v, a.get("confidence") != "alta"  # replace our year-based guess
        elif not r.get(f):
            r[f] = v
    r = K.ai_apply_extra(r, a)
    new = a.get("new_price_eur")
    if isinstance(new, (int, float)) and new > 0 and r.get("price"):
        if K.AI_NEW_PRICE_ALWAYS or not r.get("new_price"):
            r["new_price"] = new
            r["discount"] = round((1 - r["price"] / new) * 100)
    return r


def line(r):
    """Extra push line only when the AI sees a defect the listing does not mention (specs, model and new price
    already go in the title, spec and price lines: alert.finish; the note is kept out to stay short)."""
    a = r.get("ai")
    if not a:
        return ""
    s = ""
    if a.get("defects") and a.get("damage") not in ("light", "heavy") and not r.get("damage") and \
            not re.match(r"(ninguno|ninguna|no |sin |none|nada|nothing|n/a)", a["defects"], re.I):
        s += f"\n⚠️ AI sees: {a['defects'][:100]}"  # light/heavy verdicts are shown in the damage line instead
    return s.strip()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    import db  # noqa: E402
    import kinds  # noqa: E402
    u = sys.argv[1]
    d = db.fetch_details(u)
    r = {"id": u, "title": d.get("title") or u, "desc": d["desc"], "price": d.get("price"), "imgs": d.get("imgs") or []}
    ks = list(kinds.all_kinds().values())  # the kind whose url_hint ("/televisies/") is in the URL, else the first
    kind = next((k for k in ks if k.cfg.get("url_hint") and k.cfg["url_hint"] in u), ks[0])
    r = kind.parse(r)
    print(f"key: {'yes' if key() else 'MISSING'} · photos: {len(r.get('imgs') or [])}")
    a = ask(r, kind)
    print(json.dumps(a, ensure_ascii=False, indent=1))
    print(line(apply(r, a, kind)))
