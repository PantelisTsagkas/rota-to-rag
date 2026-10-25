import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from mitos_rag.answer import (
    ANSWER_MODELS,
    ATTRIBUTION,
    AnswerTextStream,
    Cancelled,
    FACTS_ROW_CHARS,
    RERANK_TOP_K_CHUNKS,
    TRIMMED_DESCRIPTION_CHARS,
    WARNING,
    Assistant,
    Generator,
    GenerationError,
    build_evidence,
    prompt_facts,
    render,
    request_params,
    user_message,
)
from mitos_rag.chunk import Chunk
from mitos_rag.embed import VectorCache, text_hash
from mitos_rag.pacing import Pacer
from mitos_rag.rerank import CANDIDATES, HaikuReranker
from mitos_rag.validate import validate


def facts_row(pid: str, title: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "procedure_id": pid,
        "title": title,
        "source_url": f"https://id.mitos.gov.gr/{pid}",
        "last_updated": "2026-09-14T10:57:32+03:00",
        "costs": [],
        "can_apply_online": False,
    }
    return {**row, **extra}


FACTS = {pid: facts_row(pid, f"Διαδικασία {pid}") for pid in ("a", "b", "c")}


def chunks_for(pid: str, n: int, size: int) -> dict[str, Chunk]:
    return {f"{pid}:{i}": Chunk(pid, "Δικαιολογητικά", i, "x" * size) for i in range(n)}


def test_evidence_keeps_each_candidates_best_chunk_and_fills_the_budget_by_similarity() -> None:
    chunks = {**chunks_for("a", 3, 400), **chunks_for("b", 2, 400), **chunks_for("c", 1, 400)}
    hits = [("a:2", 0.9), ("a:0", 0.85), ("b:1", 0.8), ("a:1", 0.7), ("c:0", 0.6), ("b:0", 0.5)]
    evidence = build_evidence("ερώτηση", hits, chunks, FACTS, top=2, budget=1200)

    assert evidence.candidate_ids == ["a", "b"]
    # best chunks a:2 and b:1 first, then a:0 by similarity; the budget stops a:1. Grouped by rank, then order.
    assert [f"{c.procedure_id}:{c.chunk_index}" for c in evidence.chunks] == ["a:0", "a:2", "b:1"]
    assert set(evidence.facts) == {"a", "b"}


def test_every_candidate_gets_its_best_chunk_even_over_budget() -> None:
    chunks = {**chunks_for("a", 1, 1000), **chunks_for("b", 1, 1000)}
    evidence = build_evidence("ερώτηση", [("a:0", 0.9), ("b:0", 0.8)], chunks, FACTS, top=2, budget=500)
    assert len(evidence.chunks) == 2


def test_evidence_follows_a_reranked_order_and_keeps_dense_scores() -> None:
    chunks = {**chunks_for("a", 1, 10), **chunks_for("b", 1, 10), **chunks_for("c", 1, 10)}
    hits = [("a:0", 0.9), ("b:0", 0.8), ("c:0", 0.7)]
    evidence = build_evidence("ερώτηση", hits, chunks, FACTS, top=2, order=["c", "zzz", "c", "a"])

    assert evidence.candidate_ids == ["c", "a"]  # unknown and repeated ids dropped
    assert [c.score for c in evidence.candidates] == [0.7, 0.9]
    assert [c.procedure_id for c in evidence.chunks] == ["c", "a"]


def test_dense_order_is_the_default_order() -> None:
    chunks = {**chunks_for("a", 1, 10), **chunks_for("b", 1, 10)}
    hits = [("b:0", 0.8), ("a:0", 0.9), ("x:0", 0.95)]  # x has no facts row
    default = build_evidence("ερώτηση", hits, chunks, FACTS)
    explicit = build_evidence("ερώτηση", hits, chunks, FACTS, order=["a", "b"])
    assert user_message(default) == user_message(explicit)


