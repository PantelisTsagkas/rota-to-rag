from typing import Any

import pytest

from mitos_rag.validate import euro_amounts, validate

# Shaped like real facts.jsonl rows (144400 passport, 977354 criminal record), trimmed.
PASSPORT: dict[str, Any] = {
    "procedure_id": "144400",
    "costs": [
        {"amount_min": 80.0, "amount_max": 80.0, "description": "πάνω από 14 ετών"},
        {"amount_min": 71.0, "amount_max": 71.0, "description": "έως 14 ετών"},
        {"amount_min": 62.5, "amount_max": 62.5, "description": "8 μηνών"},
    ],
    "deadline": "P5D",
    "estimated_min": None,
    "estimated_max": None,
    "validity": None,
    "can_apply_online": False,
}
RECORD: dict[str, Any] = {
    "procedure_id": "977354",
    "costs": [],
    "deadline": None,
    "estimated_min": "P2D",
    "estimated_max": "P3D",
    "validity": "P3M",
    "can_apply_online": True,
}
FACTS = {"144400": PASSPORT, "977354": RECORD}


def output(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "answered",
        "answer_text": "Για παιδί έως 14 ετών το παράβολο είναι 71 €.",
        "procedure_ids": ["144400"],
        "facts_used": [{"type": "cost", "value": "71", "procedure_id": "144400"}],
        "missing_facts": [],
        "clarifying_question": "",
        "options": [],
    }
    return {**base, **overrides}


@pytest.mark.parametrize(
    "text, amounts",
    [
        ("80 € ή 71€", [80.0, 71.0]),
        ("62,5 € και 5.87 €", [62.5, 5.87]),
        ("1.250,00 ευρώ", [1250.0]),
        ("έως 14 ετών, έτος 2026, 10 ημέρες", []),
        ("άνω των τριάντα (30) ευρώ", [30.0]),
    ],
)
def test_euro_amounts_read_greek_notation(text: str, amounts: list[float]) -> None:
    assert euro_amounts(text) == amounts


def test_a_correct_answer_passes() -> None:
    cleaned, result = validate(output(), ["144400", "977354"], FACTS)
    assert result.ok
    assert cleaned["status"] == "answered"
    assert result.supported_facts == [{"type": "cost", "value": "71", "procedure_id": "144400"}]


def test_a_wrong_amount_is_caught_in_facts_and_text() -> None:
    wrong = output(
        answer_text="Κοστίζει 75 €.", facts_used=[{"type": "cost", "value": "75", "procedure_id": "144400"}]
    )
    _, result = validate(wrong, ["144400"], FACTS)
    assert not result.ok
    assert result.unsupported_amounts == [75.0]
    assert result.unsupported_facts[0]["value"] == "75"


def test_durations_validity_and_online_are_checked_against_their_fields() -> None:
    facts_used = [
        {"type": "duration", "value": "P2D", "procedure_id": "977354"},
        {"type": "duration", "value": "P3D", "procedure_id": "977354"},
        {"type": "validity", "value": "P3M", "procedure_id": "977354"},
        {"type": "online", "value": "yes", "procedure_id": "977354"},
        {"type": "duration", "value": "P5D", "procedure_id": "977354"},  # the passport's, not this one's
        {"type": "online", "value": "yes", "procedure_id": "144400"},  # the passport is in person only
    ]
    _, result = validate(
        output(answer_text="Σε 2 με 3 ημέρες.", procedure_ids=["977354"], facts_used=facts_used), ["977354", "144400"], FACTS
    )
    assert [f["value"] for f in result.supported_facts] == ["P2D", "P3D", "P3M", "yes"]
    assert result.unsupported_facts == facts_used[4:]


def test_ids_that_were_not_retrieved_are_dropped_and_answered_falls_back_to_not_found() -> None:
    cleaned, result = validate(output(procedure_ids=["999999"], facts_used=[]), ["144400"], FACTS)
    assert result.invalid_procedure_ids == ["999999"]
    assert cleaned["procedure_ids"] == []
    assert cleaned["status"] == "not_found"
    assert result.status_overridden


def test_ambiguous_needs_two_valid_options() -> None:
    options = [{"label": "Πρώτη έκδοση", "procedure_id": "144400"}, {"label": "Άλλη", "procedure_id": "000000"}]
    cleaned, result = validate(output(status="ambiguous", options=options, facts_used=[]), ["144400"], FACTS)
    assert cleaned["options"] == options[:1]
    assert cleaned["status"] == "answered"
    assert result.invalid_procedure_ids == ["000000"]


def test_links_written_by_the_model_are_removed() -> None:
    cleaned, result = validate(output(answer_text="Δείτε https://example.gr/x για 71 €."), ["144400"], FACTS)
    assert result.had_links and not result.ok
    assert "http" not in cleaned["answer_text"]


def test_both_ends_of_a_range_are_checked() -> None:
    assert sorted(euro_amounts("Κόστος: 14,50-19,50 €")) == [14.5, 19.5]


