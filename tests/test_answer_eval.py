from typing import Any

from mitos_rag.answer import Answer, Candidate, Evidence, Generation
from mitos_rag.answer_eval import score, summarise
from mitos_rag.validate import validate

PASSPORT = {
    "procedure_id": "144400",
    "title": "Έκδοση Ελληνικού Διαβατηρίου",
    "costs": [{"amount_min": 80.0, "amount_max": 80.0}, {"amount_min": 71.0, "amount_max": 71.0}],
    "deadline": "P5D",
    "can_apply_online": False,
}
ITEM: dict[str, Any] = {
    "id": "q14",
    "question": "ποσο κοστιζει διαβατηριο για παιδι 10 χρονων",
    "category": "fact_extraction",
    "expected_status": "answered",
    "expected_procedure_ids": ["144400"],
    "acceptable_procedure_ids": [],
    "expected_facts": [{"type": "cost", "amount": 71.0}],
}


def answer_for(output: dict[str, Any], cached: bool = False, texts: dict[str, str] | None = None) -> Answer:
    evidence = Evidence(ITEM["question"], [Candidate("144400", PASSPORT["title"], 0.7)], [], {"144400": PASSPORT})
    cleaned, validation = validate(output, evidence.candidate_ids, evidence.facts, texts)
    generation = Generation(output, 9000, 600, 3.2, cached)
    return Answer(ITEM["question"], cleaned["status"], "", evidence, output, cleaned, validation, generation, {"generate": 3.2})


def output(text: str, value: str) -> dict[str, Any]:
    return {
        "status": "answered",
        "answer_text": text,
        "procedure_ids": ["144400"],
        "facts_used": [{"type": "cost", "value": value, "procedure_id": "144400"}],
        "missing_facts": [],
        "clarifying_question": "",
        "options": [],
    }


def test_a_correct_fact_needs_the_validated_typed_fact_and_the_amount_in_the_text() -> None:
    right = score(ITEM, answer_for(output("Για παιδί έως 14 ετών: 71 €.", "71")))
    assert right["status_correct"] and right["retrieval_hit"] and right["citation_relevant"]
    assert right["facts"][0]["correct"]
    assert right["unsupported_claims"] == 0

    adult_price = score(ITEM, answer_for(output("Κοστίζει 80 €.", "80")))
    assert not adult_price["facts"][0]["correct"]  # a real amount, but not the one this question needs
    assert adult_price["unsupported_claims"] == 0

    invented = score(ITEM, answer_for(output("Κοστίζει 75 €.", "75")))
    assert invented["unsupported_claims"] == 2  # the typed fact and the amount in the text


def test_summary_counts_cost_only_for_fresh_calls() -> None:
    results = [
        score(ITEM, answer_for(output("71 €.", "71"))),
        score(ITEM, answer_for(output("71 €.", "71"), cached=True)),
    ]
    summary = summarise(results)
    assert summary["fresh_calls"] == 1
    assert summary["cost_usd"] == results[0]["cost_usd"]
    assert summary["status_accuracy"] == 1.0
    assert summary["fact_correct"] == 1.0


def test_summary_adds_fresh_rerank_cost_and_latency() -> None:
    fresh, cached = answer_for(output("71 €.", "71")), answer_for(output("71 €.", "71"), cached=True)
    fresh.rerank, fresh.timings_s["rerank"] = {"name": "haiku", "cost_usd": 0.004, "cached": False}, 1.5
    cached.rerank, cached.timings_s["rerank"] = {"name": "haiku", "cost_usd": 0.0, "cached": True}, 0.0
    summary = summarise([score(ITEM, fresh), score(ITEM, cached)])

    assert summary["rerank_cost_usd"] == 0.004
    assert summary["total_cost_usd"] == summary["cost_usd"] + 0.004
    assert summary["rerank_fresh_calls"] == 1
    assert summary["rerank_latency_p50_s"] == 1.5  # cached reranks do not count


def test_text_grounded_numbers_are_counted_apart_and_show_no_warning() -> None:
    text = "Για παιδί έως 14 ετών: 71 €. Υπάρχει και διαβατήριο διάρκειας 13 μηνών."
    grounded = score(ITEM, answer_for(output(text, "71"), texts={"144400": "διάρκειας 13 μηνών"}))
    assert grounded["unsupported_claims"] == 0 and grounded["text_grounded"] == 1
    assert grounded["text_grounded_detail"] == {"amounts": [], "durations": ["13M"]}
    assert not grounded["warning"]

    strict = score(ITEM, answer_for(output(text, "71")))
    assert strict["unsupported_claims"] == 1 and strict["warning"]
    assert summarise([grounded, strict])["warnings"] == 1


def test_a_multi_procedure_item_needs_every_expected_procedure_cited() -> None:
    item = {**ITEM, "expected_procedure_ids": ["144400", "199437"]}
    result = score(item, answer_for(output("Για παιδί έως 14 ετών: 71 €.", "71")))
    assert result["citation_relevant"] and not result["all_expected_cited"]
    assert "all_expected_cited" not in score(ITEM, answer_for(output("71 €.", "71")))
    ambiguous = {**item, "expected_status": "ambiguous"}
    assert "all_expected_cited" not in score(ambiguous, answer_for(output("71 €.", "71")))
    summary = summarise([result])
    assert summary["all_expected_cited"] == 0.0 and summary["multi_procedure_items"] == 1