def test_prompt_facts_treat_min_above_max_as_unknown() -> None:
    row = facts_row("a", "Πρόστιμο", costs=[{"amount_min": 300.0, "amount_max": 30.0, "description": "πρόστιμο"}])
    cost = prompt_facts(row)["costs"][0]
    assert cost["amount_min"] is None and cost["amount_max"] is None


def test_user_message_has_candidates_facts_evidence_and_question() -> None:
    chunks = chunks_for("a", 1, 10)
    message = user_message(build_evidence("πόσο κοστίζει;", [("a:0", 0.9)], chunks, FACTS))
    for tag in ("<candidates>", "<facts>", "<evidence>", "<question>πόσο κοστίζει;</question>"):
        assert tag in message
    assert "1. a | Διαδικασία a" in message


class FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        return self.responses.pop(0)


    def stream(self, **params: Any) -> "FakeStream":
        self.calls.append(params)
        return FakeStream(self.responses.pop(0))


class FakeStream:
    """The SDK's MessageStream as the generator uses it: text pieces, then the final message."""

    def __init__(self, final: Any, piece: int = 7) -> None:
        self.final = final
        text = final.content[0].text
        self.text_stream = [text[i : i + piece] for i in range(0, len(text), piece)]

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self.final


def response(payload: dict[str, Any], stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=9000, output_tokens=600),
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_generator_caches_responses_and_paces_fresh_calls(tmp_path: Path) -> None:
    messages = FakeMessages([response({"status": "answered"}), response({"status": "not_found"})])
    clock = Clock()
    haiku = ANSWER_MODELS["haiku-4.5"]
    generator = Generator(SimpleNamespace(messages=messages), tmp_path, haiku, sleep=clock.sleep, clock=clock)

    first = generator.generate("ερώτηση 1")
    again = generator.generate("ερώτηση 1")
    second = generator.generate("ερώτηση 2")

    assert first.output == again.output == {"status": "answered"}
    assert not first.cached and again.cached and not second.cached
    assert len(messages.calls) == 2  # the repeat came from disk
    assert clock.slept == [pytest.approx(6.5)]  # 10 requests per minute, plus a margin
    assert first.cost_usd == pytest.approx((9000 * 1.10 + 600 * 5.50) / 1e6)
    assert messages.calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert messages.calls[0]["extra_body"] == {"temperature": 0}


def test_generator_refuses_truncated_or_non_json_responses(tmp_path: Path) -> None:
    truncated = Generator(SimpleNamespace(messages=FakeMessages([response({}, stop_reason="max_tokens")])), None)
    with pytest.raises(GenerationError):
        truncated.generate("ερώτηση")
    bad = SimpleNamespace(content=[SimpleNamespace(type="text", text="όχι json")], stop_reason="end_turn", usage=None)
    with pytest.raises(GenerationError):
        Generator(SimpleNamespace(messages=FakeMessages([bad])), None).generate("ερώτηση")


def test_cache_key_changes_with_the_prompt() -> None:
    assert request_params("α") != request_params("β")


def test_each_model_has_its_own_cache_entry_and_prices(tmp_path: Path) -> None:
    sonnet, haiku = ANSWER_MODELS["sonnet-4.6"], ANSWER_MODELS["haiku-4.5"]
    assert request_params("α", sonnet) != request_params("α", haiku)
    haiku_messages = FakeMessages([response({"status": "answered"})])
    sonnet_messages = FakeMessages([response({"status": "not_found"})])

    haiku_answer = Generator(SimpleNamespace(messages=haiku_messages), tmp_path, haiku).generate("ερώτηση")
    sonnet_generator = Generator(SimpleNamespace(messages=sonnet_messages), tmp_path, sonnet)
    sonnet_answer = sonnet_generator.generate("ερώτηση")
    sonnet_again = sonnet_generator.generate("ερώτηση")

    assert sonnet_messages.calls[0]["model"] == "eu.anthropic.claude-sonnet-4-6"
    assert not sonnet_answer.cached and sonnet_again.cached  # the Haiku entry was not reused
    assert sonnet_answer.output == {"status": "not_found"} != haiku_answer.output
    assert sonnet_again.cost_usd == pytest.approx((9000 * 3.30 + 600 * 16.50) / 1e6)


