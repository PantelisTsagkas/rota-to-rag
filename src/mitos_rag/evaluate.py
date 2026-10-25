"""Retrieval evaluation: does the right procedure come back, and how high?

With `--dense MODEL ...`, also runs dense retrieval over the cached chunk
embeddings (`mitos-embed`), and a hybrid that fuses the BM25 section/normalised
ranking with the dense ranking by reciprocal rank fusion (`dense.py`).
With `--vector-index BUCKET/INDEX` as well, also queries that S3 Vectors index
(`vectorstore.py`, approximate search) with the first dense model's cached
question vectors, to compare it with exact search. These are paid calls.

Runs BM25 over the rendered corpus in every combination of:

- granularity: whole doc, or section (best section per procedure),
- tokens: raw (casefold only), normalised (accents folded) or stemmed (text.py),
- corpus: all procedures, or without the 667 internal ones (decision 001).

Metrics (PRD 8.2), over items whose expected status is not `not_found`:

- hit@k: any expected or acceptable procedure in the top k.
- MRR: 1 / rank of the first expected or acceptable procedure (0 if not ranked).
- recall@k: share of the expected procedures in the top k. Skipped for items
  with no expected procedures (acceptable only).

`not_found` items have nothing to retrieve. Their top-1 score is reported next
to the answered items' scores, as early evidence for a not-found threshold.

Use the dev split while building. The test split is held out (PRD 8.1).

Usage:
    uv run mitos-eval --split dev
    uv run mitos-eval --split dev --dense cohere titan
    uv run mitos-eval --split dev --dense cohere --vector-index mitos-rag/chunks-cohere-v3-20261014
"""

import argparse
import json
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mitos_rag.bm25 import BM25
from mitos_rag.chunk import load_chunks
from mitos_rag.corpus import Unit, internal_procedure_ids, load_units
from mitos_rag.dense import DenseRetriever, reciprocal_rank_fusion
from mitos_rag.embed import REGION, VectorCache
from mitos_rag.text import TOKEN_MODES, TokenMode, tokenize

logger = logging.getLogger(__name__)

CUTOFFS = (1, 5, 10)


def load_eval(split: str, eval_dir: Path = Path("eval")) -> list[dict[str, Any]]:
    path = eval_dir / f"{split}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ProcedureRetriever:
    """Scores units with BM25 and ranks procedures by their best unit."""

    def __init__(self, units: list[Unit], tokens: TokenMode) -> None:
        self.units = units
        self.tokens = tokens
        self.index = BM25([tokenize(unit.text, tokens) for unit in units])

    def rank(self, query: str, exclude: frozenset[str] = frozenset()) -> list[tuple[str, float]]:
        """All procedures that match at least one query term, best first.

        Ties break on procedure id so runs are reproducible.
        """
        best: dict[str, float] = {}
        for index, score in self.index.scores(tokenize(query, self.tokens)).items():
            procedure_id = self.units[index].procedure_id
            if procedure_id in exclude:
                continue
            if score > best.get(procedure_id, 0.0):
                best[procedure_id] = score
        return sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))


def first_relevant_rank(ranked_ids: list[str], relevant: set[str]) -> int | None:
    """1-based rank of the first relevant id, or None."""
    return next((position for position, pid in enumerate(ranked_ids, start=1) if pid in relevant), None)


def recall_at_k(ranked_ids: list[str], expected: set[str], k: int) -> float | None:
    if not expected:
        return None
    return len(expected & set(ranked_ids[:k])) / len(expected)


def score_item(item: dict[str, Any], ranking: list[tuple[str, float]]) -> dict[str, Any]:
    ranked_ids = [pid for pid, _ in ranking]
    expected = set(item["expected_procedure_ids"])
    relevant = expected | set(item["acceptable_procedure_ids"])
    rank = first_relevant_rank(ranked_ids, relevant)
    result: dict[str, Any] = {
        "id": item["id"],
        "question": item["question"],
        "category": item["category"],
        "expected_status": item["expected_status"],
        "first_relevant_rank": rank,
        "gold_ranks": {pid: ranked_ids.index(pid) + 1 if pid in ranked_ids else None for pid in sorted(relevant)},
        "top": [{"procedure_id": pid, "score": round(score, 3)} for pid, score in ranking[:5]],
        "top1_score": ranking[0][1] if ranking else 0.0,
    }
    if item["expected_status"] != "not_found":
        for k in CUTOFFS:
            result[f"hit@{k}"] = float(rank is not None and rank <= k)
            result[f"recall@{k}"] = recall_at_k(ranked_ids, expected, k)
        result["rr"] = 1 / rank if rank else 0.0
    return result


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in results if r["expected_status"] != "not_found"]
    summary: dict[str, Any] = {"n": len(scored)}
    for k in CUTOFFS:
        summary[f"hit@{k}"] = statistics.fmean(r[f"hit@{k}"] for r in scored) if scored else 0.0
        recalls = [r[f"recall@{k}"] for r in scored if r[f"recall@{k}"] is not None]
        summary[f"recall@{k}"] = statistics.fmean(recalls) if recalls else None
    summary["mrr"] = statistics.fmean(r["rr"] for r in scored) if scored else 0.0
    summary["hit@5_by_category"] = {
        category: statistics.fmean(r["hit@5"] for r in scored if r["category"] == category)
        for category in sorted({r["category"] for r in scored})
    }
    summary["answered_top1_score_median"] = statistics.median(r["top1_score"] for r in scored) if scored else 0.0
    summary["answered_top1_score_min"] = min((r["top1_score"] for r in scored), default=0.0)
    summary["not_found_top1_scores"] = {
        r["id"]: round(r["top1_score"], 3) for r in results if r["expected_status"] == "not_found"
    }
    return summary


