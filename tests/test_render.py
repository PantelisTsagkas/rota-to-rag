import json
from pathlib import Path

import pytest

from mitos_rag.render import (
    ATTRIBUTION,
    clean_text,
    get_section,
    humanize_duration,
    render_directory,
    render_service_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def section(markdown: str, heading: str) -> str:
    """Text of one `## heading` section, without the heading line."""
    _, _, rest = markdown.partition(f"## {heading}\n")
    return rest.split("\n## ", 1)[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("P10D", "10 ημέρες"),
        ("P1M", "1 μήνας"),
        ("PT1H30M", "1 ώρα 30 λεπτά"),
        ("PT1S", "1 δευτερόλεπτο"),
        ("P0D", None),
        ("", None),
        (None, None),
        ("not-a-duration", "not-a-duration"),
    ],
)
def test_humanize_duration(value: str | None, expected: str | None) -> None:
    assert humanize_duration(value) == expected


def test_digital_procedure_has_source_and_key_facts() -> None:
    markdown = render_service_markdown(load("service_439993.json"))

    assert markdown.startswith("# Αποδεικτικό φορολογικής ενημερότητας\n")
    assert "https://id.mitos.gov.gr/439993" in markdown
    assert ATTRIBUTION in markdown
    assert "Παρέχεται χωρίς κόστος" in markdown
    assert "Προθεσμία διεκπεραίωσης: 10 ημέρες" in markdown
    assert "Διάρκεια ισχύος: 1 μήνας" in markdown
    assert "- Τρόπος υποβολής: Κατάθεση από τον αιτούντα (ψηφιακή)" in markdown
    assert "- Τρόπος παραλαβής: Θυρίδες gov.gr" in markdown
    assert "## Ψηφιακά βήματα" in markdown
    assert "## Δικαιολογητικά" not in markdown  # this procedure has none


def test_in_person_procedure_lists_documents_alternative_costs_and_steps() -> None:
    markdown = render_service_markdown(load("service_144400.json"))

    assert "1. Δελτίο Ταυτότητας." in section(markdown, "Δικαιολογητικά")
    cost = section(markdown, "Κόστος")
    assert "- e-Παράβολο: 80 € (Αρχική έκδοση" in cost
    assert "- e-Παράβολο: 71 € (" in cost
    assert "[εναλλακτικά του #1]" in cost
    assert "Κωδικός παραβόλου: 2014" in cost
    assert "Διάρκεια: 5 λεπτά έως 15 λεπτά" in section(markdown, "Βήματα")
    assert "Τρόπος υποβολής: Κατάθεση από τον αιτούντα (αποκλειστικά δια ζώσης)" in markdown


def test_where_lists_service_points_and_says_when_not_online() -> None:
    passport = section(render_service_markdown(load("service_144400.json")), "Πού γίνεται")

    assert "- Σημεία εξυπηρέτησης: Αστυνομικά Τμήματα (https://" in passport
    assert "- Ψηφιακά: Δεν παρέχεται ψηφιακά" in passport
    # The directory is also a service point, so it is not repeated.
    assert "Υπηρεσίες:" not in passport


def test_remote_service_estimated_time_and_status_are_rendered() -> None:
    markdown = render_service_markdown(load("service_977354.json"))

    assert "- Κατάσταση: Υπό μεταβολή θεσμικού πλαισίου" in markdown
    assert "- Εκτιμώμενος χρόνος διεκπεραίωσης: 2 ημέρες έως 3 ημέρες" in markdown
    where = section(markdown, "Πού γίνεται")
    assert "ΚΕΠ (https://www.kep.gov.gr/)" in where
    assert "- Εξ αποστάσεως: myKEPlive (https://" in where
    assert "Αριθμός δικαιολογητικών" not in markdown


def test_multi_line_notes_stay_inside_their_list_item() -> None:
    documents = section(render_service_markdown(load("service_977354.json")), "Δικαιολογητικά")

    for line in documents.strip().splitlines():
        assert line.startswith(("1.", "2.", "   ")), line