def test_render_adds_sources_dates_attribution_and_a_warning_when_checks_fail() -> None:
    facts = {"a": facts_row("a", "Έκδοση Διαβατηρίου", costs=[{"amount_min": 80.0, "amount_max": 80.0}])}
    good = {
        "status": "answered",
        "answer_text": "Κοστίζει 80 €.",
        "procedure_ids": ["a"],
        "facts_used": [],
        "clarifying_question": "",
        "options": [],
    }
    cleaned, result = validate(good, ["a"], facts)
    text = render(cleaned, result, facts)
    assert "Πηγές:\n- Έκδοση Διαβατηρίου: https://id.mitos.gov.gr/a (τελευταία ενημέρωση 2026-09-14)" in text
    assert text.endswith(ATTRIBUTION)
    assert WARNING not in text

    cleaned, result = validate({**good, "answer_text": "Κοστίζει 90 €."}, ["a"], facts)
    assert WARNING in render(cleaned, result, facts)


def test_render_shows_the_clarifying_question_and_options() -> None:
    ambiguous = {
        "status": "ambiguous",
        "answer_text": "Υπάρχουν δύο διαδικασίες.",
        "procedure_ids": [],
        "facts_used": [],
        "clarifying_question": "Είναι πρώτη έκδοση ή απώλεια;",
        "options": [{"label": "Πρώτη έκδοση", "procedure_id": "a"}, {"label": "Απώλεια", "procedure_id": "b"}],
    }
    cleaned, result = validate(ambiguous, ["a", "b"], FACTS)
    text = render(cleaned, result, FACTS)
    assert "Είναι πρώτη έκδοση ή απώλεια;\n\n1. Πρώτη έκδοση\n2. Απώλεια" in text
    assert "https://id.mitos.gov.gr/b" in text


class FakeRetriever:
    def __init__(self, queries: VectorCache, hits: list[tuple[str, float]]) -> None:
        self.queries = queries
        self.hits = hits
        self.calls: list[tuple[Any, ...]] = []

    def nearest_chunks(self, vector: Any, *args: Any) -> list[tuple[str, float]]:
        self.calls.append(args)
        return self.hits


class FakeRanking:
    """Bedrock Messages for the reranker: always returns the same ranking."""

    def __init__(self, ranking: list[str]) -> None:
        self.ranking = ranking
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        text = json.dumps({"ranking": self.ranking})
        usage = SimpleNamespace(input_tokens=3000, output_tokens=100)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=usage)


def assistant_with(tmp_path: Path, reranker: HaikuReranker | None) -> tuple[Assistant, FakeRetriever, FakeMessages]:
    queries = VectorCache(tmp_path / "query")
    queries.add([text_hash("ερώτηση")], np.array([[1.0, 0.0]], dtype=np.float32))
    chunks = {**chunks_for("a", 1, 10), **chunks_for("b", 1, 10), **chunks_for("c", 1, 10)}
    retriever = FakeRetriever(queries, [("a:0", 0.9), ("b:0", 0.8), ("c:0", 0.7)])
    answer = {
        "status": "answered", "answer_text": "Απάντηση.", "procedure_ids": ["c"], "facts_used": [],
        "missing_facts": [], "clarifying_question": "", "options": [],
    }
    messages = FakeMessages([response(answer), response(answer)])
    generator = Generator(SimpleNamespace(messages=messages), None, sleep=lambda _: None)
    return Assistant(retriever, None, chunks, FACTS, generator, reranker), retriever, messages  # type: ignore[arg-type]


