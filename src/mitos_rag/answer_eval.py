"""Answer evaluation (PRD 8.2): status, citations and facts, with retrieval misses kept apart.

For every eval item, `mitos-ask` runs end to end and the result is scored:

- retrieval_hit: an expected or acceptable procedure is among the candidates the model
  saw (the top 5 after reranking, or by best chunk with `--rerank none`). A miss here is a retrieval failure, not a generation one.
- status_correct: the final status (after code checks) equals `expected_status`.
- citation_relevant: for items expected to be answered or ambiguous, at least one cited
  procedure (or option) is an expected or acceptable one. Lenient.
- citation_precision: the share of cited procedures (and options) that are expected or
  acceptable. Strict: an off-target extra citation lowers it. This is the closer measure of
  PRD 8.2 "the cited page supports the answer", which only a human read can fully confirm.
- invalid_citations: procedure ids the model cited that were not retrieved (target 0).
- fact_correct: each `expected_facts` entry is backed by a validated typed fact, and for
  costs the amount also appears in the answer text.
- all_expected_cited: for answered items with more than one expected procedure (a birth
  needs both the registration and the AMKA), every one of them is cited. An answer that
  covers one of two is not fully correct. Ambiguous items are left out: their expected
  procedures are the possible readings, and a good clarifying question need not list all.
- unsupported_claims: costs, durations or validity periods that failed the facts check,
  plus amounts and durations in the text that are in neither the facts row nor the text of
  a cited procedure (target 0, PRD 8.3). These are what show the warning.
- text_grounded: amounts and durations found only in a cited procedure's text (decision 010).
  Reported apart: they show no warning, but they are a weaker check than the facts row.

Use `--dry-run` first: it builds every prompt and counts its tokens with Bedrock
CountTokens (free), and prints the cost estimate. Nothing is sent to Claude. With the
reranker, the answer prompts use the cached rerank order when there is one, and the dense
top 5 otherwise: a close proxy, since the evidence budget caps the prompt size.

Results go to `data/eval/answers-<split>-<model>[-rerank-haiku].json`, so the reranked
runs never overwrite the 006 and 007 baselines.

Usage:
    uv run mitos-answer-eval --split dev --dry-run
    uv run mitos-answer-eval --split dev
    uv run mitos-answer-eval --split dev --model sonnet-4.6 --dry-run
    uv run mitos-answer-eval --split dev --rerank none   # the 006 / 007 pipeline
"""

import argparse
import json
import logging
import statistics
from pathlib import Path
from typing import Any

from mitos_rag.answer import (
    ANSWER_MODELS,
    MAX_TOKENS,
    SYSTEM_PROMPT,
    Answer,
    Assistant,
    ModelSpec,
    add_common_arguments,
    build_assistant,
    build_evidence,
    user_message,
)
from mitos_rag.embed import REGION
from mitos_rag.evaluate import load_eval
from mitos_rag.rerank import HAIKU_USD_PER_MTOK, count_haiku_tokens, haiku_request
from mitos_rag.validate import euro_amounts

logger = logging.getLogger(__name__)

ESTIMATED_OUTPUT_TOKENS = 700  # for the dry-run quote; the real run reports actual usage
ESTIMATED_RERANK_OUTPUT_TOKENS = 150


def fact_correct(expected: dict[str, Any], answer: Answer, relevant: set[str]) -> bool:
    supported = [f for f in answer.validation.supported_facts if f["procedure_id"] in relevant]
    kind = expected["type"]
    if kind == "cost":
        amount = round(float(expected["amount"]), 2)
        in_facts = any(f["type"] == "cost" and round(float(f["value"].replace(",", ".")), 2) == amount for f in supported)
        return in_facts and amount in euro_amounts(answer.cleaned["answer_text"])
    if kind == "estimated_duration":
        values = {f["value"].upper() for f in supported if f["type"] == "duration"}
        return {expected["min"], expected["max"]} <= values
    if kind == "validity":
        return any(f["type"] == "validity" and f["value"].upper() == expected["value"] for f in supported)
    if kind == "online":
        wanted = "yes" if expected["value"] else "no"
        return any(f["type"] == "online" and f["value"].lower() == wanted for f in supported)
    raise ValueError(f"unknown expected fact type: {kind}")


