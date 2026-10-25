"""Accent-insensitive title search over data/raw/index.jsonl. Usage: uv run python scripts/find_procedure.py data/raw/index.jsonl term [term ...] (all terms must match)."""

import json
import sys
import unicodedata


def norm(text: str) -> str:
    stripped = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    return stripped.replace("ς", "σ")


rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
for group in sys.argv[2:]:
    terms = [norm(t) for t in group.split("+")]
    hits = [r for r in rows if all(t in norm(r["title_el"]) for t in terms)]
    print(f"== {group} ({len(hits)})")
    for r in hits[:12]:
        print(f"  {r['id']}  {r['title_el'][:110]}")