def test_without_a_reranker_the_dense_path_is_unchanged(tmp_path: Path) -> None:
    assistant, retriever, messages = assistant_with(tmp_path, None)
    answer = assistant.ask("ερώτηση")

    assert retriever.calls == [()]  # the default top 100 chunks, as in 006 and 007
    assert answer.evidence.candidate_ids == ["a", "b", "c"]
    assert answer.rerank is None and "rerank" not in answer.timings_s
    assert messages.calls[0]["messages"][0]["content"].startswith("<candidates>\n1. a |")


def test_the_reranker_orders_the_candidates_and_its_cost_is_counted(tmp_path: Path) -> None:
    ranking = FakeRanking(["c", "a"])
    cards = {pid: f"Διαδικασία {pid}" for pid in FACTS}
    reranker = HaikuReranker(SimpleNamespace(messages=ranking), cards, tmp_path / "rerank", sleep=lambda _: None)
    assistant, retriever, _ = assistant_with(tmp_path, reranker)

    answer = assistant.ask("ερώτηση")
    again = assistant.ask("ερώτηση")

    assert retriever.calls[0] == (RERANK_TOP_K_CHUNKS, CANDIDATES)
    assert "1. a |" in ranking.calls[0]["messages"][0]["content"]  # the reranker saw the dense order
    assert answer.evidence.candidate_ids == ["c", "a", "b"]
    assert answer.rerank == {"name": "haiku", "cost_usd": pytest.approx((3000 * 1.10 + 100 * 5.50) / 1e6), "cached": False}
    assert answer.cost_usd == pytest.approx(answer.generation.cost_usd + answer.rerank["cost_usd"])
    assert "rerank" in answer.timings_s
    assert again.rerank == {"name": "haiku", "cost_usd": 0.0, "cached": True}
    assert len(ranking.calls) == 1


def test_a_shared_pacer_spaces_rerank_and_answer_calls_on_one_quota(tmp_path: Path) -> None:
    clock = Clock()
    pacer = Pacer(10, clock.sleep, clock)
    cards = {pid: f"Διαδικασία {pid}" for pid in FACTS}
    reranker = HaikuReranker(SimpleNamespace(messages=FakeRanking(["c"])), cards, None, pacer=pacer)
    assistant, _, _ = assistant_with(tmp_path, reranker)
    assistant.generator.pacer = pacer

    answer = assistant.ask("ερώτηση")
    assert clock.slept == [pytest.approx(6.5)]  # the answer waited for the rerank call
    assert answer.timings_s["rerank"] == 0.0  # the fake call takes no time on this clock


def test_rerank_latency_leaves_out_the_wait_for_the_quota(tmp_path: Path) -> None:
    clock = Clock()
    pacer = Pacer(10, clock.sleep, clock)
    pacer.wait()  # an answer call just went out
    cards = {pid: f"Διαδικασία {pid}" for pid in FACTS}
    reranker = HaikuReranker(SimpleNamespace(messages=FakeRanking(["c"])), cards, None, pacer=pacer)
    assistant, _, _ = assistant_with(tmp_path, reranker)

    answer = assistant.ask("ερώτηση")
    assert clock.slept[0] == pytest.approx(6.5)
    assert answer.timings_s["rerank"] == 0.0


def test_the_default_answer_model_is_sonnet_and_render_drops_em_dashes() -> None:
    assert request_params("α")["model"] == "eu.anthropic.claude-sonnet-4-6"
    output = {
        "status": "answered", "answer_text": "Κόστος \u2014 71 €.", "procedure_ids": ["a"], "facts_used": [],
        "missing_facts": [], "clarifying_question": "", "options": [],
    }
    cleaned, validation = validate(output, ["a"], FACTS)
    text = render(cleaned, validation, FACTS)
    assert "\u2014" not in text and "Κόστος - 71 €." in text


