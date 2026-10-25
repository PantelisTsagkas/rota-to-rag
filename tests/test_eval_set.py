from collections import Counter
from pathlib import Path

import pytest

from mitos_rag.evaluate import load_eval

EVAL_DIR = Path(__file__).parent.parent / "eval"
CATEGORIES = {"exact_lookup", "natural_greek", "fact_extraction", "multi_procedure", "ambiguous", "out_of_scope"}
STATUSES = {"answered", "ambiguous", "not_found"}
FACT_TYPES = {"cost", "estimated_duration", "validity", "online"}


def load(split: str) -> list[dict]:
    return load_eval(split, EVAL_DIR)


@pytest.mark.parametrize("split", ["dev", "test"])
def test_items_are_well_formed(split: str) -> None:
    for item in load(split):
        assert item["category"] in CATEGORIES, item["id"]
        assert item["expected_status"] in STATUSES, item["id"]
        assert all(fact["type"] in FACT_TYPES for fact in item["expected_facts"]), item["id"]
        if item["expected_status"] == "answered":
            assert item["expected_procedure_ids"], item["id"]
        if item["expected_status"] == "not_found":
            assert not item["expected_procedure_ids"] and not item["acceptable_procedure_ids"], item["id"]
        if item["expected_facts"]:
            assert len(item["expected_procedure_ids"]) == 1, item["id"]


def test_ids_and_questions_are_unique_and_splits_do_not_overlap() -> None:
    items = load("dev") + load("test")
    assert len({item["id"] for item in items}) == len(items)
    assert len({item["question"] for item in items}) == len(items)


def test_every_category_is_in_both_splits() -> None:
    for split in ("dev", "test"):
        assert set(Counter(item["category"] for item in load(split))) == CATEGORIES, split
