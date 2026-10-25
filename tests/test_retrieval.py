import json
from pathlib import Path

import pytest

from mitos_rag.bm25 import BM25
from mitos_rag.corpus import Unit, is_internal, load_units, section_units, split_sections, strip_boilerplate
from mitos_rag.evaluate import ProcedureRetriever, first_relevant_rank, recall_at_k, score_item, summarise
from mitos_rag.render import ATTRIBUTION, render_service_markdown
from mitos_rag.text import normalise, tokenize

FIXTURES = Path(__file__).parent / "fixtures"

DOC = f"""# Αρχική έκδοση δελτίου ταυτότητας

- Κωδικός Μίτος: 707544
- {ATTRIBUTION}

## Άλλες ονομασίες

Νέα ταυτότητα

## Κόστος

- e-Παράβολο: 10 €

## Βήματα

1. Αίτηση στο Αστυνομικό Τμήμα.
"""


def item(**overrides: object) -> dict:
    base = {
        "id": "q1",
        "question": "q",
        "category": "natural_greek",
        "expected_status": "answered",
        "expected_procedure_ids": ["a"],
        "acceptable_procedure_ids": [],
    }
    return base | overrides


def test_normalise_folds_case_accents_diaeresis_and_final_sigma() -> None:
    assert normalise("Ταυτότητας") == "ταυτοτητασ"
    assert normalise("Ευρωπαϊκή") == "ευρωπαικη"
    assert normalise("ΔΙΑΒΑΤΉΡΙΟ") == "διαβατηριο"


def test_raw_tokens_keep_accents_but_casefold_final_sigma() -> None:
    assert tokenize("Διαβατήριο, 80 €", "raw") == ["διαβατήριο", "80"]
    assert tokenize("ταυτότητας", "raw") == ["ταυτότητασ"]
    assert tokenize("Διαβατήριο, 80 €") == ["διαβατηριο", "80"]


def test_stemming_joins_nominative_and_genitive() -> None:
    assert tokenize("ταυτότητα") != tokenize("Ταυτότητας")
    assert tokenize("ταυτότητα", "stemmed") == tokenize("Ταυτότητας", "stemmed")
    assert tokenize("συνταξη", "stemmed") == tokenize("Σύνταξης", "stemmed")
    with pytest.raises(ValueError):
        tokenize("x", "lemmatised")  # type: ignore[arg-type]


def test_bm25_ranks_the_matching_document_first() -> None:
    index = BM25([["διαβατηριο", "εκδοση"], ["ταυτοτητα", "εκδοση"], ["συνταξη"]])
    scores = index.scores(["διαβατηριο", "εκδοση"])

    assert max(scores, key=scores.get) == 0
    assert 2 not in scores  # no shared term, not scored


def test_bm25_rare_terms_outweigh_common_ones() -> None:
    index = BM25([["κοινο", "σπανιο"], ["κοινο"], ["κοινο"]])

    assert index.idf["σπανιο"] > index.idf["κοινο"] > 0


def test_strip_boilerplate_removes_only_the_attribution() -> None:
    stripped = strip_boilerplate(DOC)

    assert ATTRIBUTION not in stripped
    assert "Κωδικός Μίτος: 707544" in stripped


def test_split_sections() -> None:
    title, header, sections = split_sections(DOC)

    assert title == "Αρχική έκδοση δελτίου ταυτότητας"
    assert header.startswith("# Αρχική")
    assert [heading for heading, _ in sections] == ["Άλλες ονομασίες", "Κόστος", "Βήματα"]


def test_section_units_carry_title_and_alternative_titles() -> None:
    units = section_units("707544", DOC)

    assert [unit.section for unit in units] == ["header", "Κόστος", "Βήματα"]
    for unit in units[1:]:
        assert unit.text.startswith("Αρχική έκδοση δελτίου ταυτότητας\nΝέα ταυτότητα\n")
    assert "10 €" in units[1].text and "Αστυνομικό" not in units[1].text


def test_load_units_from_rendered_fixtures(tmp_path: Path) -> None:
    for name in ("service_144400.json", "service_977354.json"):
        service = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        (tmp_path / f"{service['id']}.md").write_text(render_service_markdown(service), encoding="utf-8")

    docs = load_units(tmp_path, "doc")
    sections = load_units(tmp_path, "section")

    assert [unit.procedure_id for unit in docs] == ["144400", "977354"]
    assert len(sections) > len(docs)
    assert {unit.procedure_id for unit in sections} == {"144400", "977354"}
    assert all(ATTRIBUTION not in unit.text for unit in docs + sections)
    with pytest.raises(ValueError):
        load_units(tmp_path, "paragraph")


def test_retriever_dedups_by_procedure_using_the_best_section() -> None:
    units = [
        Unit("a", "Κόστος", "διαβατηριο κοστος"),
        Unit("a", "Βήματα", "διαβατηριο βηματα"),
        Unit("b", "Κόστος", "ταυτοτητα κοστος"),
    ]
    ranking = ProcedureRetriever(units, "normalised").rank("Διαβατήριο κόστος")

    assert [pid for pid, _ in ranking] == ["a", "b"]
    assert ProcedureRetriever(units, "normalised").rank("διαβατηριο", exclude=frozenset({"a"})) == []


def test_internal_procedures() -> None:
    def service(kind: str, trigger: list[str]) -> dict:
        return {"metadata": {"process": {"type": kind, "trigger": trigger}}}

    assert is_internal(service("Εσωστρεφής", ["Αιτούμενη"]))
    assert is_internal(service("Αμφιστρεφής", ["Εσωτερική"]))
    assert not is_internal(service("Αμφιστρεφής", ["Εσωτερική", "Αιτούμενη"]))
    assert not is_internal(json.loads((FIXTURES / "service_977354.json").read_text(encoding="utf-8")))


def test_rank_and_recall() -> None:
    assert first_relevant_rank(["x", "b", "a"], {"a", "b"}) == 2
    assert first_relevant_rank(["x"], {"a"}) is None
    assert recall_at_k(["a", "x", "b"], {"a", "b"}, 2) == 0.5
    assert recall_at_k(["a"], set(), 5) is None


def test_score_item_counts_acceptable_ids_for_hit_but_not_recall() -> None:
    ranking = [("x", 3.0), ("ok", 2.0), ("a", 1.0)]
    result = score_item(item(acceptable_procedure_ids=["ok"]), ranking)

    assert result["first_relevant_rank"] == 2
    assert result["hit@1"] == 0.0 and result["hit@5"] == 1.0
    assert result["rr"] == 0.5
    assert result["recall@1"] == 0.0 and result["recall@5"] == 1.0
    assert result["gold_ranks"] == {"a": 3, "ok": 2}


def test_summary_leaves_not_found_items_out_of_retrieval_metrics() -> None:
    results = [
        score_item(item(id="q1"), [("a", 5.0)]),
        score_item(item(id="q2", expected_procedure_ids=[], acceptable_procedure_ids=["b"]), [("x", 4.0)]),
        score_item(
            item(id="q3", category="out_of_scope", expected_status="not_found", expected_procedure_ids=[]),
            [("x", 7.0)],
        ),
    ]
    summary = summarise(results)

    assert summary["n"] == 2
    assert summary["hit@5"] == 0.5
    assert summary["mrr"] == 0.5
    assert summary["recall@5"] == 1.0  # q2 has no expected ids, so only q1 counts
    assert summary["not_found_top1_scores"] == {"q3": 7.0}