def test_the_checker_reads_the_cited_procedures_own_text(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "c.md").write_text("# Διαδικασία c\n\nΔιάρκεια 13 μηνών.\n", encoding="utf-8")
    assistant, _, messages = assistant_with(tmp_path, None)
    assistant.docs_dir = docs
    assert assistant.procedure_texts(["c", "missing"]) == {"c": "# Διαδικασία c\n\nΔιάρκεια 13 μηνών."}

    messages.responses = [
        response({
            "status": "answered", "answer_text": "Ισχύει για 13 μήνες.", "procedure_ids": ["c"], "facts_used": [],
            "missing_facts": [], "clarifying_question": "", "options": [],
        })
    ]
    answer = assistant.ask("ερώτηση")
    assert answer.validation.ok
    assert answer.validation.text_grounded_durations == [(13, "M")]


def fine(n: int, description: str = "Πρόστιμο για παράβαση") -> dict[str, Any]:
    return {"num_id": n, "cost_type": "Πρόστιμο", "amount_min": 100.0 + n, "amount_max": 100.0 + n, "description": description}


def test_a_facts_row_under_budget_is_unchanged() -> None:
    row = facts_row("a", "Διαδικασία a", costs=[fine(1), fine(2, "x" * 2000)])
    shown = prompt_facts(row)
    assert "costs_total" not in shown
    assert shown["costs"][1]["description"] == "x" * 2000  # long, but the row fits: not trimmed


def test_a_fines_registry_is_capped_and_says_how_many_fees_it_shows() -> None:
    row = facts_row("a", "Διαδικασία a", costs=[fine(n, "Παράβαση άρθρου " + "λ" * 400) for n in range(98)])
    shown = prompt_facts(row)
    assert len(json.dumps(shown, ensure_ascii=False)) <= FACTS_ROW_CHARS
    assert shown["costs_total"] == 98
    assert shown["costs_shown"] == len(shown["costs"]) < 98
    assert [c["num_id"] for c in shown["costs"]] == list(range(shown["costs_shown"]))  # source order kept
    assert f"{shown['costs_shown']} από 98" in shown["costs_note"]


def test_long_descriptions_are_trimmed_before_any_fee_is_dropped() -> None:
    # Like 125220: five fees with very long descriptions.
    row = facts_row("a", "Διαδικασία a", costs=[fine(n, "δ" * 1100) for n in range(5)])
    shown = prompt_facts(row)
    assert "costs_total" not in shown
    assert all(len(c["description"]) == TRIMMED_DESCRIPTION_CHARS + 1 for c in shown["costs"])
    assert shown["costs"][0]["description"].endswith("…")


def test_the_cap_keeps_the_min_above_max_rule() -> None:
    costs = [{**fine(n, "λ" * 400), "amount_min": 500.0, "amount_max": 50.0} for n in range(40)]
    shown = prompt_facts(facts_row("a", "Διαδικασία a", costs=costs))
    assert shown["costs_total"] == 40
    assert all(c["amount_min"] is None and c["amount_max"] is None for c in shown["costs"])


def test_a_stop_before_the_rerank_skips_every_model_call(tmp_path: Path) -> None:
    ranking = FakeRanking(["c", "a"])
    cards = {pid: f"Διαδικασία {pid}" for pid in FACTS}
    reranker = HaikuReranker(SimpleNamespace(messages=ranking), cards, None, sleep=lambda _: None)
    assistant, _, messages = assistant_with(tmp_path, reranker)

    with pytest.raises(Cancelled):
        assistant.ask("ερώτηση", stop=lambda: True)
    assert ranking.calls == [] and messages.calls == []


def test_a_stop_after_the_rerank_skips_the_answer_call(tmp_path: Path) -> None:
    ranking = FakeRanking(["c", "a"])
    cards = {pid: f"Διαδικασία {pid}" for pid in FACTS}
    reranker = HaikuReranker(SimpleNamespace(messages=ranking), cards, None, sleep=lambda _: None)
    assistant, _, messages = assistant_with(tmp_path, reranker)
    checks = iter([False, True])  # still there before the rerank, gone before the answer

    with pytest.raises(Cancelled):
        assistant.ask("ερώτηση", stop=lambda: next(checks))
    assert len(ranking.calls) == 1 and messages.calls == []