def score(item: dict[str, Any], answer: Answer) -> dict[str, Any]:
    relevant = set(item["expected_procedure_ids"]) | set(item["acceptable_procedure_ids"])
    cleaned, validation = answer.cleaned, answer.validation
    cited = set(cleaned["procedure_ids"]) | {opt["procedure_id"] for opt in cleaned["options"]}
    result: dict[str, Any] = {
        "id": item["id"],
        "question": item["question"],
        "category": item["category"],
        "expected_status": item["expected_status"],
        "status": answer.status,
        "status_correct": answer.status == item["expected_status"],
        "retrieval_hit": bool(relevant & set(answer.evidence.candidate_ids)) if relevant else None,
        "cited": sorted(cited),
        "invalid_citations": len(validation.invalid_procedure_ids),
        "unsupported_claims": len([f for f in validation.unsupported_facts if f["type"] != "other"])
        + len(validation.unsupported_amounts)
        + len(validation.unsupported_durations),
        "unsupported_detail": {
            "facts": validation.unsupported_facts,
            "amounts": validation.unsupported_amounts,
            "durations": [f"{n}{unit}" for n, unit in validation.unsupported_durations],
        },
        "text_grounded": len(validation.text_grounded_amounts) + len(validation.text_grounded_durations),
        "text_grounded_detail": {
            "amounts": validation.text_grounded_amounts,
            "durations": [f"{n}{unit}" for n, unit in validation.text_grounded_durations],
        },
        "warning": not validation.ok,
        "status_overridden": validation.status_overridden,
        "input_tokens": answer.generation.input_tokens,
        "output_tokens": answer.generation.output_tokens,
        "cost_usd": answer.generation.cost_usd,
        "cached": answer.generation.cached,
        "rerank_cost_usd": answer.rerank["cost_usd"] if answer.rerank else 0.0,
        "rerank_cached": answer.rerank["cached"] if answer.rerank else None,
        "timings_s": answer.timings_s,
        "text": answer.text,
    }
    if item["expected_status"] != "not_found":
        result["citation_relevant"] = bool(cited & relevant)
        if cited:
            result["citation_precision"] = len(cited & relevant) / len(cited)
    if item["expected_status"] == "answered" and len(item["expected_procedure_ids"]) > 1:
        result["all_expected_cited"] = set(item["expected_procedure_ids"]) <= cited
    result["facts"] = [{**fact, "correct": fact_correct(fact, answer, relevant)} for fact in item["expected_facts"]]
    return result


def _rate(values: list[bool]) -> float | None:
    return statistics.fmean(values) if values else None


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    fresh = [r for r in results if not r["cached"]]
    generate = sorted(r["timings_s"]["generate"] for r in fresh)
    fresh_rerank = [r for r in results if r["rerank_cached"] is False]
    rerank = sorted(r["timings_s"]["rerank"] for r in fresh_rerank)
    answer_cost = sum(r["cost_usd"] for r in fresh)
    rerank_cost = sum(r["rerank_cost_usd"] for r in results)
    by_status: dict[str, float | None] = {}
    for status in ("answered", "ambiguous", "not_found"):
        by_status[status] = _rate([r["status_correct"] for r in results if r["expected_status"] == status])
    facts = [fact["correct"] for r in results for fact in r["facts"]]
    hit = [r for r in results if r["retrieval_hit"]]
    return {
        "n": len(results),
        "status_accuracy": _rate([r["status_correct"] for r in results]),
        "status_accuracy_by_expected": by_status,
        "retrieval_hit_at_5": _rate([r["retrieval_hit"] for r in results if r["retrieval_hit"] is not None]),
        "citation_relevant": _rate([r["citation_relevant"] for r in results if "citation_relevant" in r]),
        "citation_relevant_when_retrieved": _rate([r["citation_relevant"] for r in hit if "citation_relevant" in r]),
        "citation_precision": _rate([r["citation_precision"] for r in results if "citation_precision" in r]),
        "citation_precision_when_retrieved": _rate([r["citation_precision"] for r in hit if "citation_precision" in r]),
        "invalid_citations": sum(r["invalid_citations"] for r in results),
        "fact_correct": _rate(facts),
        "fact_items": len(facts),
        "all_expected_cited": _rate([r["all_expected_cited"] for r in results if "all_expected_cited" in r]),
        "multi_procedure_items": len([r for r in results if "all_expected_cited" in r]),
        "unsupported_claims": sum(r["unsupported_claims"] for r in results),
        "text_grounded": sum(r["text_grounded"] for r in results),
        "warnings": sum(r["warning"] for r in results),
        "cost_usd": answer_cost,
        "rerank_cost_usd": rerank_cost,
        "total_cost_usd": answer_cost + rerank_cost,
        "cost_usd_per_question": statistics.fmean(r["cost_usd"] for r in results) if results else 0.0,
        "input_tokens_mean": statistics.fmean(r["input_tokens"] for r in results) if results else 0.0,
        "output_tokens_mean": statistics.fmean(r["output_tokens"] for r in results) if results else 0.0,
        "generate_latency_p50_s": statistics.median(generate) if generate else None,
        "generate_latency_max_s": generate[-1] if generate else None,
        "fresh_calls": len(fresh),
        "rerank_fresh_calls": len(fresh_rerank),
        "rerank_latency_p50_s": statistics.median(rerank) if rerank else None,
    }


