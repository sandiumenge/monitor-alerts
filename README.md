# monitor-alerts

Checks the newest Marktplaats "Monitoren" listings around Amsterdam and Utrecht every 20 minutes (GitHub Actions)
and sends a phone push through ntfy when one matches a rule in `alerts.json`.

- Rules: edit `alerts.json` (see its `_help`). Known models, sizes and new prices: `models.json`.
- Phone push topic: repository secret `NTFY_TOPIC`.
- Run now: Actions → monitor-alerts → Run workflow. Pause: Actions → monitor-alerts → ⋯ → Disable workflow.
- Locally on Windows, `python -I alert.py run` also shows a Windows toast and keeps a full database (`db.py`).