def test_sections_nested_under_process_are_still_rendered() -> None:
    service = load("service_144400.json")
    metadata = service["metadata"]
    metadata["process"]["process_evidences"] = metadata.pop("process_evidences")

    assert "1. Δελτίο Ταυτότητας." in render_service_markdown(service)


def test_minimal_procedure_does_not_crash() -> None:
    markdown = render_service_markdown({"id": "1", "metadata": {}})
    assert markdown.startswith("# 1\n")


def test_render_directory(tmp_path: Path) -> None:
    services = tmp_path / "raw" / "services"
    services.mkdir(parents=True)
    for name in ("service_439993.json", "service_144400.json"):
        (services / name.removeprefix("service_")).write_text((FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")

    count = render_directory(tmp_path / "raw", tmp_path / "docs")

    assert count == 2
    assert sorted(p.name for p in (tmp_path / "docs").iterdir()) == ["144400.md", "439993.md"]


def test_get_section_accepts_dict_keyed_by_number() -> None:
    # Shape seen in record 275554 for process_provision_digital_locations.
    metadata = {"process_provision_digital_locations": {"2": {"n": "b"}, "10": {"n": "c"}, "1": {"n": "a"}}}

    assert [item["n"] for item in get_section(metadata, "process_provision_digital_locations")] == ["a", "b", "c"]


SECTION_HEADINGS = {
    "Άλλες ονομασίες", "Περιγραφή", "Με μια ματιά", "Πού γίνεται", "Προϋποθέσεις", "Δικαιολογητικά", "Κόστος",
    "Αίτηση", "Βήματα", "Ψηφιακά βήματα", "Παρατηρήσεις", "Νομοθεσία", "Χρήσιμοι σύνδεσμοι",
}


def test_wiki_list_markup_never_becomes_a_heading() -> None:
    markdown = render_service_markdown(load("service_197055.json"))

    headings = [line for line in markdown.splitlines()[1:] if line.startswith("#")]
    assert {h.removeprefix("## ") for h in headings} <= SECTION_HEADINGS
    conditions = section(markdown, "Προϋποθέσεις")
    assert "   - Τεχνικοί ασφάλειας των ΕΑΚ" in conditions
    assert "     - Πτυχιούχοι ΑΕΙ και ΑΤΕΙ" in conditions


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# one\n## nested\n#: more", "- one\n  - nested\n  more"),
        ("*α. item\n**β. sub", "- α. item\n  - β. sub"),
        ("**Bold** stays", "**Bold** stays"),
        ("plain\n\nsecond", "plain\n\nsecond"),
        ("line one<br />line two<BR>", "line one\nline two"),
        (None, ""),
    ],
)
def test_clean_text_converts_wiki_list_markers(text: str | None, expected: str) -> None:
    assert clean_text(text) == expected


def test_cost_with_only_a_maximum_reads_as_up_to() -> None:
    service = {"id": "1", "metadata": {"process_evidences_cost": [
        {"evidence_cost_num_id": 1, "evidence_cost_type": "Τέλος", "evidence_cost_min": None, "evidence_cost_max": 14.67},
    ]}}

    assert "- Τέλος: έως 14.67 €" in section(render_service_markdown(service), "Κόστος")


def test_zero_durations_are_left_out() -> None:
    service = {"id": "1", "metadata": {"process": {"deadline_duration": "P0D", "validity_duration": "P0D"}}}

    assert "None" not in render_service_markdown(service)


def test_cost_with_min_above_max_shows_no_amount() -> None:
    service = {"id": "1", "metadata": {"process_evidences_cost": [
        {"evidence_cost_num_id": 1, "evidence_cost_type": "Πρόστιμο", "evidence_cost_min": 500, "evidence_cost_max": 1,
         "evidence_cost_description": "από 500 έως 1.000 ευρώ"},
    ]}}

    assert section(render_service_markdown(service), "Κόστος").strip() == "- Πρόστιμο (από 500 έως 1.000 ευρώ)"