def test_free_is_supported_only_when_mitos_says_free() -> None:
    free = {**RECORD, "is_free": True}
    unknown = {**RECORD, "is_free": False}
    fact = {"type": "cost", "value": "0", "procedure_id": "977354"}
    answer = output(answer_text="Είναι δωρεάν.", procedure_ids=["977354"], facts_used=[fact])
    assert validate(answer, ["977354"], {"977354": free})[1].ok
    assert not validate(answer, ["977354"], {"977354": unknown})[1].ok


def test_durations_in_the_text_must_be_facts_of_a_cited_procedure() -> None:
    answer = output(
        answer_text="Βγαίνει σε 2-3 ημέρες και ισχύει 3 μήνες. Για παιδιά έως 14 ετών.",
        procedure_ids=["977354"],
        facts_used=[],
    )
    assert validate(answer, ["977354"], FACTS)[1].ok
    wrong = output(answer_text="Βγαίνει σε 10 ημέρες.", procedure_ids=["977354"], facts_used=[])
    assert validate(wrong, ["977354"], FACTS)[1].unsupported_durations == [(10, "D")]


# Lines from the rendered 144400 and 439993 docs (data/docs), trimmed.
TEXTS = {
    "144400": "- e-Παράβολο: 67 € (Κατ' εξαίρεση έκδοση διαβατ. διάρκειας 13 μηνών) [εναλλακτικά του #1]\n"
    "Η απαγόρευση ισχύει για 5 έτη από την έκδοση της καταδικαστικής απόφασης.",
    "977354": "Να έχουν τακτοποιηθεί ληξιπρόθεσμες οφειλές άνω των 30€.",
}


def test_a_number_written_only_in_a_cited_procedures_text_is_text_grounded() -> None:
    answer = output(answer_text="Υπάρχει και διαβατήριο διάρκειας 13 μηνών.", facts_used=[])
    _, result = validate(answer, ["144400"], FACTS, TEXTS)
    assert result.ok
    assert result.text_grounded_durations == [(13, "M")]
    assert result.unsupported_durations == []

    debts = output(answer_text="Οφειλές άνω των 30 € πρέπει να ρυθμιστούν.", procedure_ids=["977354"], facts_used=[])
    _, result = validate(debts, ["977354"], FACTS, TEXTS)
    assert result.ok and result.text_grounded_amounts == [30.0]


def test_a_number_in_neither_the_facts_nor_the_text_still_warns() -> None:
    _, result = validate(output(answer_text="Κοστίζει 75 € και βγαίνει σε 9 μήνες.", facts_used=[]), ["144400"], FACTS, TEXTS)
    assert not result.ok
    assert result.unsupported_amounts == [75.0]
    assert result.unsupported_durations == [(9, "M")]


def test_only_the_text_of_a_cited_procedure_counts() -> None:
    # 30 € is in 977354's text, but the answer cites only the passport.
    _, result = validate(output(answer_text="Οφειλές άνω των 30 €.", facts_used=[]), ["144400", "977354"], FACTS, TEXTS)
    assert result.unsupported_amounts == [30.0]


def test_a_total_is_never_text_grounded() -> None:
    answer = output(answer_text="- Παράβολο: 37 €\n- Σύνολο: περίπου 67 €", facts_used=[])
    texts = {"144400": "Σύνολο 67 € και 37 €"}
    _, result = validate(answer, ["144400"], {"144400": {**PASSPORT, "costs": []}}, texts)
    assert result.text_grounded_amounts == [37.0]
    assert result.unsupported_amounts == [67.0]
    # "συνολικά" is caught too, with or without accents
    _, result = validate(output(answer_text="Συνολικα 67 €.", facts_used=[]), ["144400"], {"144400": {**PASSPORT, "costs": []}}, texts)
    assert result.unsupported_amounts == [67.0]


def test_a_total_that_is_in_the_facts_row_still_passes() -> None:
    _, result = validate(output(answer_text="Σύνολο: 80 €.", facts_used=[]), ["144400"], FACTS, TEXTS)
    assert result.ok


def test_a_typed_fact_is_never_rescued_by_the_text() -> None:
    fact = {"type": "validity", "value": "P13M", "procedure_id": "144400"}
    _, result = validate(output(answer_text="Ισχύει 13 μήνες.", facts_used=[fact]), ["144400"], FACTS, TEXTS)
    assert result.unsupported_facts == [fact]
    assert not result.ok


def test_without_texts_the_check_is_the_strict_one() -> None:
    answer = output(answer_text="Υπάρχει και διαβατήριο διάρκειας 13 μηνών.", facts_used=[])
    _, result = validate(answer, ["144400"], FACTS)
    assert result.unsupported_durations == [(13, "M")]
    assert result.text_grounded_durations == []


def test_numbers_in_the_legal_style_are_read() -> None:
    from mitos_rag.validate import text_durations

    assert text_durations("επί έξι (6) μήνες τουλάχιστον") == [(6, "M")]
    assert text_durations("από (2) έως (3) ημέρες") == [(2, "D"), (3, "D")]
