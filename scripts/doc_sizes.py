"""Size of rendered docs and of each section, in KB, characters and tokens.

Tokens come from the bge-m3 tokenizer (XLM-R vocabulary, shared by bge-m3 and
multilingual-e5). It is a measuring stick for Greek text, not a chosen model.

Usage: uv run --with tokenizers python scripts/doc_sizes.py data/docs
"""

import statistics
import sys
from collections import defaultdict
from pathlib import Path

from tokenizers import Tokenizer


def quantile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[round(pct / 100 * (len(ordered) - 1))]


def main() -> None:
    tokenizer = Tokenizer.from_pretrained("BAAI/bge-m3")
    docs = sorted(Path(sys.argv[1]).glob("*.md"))
    sizes: dict[str, tuple[int, int, int]] = {}
    sections: dict[str, list[int]] = defaultdict(list)
    for path in docs:
        text = path.read_text(encoding="utf-8")
        tokens = len(tokenizer.encode(text, add_special_tokens=False).ids)
        sizes[path.stem] = (len(text.encode()), len(text), tokens)
        header, *parts = text.split("\n## ")
        sections["(header)"].append(len(tokenizer.encode(header, add_special_tokens=False).ids))
        for part in parts:
            name, _, body = part.partition("\n")
            sections[name].append(len(tokenizer.encode(body, add_special_tokens=False).ids))

    print(f"{len(docs)} docs")
    for label, index, scale in (("KB", 0, 1024), ("chars", 1, 1), ("tokens", 2, 1)):
        values = [row[index] / scale for row in sizes.values()]
        print(
            f"{label:7s} p50={quantile(values, 50):8.1f} p90={quantile(values, 90):8.1f} "
            f"p95={quantile(values, 95):8.1f} p99={quantile(values, 99):8.1f} "
            f"max={max(values):8.1f} mean={statistics.mean(values):8.1f}"
        )
    tokens = [row[2] for row in sizes.values()]
    for limit in (512, 1024, 2048, 4096, 8192):
        over = sum(t > limit for t in tokens)
        print(f"> {limit:5d} tokens: {over:5d} docs ({100 * over / len(tokens):.1f}%)")
    print(f"chars per token: {sum(row[1] for row in sizes.values()) / sum(tokens):.2f}")
    largest = sorted(sizes.items(), key=lambda item: -item[1][2])[:5]
    print("largest (id, tokens):", [(pid, row[2]) for pid, row in largest])

    total = sum(sum(values) for values in sections.values())
    print(f"\n{'section':22s} docs  share   p50   p95    max")
    for name, values in sorted(sections.items(), key=lambda item: -sum(item[1])):
        print(
            f"{name[:22]:22s} {len(values):4d} {100 * sum(values) / total:5.1f}% "
            f"{quantile(values, 50):5.0f} {quantile(values, 95):5.0f} {max(values):6d}"
        )


if __name__ == "__main__":
    main()
