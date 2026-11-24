# monitor-alerts

Checks the newest Marktplaats listings around Amsterdam and Utrecht every 10 minutes (GitHub Actions) and sends a
phone push through ntfy when one matches a rule. What it looks for is a set of *kinds* in `kinds/`: today big
monitors (≥31") and big TVs (≥50").

- Kinds: `kinds/<name>.json` holds what to search (Marktplaats category, max price), the rules, labels, push icon
  and topic, and the value model (how fast it loses value). Logic a product needs of its own (parsing sizes,
  estimating the new price, the push layout) goes in an optional `kinds/<name>.py` subclass of `kinds/base.py`.
  Rule keys are listed in `Kind.match` (`max_price`, `min_<field>`, `<field>: [values]`, `models` regex, …).
- Shared settings (AI budget, Utrecht price factor, damage handling, quiet hours): `alerts.json` (see its `_help`).
  Known monitor models, sizes and new prices: `models.json`.
- Test a change without network: `python -I alert.py test` (every kind's rules against stored listings).
- Phone push: repository secret `NTFY_TOPIC` (`monitores-<id>`); each kind gets its own topic `<topic>-<id>`
  (or `NTFY_TOPIC_<NAME>`), so it shows as its own thread in the ntfy app.
- Run now: Actions → monitor-alerts → Run workflow (tick "test push" to only send one sample push per kind).
  Pause: Actions → monitor-alerts → ⋯ → Disable workflow.
- Locally on Windows, `python -I alert.py run` also shows a Windows toast and keeps a full monitor database (`db.py`).

## Adding a kind

1. Find the Marktplaats category ids (`l1CategoryId`, `l2CategoryIds`) of the product.
2. Copy `kinds/tv.json` to `kinds/<name>.json` and change label, plural, icon (an ntfy tag), topic, search, rules
   and value (`age_rate`: value kept per year, e.g. 0.90 for electronics, higher for bikes or furniture).
3. If the listings need their own parsing or push lines, add `kinds/<name>.py` with `class Kind(base.Kind)`.
4. `python -I alert.py test`, then push: the next run fetches 2 more pages (one per center), 3–6 s apart.
