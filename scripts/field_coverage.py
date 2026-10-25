"""Field coverage across downloaded Mitos JSON: which keys exist, how often, sample values. Usage: uv run python scripts/field_coverage.py data/raw/services"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def walk(value: object, path: str, counts: Counter, samples: dict, types: dict) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            walk(child, f"{path}.{key}" if path else key, counts, samples, types)
    elif isinstance(value, list):
        for child in value:
            walk(child, f"{path}[]", counts, samples, types)
        if not value:
            types[path].add("emptylist")
    else:
        counts[path] += 1
        types[path].add(type(value).__name__)
        if value not in (None, "", False) and len(samples[path]) < 3:
            text = str(value).replace("\n", " ")
            if text not in samples[path]:
                samples[path].append(text[:90])


def main() -> None:
    services_dir = Path(sys.argv[1])
    files = sorted(services_dir.glob("*.json"))
    per_doc: Counter = Counter()
    counts: Counter = Counter()
    samples: dict = defaultdict(list)
    types: dict = defaultdict(set)
    for path in files:
        doc_counts: Counter = Counter()
        walk(json.loads(path.read_text(encoding="utf-8")), "", doc_counts, samples, types)
        counts.update(doc_counts)
        per_doc.update(doc_counts.keys())
    print(f"{len(files)} documents\n")
    for key in sorted(per_doc):
        print(f"{per_doc[key]:5d} docs {counts[key]:6d} vals  {key}  {sorted(types[key])}  {samples[key][:2]}")


if __name__ == "__main__":
    main()
