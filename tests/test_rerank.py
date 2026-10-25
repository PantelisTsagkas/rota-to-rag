import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mitos_rag.rerank import CohereReranker, HaikuReranker, card, merge, score_item

CARDS = {"a": "Ανανέωση άδειας διαμονής", "b": "Ανανέωση άδειας οδήγησης | Ανανέωση κατηγοριών άδειας οδήγησης", "c": "Έκδοση Διαβατηρίου"}


def test_card_keeps_title_and_alternative_titles_only() -> None:
    markdown = (
        "# Ανανέωση άδειας οδήγησης\n\n- Κωδικός Μίτος: 664541\n\n"
        "## Άλλες ονομασίες\n\nΑνανέωση κατηγοριών ελληνικής άδειας οδήγησης\n\n"
        "## Περιγραφή\n\nΜεγάλο κείμενο.\n"
    )
    assert card(markdown) == "Ανανέωση άδειας οδήγησης | Ανανέωση κατηγοριών ελληνικής άδειας οδήγησης"


def test_merge_drops_unknown_and_repeated_ids_and_keeps_the_rest_in_dense_order() -> None:
    assert merge(["c", "x", "c", "a"], ["a", "b", "c"]) == ["c", "a", "b"]


class FakeMessages:
    def __init__(self, ranking: list[str]) -> None:
        self.ranking = ranking
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        text = json.dumps({"ranking": self.ranking})
        usage = SimpleNamespace(input_tokens=4000, output_tokens=50)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=usage)


def test_haiku_reranker_orders_caches_and_prices(tmp_path: Path) -> None:
    messages = FakeMessages(["b", "zzz"])
    slept: list[float] = []
    reranker = HaikuReranker(SimpleNamespace(messages=messages), CARDS, tmp_path, sleep=slept.append)

    first = reranker.rerank("ανανεωση διπλωματος", ["a", "b", "c"])
    again = reranker.rerank("ανανεωση διπλωματος", ["a", "b", "c"])

    assert first == again == ["b", "a", "c"]
    assert len(messages.calls) == 1  # the repeat came from disk
    assert reranker.cost_usd == pytest.approx((4000 * 1.10 + 50 * 5.50) / 1e6)
    assert "2. b | Ανανέωση άδειας οδήγησης" in messages.calls[0]["messages"][0]["content"]
    assert messages.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_cohere_reranker_maps_result_indices_to_ids(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def invoke_model(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        results = [{"index": 0, "relevance_score": 0.2}, {"index": 2, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.5}]
        return {"body": io.BytesIO(json.dumps({"results": results}).encode())}

    reranker = CohereReranker(SimpleNamespace(invoke_model=invoke_model), CARDS, tmp_path)
    assert reranker.rerank("διαβατηριο", ["a", "b", "c"]) == ["c", "b", "a"]
    body = json.loads(calls[0]["body"])
    assert body["api_version"] == 2 and body["documents"][2] == "Έκδοση Διαβατηρίου"
    assert reranker.cost_usd == pytest.approx(0.002)


def test_score_item_needs_every_expected_procedure_for_multi_questions() -> None:
    item = {"id": "q21", "question": "", "expected_procedure_ids": ["x", "y"], "acceptable_procedure_ids": []}
    partial = score_item(item, ["x", "a", "b", "c", "d", "y"])
    assert partial["hit_at_5"] and partial["rank"] == 1
    assert not partial["all_expected_at_5"]
    assert score_item(item, ["a", "y", "x"])["all_expected_at_5"]