STREAMED = {
    "status": "answered",
    "answer_text": 'Κοστίζει 80 € — με "e-Παράβολο".\n\n- Θυρίδα \\ 2 😀',
    "procedure_ids": ["c"],
    "facts_used": [],
    "missing_facts": [],
    "clarifying_question": "",
    "options": [],
}


def test_answer_text_is_read_from_partial_json_at_every_split_point() -> None:
    expected = STREAMED["answer_text"]
    # Raw Greek and \uXXXX escapes (with a surrogate pair), with and without spaces after ":".
    for full in (
        json.dumps(STREAMED, ensure_ascii=False),
        json.dumps(STREAMED, ensure_ascii=True),
        json.dumps(STREAMED, ensure_ascii=False, separators=(",", ":")),
    ):
        for cut in range(len(full) + 1):
            reader = AnswerTextStream()
            assert reader.feed(full[:cut]) + reader.feed(full[cut:]) == expected
        reader = AnswerTextStream()
        assert "".join(reader.feed(char) for char in full) == expected
        assert reader.done


def test_a_streamed_call_sends_the_same_request_and_keeps_the_cache_key(tmp_path: Path) -> None:
    messages = FakeMessages([response(STREAMED), response(STREAMED)])
    pieces: list[str] = []
    streamed = Generator(SimpleNamespace(messages=messages), tmp_path / "a", sleep=lambda _: None)
    plain = Generator(SimpleNamespace(messages=messages), tmp_path / "b", sleep=lambda _: None)

    first = streamed.generate("ερώτηση", on_text=pieces.append)
    second = plain.generate("ερώτηση")

    assert messages.calls[0] == messages.calls[1]  # streaming changes nothing in the request
    assert "".join(pieces) == json.dumps(STREAMED, ensure_ascii=False)
    assert first.output == second.output == STREAMED
    assert first.first_token_s is not None and second.first_token_s is None
    assert [p.name for p in (tmp_path / "a").iterdir()] == [p.name for p in (tmp_path / "b").iterdir()]


def test_ask_with_events_sends_the_found_titles_then_the_answer_text(tmp_path: Path) -> None:
    assistant, _, messages = assistant_with(tmp_path, None)
    messages.responses[0] = response(STREAMED)
    events: list[dict[str, Any]] = []

    answer = assistant.ask("ερώτηση", on_event=events.append)

    assert events[0] == {"type": "found", "titles": ["Διαδικασία a", "Διαδικασία b", "Διαδικασία c"]}
    assert {event["type"] for event in events[1:]} == {"text"}
    streamed = "".join(event["text"] for event in events[1:])
    assert streamed == STREAMED["answer_text"].replace("—", "-")  # no em dashes reach the user
    assert "first_token" in answer.timings_s


def test_ask_without_events_does_not_stream(tmp_path: Path) -> None:
    assistant, _, messages = assistant_with(tmp_path, None)
    answer = assistant.ask("ερώτηση")
    assert "first_token" not in answer.timings_s and answer.generation.first_token_s is None


def test_a_stop_while_the_answer_streams_closes_the_stream(tmp_path: Path, monkeypatch: Any) -> None:
    import mitos_rag.answer as answer_module

    monkeypatch.setattr(answer_module, "STOP_CHECK_S", 0.0)  # check on every piece
    assistant, _, messages = assistant_with(tmp_path, None)
    messages.responses[0] = response(STREAMED)
    checks: list[bool] = []

    def stop() -> bool:
        checks.append(True)
        return len(checks) > 3  # still there before the answer call and for two pieces

    events: list[dict[str, Any]] = []
    with pytest.raises(Cancelled):
        assistant.ask("ερώτηση", stop=stop, on_event=events.append)
    assert events[0]["type"] == "found" and len(checks) == 4