@dataclass(frozen=True)
class Config:
    granularity: str
    tokens: TokenMode
    exclude_internal: bool

    @property
    def name(self) -> str:
        corpus = "external" if self.exclude_internal else "all"
        return f"{self.granularity}/{self.tokens}/{corpus}"


def check_gold_ids(items: list[dict[str, Any]], known_ids: set[str]) -> None:
    missing = {
        pid
        for item in items
        for pid in item["expected_procedure_ids"] + item["acceptable_procedure_ids"]
        if pid not in known_ids
    }
    if missing:
        raise ValueError(f"eval ids not in the corpus (re-render or fix labels): {sorted(missing)}")


def run(
    docs_dir: Path,
    raw_dir: Path,
    eval_dir: Path,
    split: str,
    dense_models: tuple[str, ...] = (),
    embeddings_dir: Path = Path("data/embeddings"),
    vector_index: str | None = None,
) -> dict[str, Any]:
    items = load_eval(split, eval_dir)
    internal = frozenset(internal_procedure_ids(raw_dir))
    gold = {pid for item in items for pid in item["expected_procedure_ids"] + item["acceptable_procedure_ids"]}
    if gold & internal:
        logger.warning("the internal filter removes eval ids: %s", sorted(gold & internal))

    report: dict[str, Any] = {"split": split, "internal_excluded": len(internal), "configs": {}}
    for granularity in ("doc", "section"):
        units = load_units(docs_dir, granularity)
        check_gold_ids(items, {unit.procedure_id for unit in units})
        logger.info("%s: %d units", granularity, len(units))
        for tokens in TOKEN_MODES:
            retriever = ProcedureRetriever(units, tokens)
            for exclude_internal in (False, True):
                config = Config(granularity, tokens, exclude_internal)
                exclude = internal if exclude_internal else frozenset()
                results = [score_item(item, retriever.rank(item["question"], exclude)) for item in items]
                report["configs"][config.name] = {"summary": summarise(results), "items": results}
            if granularity == "section" and tokens == "normalised":
                keyword = retriever  # the 002 baseline, fused with dense below

    if dense_models:
        chunks = load_chunks(docs_dir)
        for model in dense_models:
            dense = DenseRetriever.from_directory(chunks, embeddings_dir / model)
            dense_results, hybrid_results = [], []
            for item in items:
                dense_ranking = dense.rank(item["question"])
                fused = reciprocal_rank_fusion([keyword.rank(item["question"]), dense_ranking])
                dense_results.append(score_item(item, dense_ranking))
                hybrid_results.append(score_item(item, fused))
            report["configs"][f"dense/{model}/all"] = {"summary": summarise(dense_results), "items": dense_results}
            report["configs"][f"hybrid/{model}/all"] = {"summary": summarise(hybrid_results), "items": hybrid_results}

    if vector_index:
        import boto3

        from mitos_rag.vectorstore import S3VectorsRetriever

        bucket, index = vector_index.split("/", 1)
        model = dense_models[0]
        store = S3VectorsRetriever(
            boto3.client("s3vectors", region_name=REGION), bucket, index, VectorCache(embeddings_dir / model / "query")
        )
        results = [score_item(item, store.rank(item["question"])) for item in items]
        report["configs"][f"s3vectors/{model}/top{store.top_k}"] = {"summary": summarise(results), "items": results}
    return report


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def format_table(report: dict[str, Any]) -> str:
    lines = [
        "| config | hit@1 | hit@5 | hit@10 | MRR | recall@5 | recall@10 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, config in report["configs"].items():
        s = config["summary"]
        lines.append(
            f"| {name} | {_fmt(s['hit@1'])} | {_fmt(s['hit@5'])} | {_fmt(s['hit@10'])} | {_fmt(s['mrr'])} "
            f"| {_fmt(s['recall@5'])} | {_fmt(s['recall@10'])} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate procedure retrieval on the eval set.")
    parser.add_argument("--docs", type=Path, default=Path("data/docs"))
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--eval-dir", type=Path, default=Path("eval"))
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--dense", nargs="*", default=[], help="embedding models to add, e.g. cohere titan")
    parser.add_argument("--embeddings", type=Path, default=Path("data/embeddings"))
    parser.add_argument("--vector-index", help="BUCKET/INDEX in S3 Vectors to compare (needs --dense, paid calls)")
    parser.add_argument("--out", type=Path, default=Path("data/eval"), help="per-question results (git-ignored)")
    args = parser.parse_args()
    if args.vector_index and not args.dense:
        parser.error("--vector-index needs --dense MODEL for the cached question vectors")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.split == "test":
        logger.warning("test is the held-out split: report results, never tune on them (PRD 8.1)")

    report = run(args.docs, args.raw, args.eval_dir, args.split, tuple(args.dense), args.embeddings, args.vector_index)
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"retrieval-{args.split}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    first = next(iter(report["configs"].values()))["summary"]
    print(f"split={args.split}  scored items={first['n']}  internal procedures={report['internal_excluded']}\n")
    print(format_table(report))
    print(f"\nPer-question results: {out_path}")


if __name__ == "__main__":
    main()
