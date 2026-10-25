"""Rerank the dense top candidates before generation: `uv run mitos-rerank-eval`.

Dense retrieval almost always finds the right procedure (dev hit@20 1.00) but often
ranks it just outside the top 5, because the top scores are a near tie between
procedures that look alike: renewals of residence permits next to a driving licence
renewal, refugee travel documents next to the Greek passport. A reranker reads the
question next to each candidate's title and alternative titles and reorders them.

Two rerankers, both on Bedrock eu-central-1:

- `haiku`: Claude Haiku 4.5 with structured output returns the best procedure ids in
  order. General rules only (assume the ordinary citizen); no synonyms from the eval set.
- `cohere`: Cohere Rerank 3.5, a cross-encoder. $0.002 per call of up to 100 documents.
  The account quota is 3 requests per minute.

The eval is retrieval only: no answers are generated. It reports hit@5, MRR and
"all expected in the top 5" for questions that need several procedures. Responses are
cached on disk, so re-scoring is free.

Usage:
    uv run mitos-rerank-eval --split dev --dry-run
    uv run mitos-rerank-eval --split dev --reranker haiku cohere
"""

import argparse
import hashlib
import json
import logging
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from mitos_rag.chunk import load_chunks
from mitos_rag.corpus import ALTERNATIVE_TITLES, split_sections, strip_boilerplate
from mitos_rag.dense import DenseRetriever
from mitos_rag.embed import REGION
from mitos_rag.evaluate import first_relevant_rank, load_eval
from mitos_rag.pacing import Pacer

logger = logging.getLogger(__name__)

CANDIDATES = 30  # dense top procedures handed to the reranker
KEEP = 10  # how many the Haiku reranker returns in order
CARD_CHARS = 400

HAIKU_MODEL_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU_COUNT_TOKENS_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU_USD_PER_MTOK = (1.10, 5.50)  # Price List API, eu-central-1, 2026-10-17
HAIKU_REQUESTS_PER_MINUTE = 10

COHERE_MODEL_ID = "cohere.rerank-v3-5:0"
COHERE_USD_PER_CALL = 0.002  # one search unit: one query with up to 100 documents
COHERE_REQUESTS_PER_MINUTE = 3

RERANK_PROMPT = f"""You rank procedures from the Greek registry of public administration procedures (Mitos) by how well they match a citizen's question.

Citizens write in everyday Greek, often without accents, and use everyday words where the registry uses official terms. Work out what the citizen means, then rank the candidates by how likely each one is the procedure they need.

Assume an ordinary adult Greek citizen living in Greece, unless the question names a special group (for example foreigners, refugees, diplomats, seafarers, civil servants or businesses). Procedures for special groups rank below the general procedure for the same thing.

If the question asks about several things, put the best procedure for each of them near the top.

Return the procedure_ids of the {KEEP} best candidates, best first. Use only ids from <candidates>."""

RANKING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ranking": {"type": "array", "items": {"type": "string"}}},
    "required": ["ranking"],
    "additionalProperties": False,
}


def card(markdown: str) -> str:
    """Title and alternative titles: what the procedure is, without its long body."""
    title, _, sections = split_sections(strip_boilerplate(markdown))
    alternatives = next((body for heading, body in sections if heading == ALTERNATIVE_TITLES), "")
    text = title if not alternatives else f"{title} | {alternatives}"
    return " ".join(text.split())[:CARD_CHARS]


def load_cards(docs: Path) -> dict[str, str]:
    return {path.stem: card(path.read_text(encoding="utf-8")) for path in sorted(docs.glob("*.md"))}


def merge(reranked: list[str], candidates: list[str]) -> list[str]:
    """Reranked ids first (valid and unique only), then the remaining candidates in dense order."""
    allowed = set(candidates)
    head = [pid for pid in dict.fromkeys(reranked) if pid in allowed]
    return head + [pid for pid in candidates if pid not in head]


class Reranker(Protocol):
    name: str

    def rerank(self, question: str, candidates: list[str]) -> list[str]: ...


