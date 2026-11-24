"""Kinds of product the alerts look for (monitor, tv, …).

Each kind is kinds/<name>.json (what to search on Marktplaats, the rules, labels, push topic) plus an optional
kinds/<name>.py with a `Kind` subclass (parsing, value model, push layout). A kind without a .py uses kinds/base.py
as is. alert.py runs every enabled kind the same way: fetch → parse → rules → full-description check → AI → push.

To add a kind, see README.md ("Adding a kind").
"""
import importlib
import json
from pathlib import Path

from .base import Kind  # noqa: F401

DIR = Path(__file__).resolve().parent
_ALL = None


def all_kinds():
    """{name: Kind} for every kinds/<name>.json, in their "order"."""
    global _ALL
    if _ALL is None:
        out = []
        for p in DIR.glob("*.json"):
            cfg = json.loads(p.read_text(encoding="utf-8"))
            cls = importlib.import_module(f"kinds.{p.stem}").Kind if (DIR / f"{p.stem}.py").exists() else Kind
            out.append(cls(p.stem, cfg))
        _ALL = {k.name: k for k in sorted(out, key=lambda k: (k.cfg.get("order", 99), k.name))}
    return _ALL


def enabled():
    return [k for k in all_kinds().values() if k.cfg.get("enabled", True)]


def get(kind):
    """A Kind from its name (or the Kind itself)."""
    return kind if isinstance(kind, Kind) else all_kinds()[kind]