def count_tokens(client: Any, user: str, model: ModelSpec) -> int:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}],
    }
    response = client.count_tokens(modelId=model.count_tokens_model_id, input={"invokeModel": {"body": json.dumps(body)}})
    return int(response["inputTokens"])


def dry_run(assistant: Assistant, items: list[dict[str, Any]], model: ModelSpec) -> None:
    """Free: S3 Vectors queries (under $0.001) and CountTokens only, never a Claude call."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name=REGION)
    reranker = assistant.reranker
    totals, rerank_totals, proxies = [], [], 0
    for item in items:
        question = item["question"]
        hits, _ = assistant.retrieve(question)
        order = None
        note = ""
        if reranker is not None:
            candidates = assistant.rerank_candidates(hits)
            order = reranker.cached_rerank(question, candidates)
            if order is None:
                proxies += 1
                note = "  (dense top 5 as proxy)"
                rerank_totals.append(count_haiku_tokens(client, haiku_request(question, candidates, reranker.cards)))
            else:
                note = "  (cached rerank)"
        evidence = build_evidence(question, hits, assistant.chunks_by_key, assistant.facts, order=order)
        tokens = count_tokens(client, user_message(evidence), model)
        totals.append(tokens)
        print(f"{item['id']}  {tokens:>6} input tokens  {len(evidence.chunks)} chunks{note}  {question}")
    input_cost = sum(totals) * model.input_usd_per_mtok / 1e6
    output_cost = len(items) * ESTIMATED_OUTPUT_TOKENS * model.output_usd_per_mtok / 1e6
    answer_cost = input_cost + output_cost
    print(
        f"\n{model.name}: {len(items)} questions, {sum(totals):,} input tokens (mean {statistics.fmean(totals):,.0f}, "
        f"max {max(totals):,}). The structured-output schema adds a little on top.\n"
        f"Answers: ${input_cost:.3f} input + ${output_cost:.3f} output "
        f"({ESTIMATED_OUTPUT_TOKENS} output tokens each) = ${answer_cost:.3f}"
    )
    if reranker is not None:
        rerank_cost = (
            sum(rerank_totals) * HAIKU_USD_PER_MTOK[0]
            + len(rerank_totals) * ESTIMATED_RERANK_OUTPUT_TOKENS * HAIKU_USD_PER_MTOK[1]
        ) / 1e6
        print(
            f"Rerank (Haiku 4.5): {len(rerank_totals)} fresh calls, {sum(rerank_totals):,} input tokens = ${rerank_cost:.3f} "
            f"({len(items) - len(rerank_totals)} cached). {proxies} answer prompts used the dense top 5 as a proxy.\n"
            f"Total: ${answer_cost + rerank_cost:.3f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate grounded answers on the eval set.")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--eval-dir", type=Path, default=Path("eval"))
    parser.add_argument("--out", type=Path, default=Path("data/eval"))
    parser.add_argument("--dry-run", action="store_true", help="count prompt tokens and quote the cost; no Claude calls")
    add_common_arguments(parser)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.split == "test":
        logger.warning("test is the held-out split: report results, never tune on them (PRD 8.1)")

    items = load_eval(args.split, args.eval_dir)
    model = ANSWER_MODELS[args.model]
    assistant = build_assistant(
        args.docs, args.facts, args.embeddings, args.vector_index, args.cache, model, args.rerank, args.rerank_cache
    )
    if args.dry_run:
        dry_run(assistant, items, model)
        return

    results = []
    for item in items:
        answer = assistant.ask(item["question"])
        results.append(score(item, answer))
        r = results[-1]
        logger.info(
            "%s %s (expected %s), $%.4f + rerank $%.4f",
            item["id"], r["status"], r["expected_status"], r["cost_usd"], r["rerank_cost_usd"],
        )
    summary = {"model": model.name, "rerank": args.rerank, **summarise(results)}
    args.out.mkdir(parents=True, exist_ok=True)
    suffix = "" if args.rerank == "none" else f"-rerank-{args.rerank}"
    out_path = args.out / f"answers-{args.split}-{model.name}{suffix}.json"
    out_path.write_text(json.dumps({"summary": summary, "items": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nPer-question results: {out_path}")


if __name__ == "__main__":
    main()