class _Paced:
    """Disk cache by request hash, and pacing to the per-minute quota for fresh calls.

    Pass `pacer` to share one quota with another caller of the same model.
    """

    def __init__(
        self,
        cache_dir: Path | None,
        requests_per_minute: int,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        pacer: Pacer | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.pacer = pacer or Pacer(requests_per_minute, sleep, clock)
        self.fresh_calls = 0
        self.cost_usd = 0.0
        self.last_latency_s = 0.0

    def _path(self, request: dict[str, Any]) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def cached_result(self, request: dict[str, Any]) -> Any | None:
        path = self._path(request)
        if path is not None and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))["result"]
        return None

    def cached_call(self, request: dict[str, Any], call: Callable[[], tuple[Any, float]]) -> Any:
        stored = self.cached_result(request)
        if stored is not None:
            return stored
        self.pacer.wait()
        start = self.pacer.clock()
        result, cost = call()
        self.last_latency_s = self.pacer.clock() - start  # the call only, not the pacing wait
        self.fresh_calls += 1
        self.cost_usd += cost
        path = self._path(request)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"request": request, "result": result, "cost_usd": cost}, ensure_ascii=False), encoding="utf-8")
        return result


def haiku_user_message(question: str, candidates: list[str], cards: dict[str, str]) -> str:
    lines = ["<candidates>"]
    lines += [f"{rank}. {pid} | {cards[pid]}" for rank, pid in enumerate(candidates, start=1)]
    lines += ["</candidates>", "", f"<question>{question}</question>"]
    return "\n".join(lines)


def haiku_request(question: str, candidates: list[str], cards: dict[str, str]) -> dict[str, Any]:
    return {
        "model": HAIKU_MODEL_ID,
        "max_tokens": 400,
        "system": RERANK_PROMPT,
        "messages": [{"role": "user", "content": haiku_user_message(question, candidates, cards)}],
        "output_config": {"format": {"type": "json_schema", "schema": RANKING_SCHEMA}},
        "extra_body": {"temperature": 0},
    }


class HaikuReranker(_Paced):
    name = "haiku"

    def __init__(self, client: Any, cards: dict[str, str], cache_dir: Path | None, **kwargs: Any) -> None:
        super().__init__(cache_dir, HAIKU_REQUESTS_PER_MINUTE, **kwargs)
        self.client = client
        self.cards = cards

    def rerank(self, question: str, candidates: list[str]) -> list[str]:
        request = haiku_request(question, candidates, self.cards)

        def call() -> tuple[list[str], float]:
            response = self.client.messages.create(**request)
            if response.stop_reason != "end_turn":
                raise RuntimeError(f"rerank stop_reason {response.stop_reason}")
            text = "".join(block.text for block in response.content if block.type == "text")
            usage = response.usage
            cost = (usage.input_tokens * HAIKU_USD_PER_MTOK[0] + usage.output_tokens * HAIKU_USD_PER_MTOK[1]) / 1e6
            return json.loads(text)["ranking"], cost

        return merge(self.cached_call(request, call), candidates)

    def cached_rerank(self, question: str, candidates: list[str]) -> list[str] | None:
        """The reranked order if this exact request is cached, else None. Never calls Bedrock."""
        stored = self.cached_result(haiku_request(question, candidates, self.cards))
        return None if stored is None else merge(stored, candidates)


def cohere_body(question: str, candidates: list[str], cards: dict[str, str]) -> dict[str, Any]:
    return {
        "api_version": 2,
        "query": question,
        "documents": [cards[pid] for pid in candidates],
        "top_n": len(candidates),
    }


class CohereReranker(_Paced):
    name = "cohere"

    def __init__(self, client: Any, cards: dict[str, str], cache_dir: Path | None, **kwargs: Any) -> None:
        super().__init__(cache_dir, COHERE_REQUESTS_PER_MINUTE, **kwargs)
        self.client = client
        self.cards = cards

    def rerank(self, question: str, candidates: list[str]) -> list[str]:
        body = cohere_body(question, candidates, self.cards)
        request = {"model": COHERE_MODEL_ID, "body": body}

        def call() -> tuple[list[int], float]:
            response = self.client.invoke_model(modelId=COHERE_MODEL_ID, body=json.dumps(body))
            payload = json.loads(response["body"].read())
            ordered = sorted(payload["results"], key=lambda r: -r["relevance_score"])
            return [r["index"] for r in ordered], COHERE_USD_PER_CALL

        indices = self.cached_call(request, call)
        return merge([candidates[i] for i in indices], candidates)


def score_item(item: dict[str, Any], ranked: list[str]) -> dict[str, Any]:
    expected = set(item["expected_procedure_ids"])
    relevant = expected | set(item["acceptable_procedure_ids"])
    rank = first_relevant_rank(ranked, relevant)
    return {
        "id": item["id"],
        "question": item["question"],
        "rank": rank,
        "hit_at_5": rank is not None and rank <= 5,
        "reciprocal_rank": 1 / rank if rank else 0.0,
        "all_expected_at_5": expected <= set(ranked[:5]),
        "top5": ranked[:5],
    }


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    multi = [r for r in results if r["n_expected"] > 1]
    return {
        "n": len(results),
        "hit_at_5": statistics.fmean(r["hit_at_5"] for r in results),
        "mrr": statistics.fmean(r["reciprocal_rank"] for r in results),
        "all_expected_at_5": statistics.fmean(r["all_expected_at_5"] for r in results),
        "multi_procedure_items": len(multi),
        "multi_all_expected_at_5": statistics.fmean(r["all_expected_at_5"] for r in multi) if multi else None,
        "misses_at_5": {r["id"]: r["rank"] for r in results if not r["hit_at_5"]},
    }


def count_haiku_tokens(client: Any, request: dict[str, Any]) -> int:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": request["max_tokens"],
        "system": request["system"],
        "messages": request["messages"],
    }
    response = client.count_tokens(modelId=HAIKU_COUNT_TOKENS_ID, input={"invokeModel": {"body": json.dumps(body)}})
    return int(response["inputTokens"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieval eval with a reranker over the dense top candidates.")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--eval-dir", type=Path, default=Path("eval"))
    parser.add_argument("--docs", type=Path, default=Path("data/docs"))
    parser.add_argument("--embeddings", type=Path, default=Path("data/embeddings"))
    parser.add_argument("--reranker", nargs="+", choices=("haiku", "cohere"), default=["haiku", "cohere"])
    parser.add_argument("--candidates", type=int, default=CANDIDATES)
    parser.add_argument("--cache", type=Path, default=Path("data/rerank/cache"))
    parser.add_argument("--out", type=Path, default=Path("data/eval"))
    parser.add_argument("--dry-run", action="store_true", help="quote the cost with CountTokens (free); no rerank calls")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.split == "test":
        logger.warning("test is the held-out split: report results, never tune on them (PRD 8.1)")

    import boto3

    items = [i for i in load_eval(args.split, args.eval_dir) if i["expected_procedure_ids"] or i["acceptable_procedure_ids"]]
    cards = load_cards(args.docs)
    dense = DenseRetriever.from_directory(load_chunks(args.docs), args.embeddings / "cohere")
    candidates = {item["id"]: [pid for pid, _ in dense.rank(item["question"])[: args.candidates]] for item in items}

    if args.dry_run:
        runtime = boto3.client("bedrock-runtime", region_name=REGION)
        tokens = [count_haiku_tokens(runtime, haiku_request(i["question"], candidates[i["id"]], cards)) for i in items]
        haiku = (sum(tokens) * HAIKU_USD_PER_MTOK[0] + len(items) * 150 * HAIKU_USD_PER_MTOK[1]) / 1e6
        print(f"{len(items)} questions, {args.candidates} candidates each")
        print(f"haiku: {sum(tokens):,} input tokens (mean {statistics.fmean(tokens):,.0f}), about ${haiku:.3f} at 150 output tokens each")
        print(f"cohere: {len(items)} calls x ${COHERE_USD_PER_CALL} = ${len(items) * COHERE_USD_PER_CALL:.3f}")
        return

    rerankers: list[Reranker] = []
    for name in args.reranker:
        if name == "haiku":
            from anthropic import AnthropicBedrock

            client = AnthropicBedrock(aws_region=REGION, max_retries=4)
            rerankers.append(HaikuReranker(client, cards, args.cache / "haiku"))
        else:
            rerankers.append(CohereReranker(boto3.client("bedrock-runtime", region_name=REGION), cards, args.cache / "cohere"))

    report: dict[str, Any] = {}
    arms: list[tuple[str, Callable[[dict[str, Any]], list[str]]]] = [("dense", lambda item: candidates[item["id"]])]
    arms += [(r.name, lambda item, r=r: r.rerank(item["question"], candidates[item["id"]])) for r in rerankers]
    for name, ranker in arms:
        results = []
        for item in items:
            result = score_item(item, ranker(item))
            result["n_expected"] = len(item["expected_procedure_ids"])
            results.append(result)
            logger.info("%s %s rank %s", name, item["id"], result["rank"])
        report[name] = {"summary": summarise(results), "items": results}
    for reranker in rerankers:
        paced = reranker if isinstance(reranker, _Paced) else None
        if paced is not None:
            report[reranker.name]["summary"].update(fresh_calls=paced.fresh_calls, cost_usd=round(paced.cost_usd, 4))

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"rerank-{args.split}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({name: arm["summary"] for name, arm in report.items()}, ensure_ascii=False, indent=2))
    print(f"\nPer-question results: {out_path}")


if __name__ == "__main__":
    main()
