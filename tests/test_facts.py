import json
import os
from pathlib import Path

from mitos_rag.facts import build_facts, content_hash, extract_facts, submission_channels

FIXTURES = Path(__file__).parent / "fixtures"
INGESTED_AT = "2026-10-14T00:00:00+00:00"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_passport_costs_keep_alternatives_and_conditions() -> None:
    facts = extract_facts(load("service_144400.json"), ingested_at=INGESTED_AT)

    assert facts.is_free is False
    assert facts.can_apply_online is False
    assert facts.submission_channels == ("in_person",)
    assert facts.deadline == "P5D"
    by_amount = {cost.amount_min: cost for cost in facts.costs}
    assert by_amount[80.0].alternative_of is None
    assert "πάνω από 14" in (by_amount[80.0].description or "")
    assert by_amount[71.0].alternative_of == 1
    assert "έως 14" in (by_amount[71.0].description or "")
    assert by_amount[71.0].code == "2015"
    assert facts.document_count == 16


def test_amounts_mitos_confirmed_as_misread_are_unknown() -> None:
    # Mitos confirmed the thousands-dot bug in 242765 (reply of 2026-10-28): keep the rows, drop the numbers.
    service = {**load("service_144400.json"), "id": "242765"}
    facts = extract_facts(service, ingested_at=INGESTED_AT)

    assert len(facts.costs) == len(extract_facts(load("service_144400.json"), ingested_at=INGESTED_AT).costs)
    assert all(cost.amount_min is None and cost.amount_max is None for cost in facts.costs)
    assert facts.is_free is False


def test_digital_procedure_is_free_and_online() -> None:
    facts = extract_facts(load("service_439993.json"), ingested_at=INGESTED_AT)

    assert facts.is_free is True
    assert facts.costs == ()
    assert facts.can_apply_online is True
    assert facts.deadline == "P10D"
    assert facts.validity == "P1M"
    assert facts.online_urls[0].startswith("https://www.gov.gr/")


def test_provenance_and_status() -> None:
    service = load("service_977354.json")
    facts = extract_facts(service, ingested_at=INGESTED_AT)

    assert facts.procedure_id == "977354"
    assert facts.source_url == "https://id.mitos.gov.gr/977354"
    assert facts.ns == service["ns"] == "ΔΔ"
    assert facts.last_updated == service["last_updated"]
    assert facts.ingested_at == INGESTED_AT
    assert facts.content_hash == content_hash(service)
    assert facts.status == "Υπό μεταβολή θεσμικού πλαισίου"
    assert (facts.estimated_min, facts.estimated_max) == ("P2D", "P3D")
    assert set(facts.submission_channels) == {"in_person", "post", "digital"}


def test_content_hash_ignores_key_order_but_not_values() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_missing_fields_are_unknown_not_guessed() -> None:
    facts = extract_facts({"id": "1", "metadata": {}}, ingested_at=INGESTED_AT)

    assert facts.is_free is None
    assert facts.can_apply_online is None
    assert facts.deadline is None
    assert facts.costs == ()


def test_free_in_glance_but_costs_listed_is_not_free() -> None:
    service = {
        "id": "2",
        "metadata": {
            "glance": {"cost": {"value": "Παρέχεται χωρίς κόστος"}},
            "process_evidences_cost": [{"evidence_cost_type": "Τέλος", "evidence_cost_min": 10, "evidence_cost_max": 10}],
        },
    }
    assert extract_facts(service, ingested_at=INGESTED_AT).is_free is False


def test_unknown_submission_type_is_kept_visible() -> None:
    assert submission_channels(("Κάτι καινούργιο",)) == ("other",)


def test_build_facts_writes_one_line_per_procedure(tmp_path: Path) -> None:
    services = tmp_path / "raw" / "services"
    services.mkdir(parents=True)
    target = services / "439993.json"
    target.write_text((FIXTURES / "service_439993.json").read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(target, (1_760_400_000, 1_760_400_000))

    out = tmp_path / "facts" / "facts.jsonl"
    assert build_facts(tmp_path / "raw", out) == 1

    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["procedure_id"] == "439993"
    assert row["ingested_at"] == "2025-10-14T00:00:00+00:00"
    assert row["ns"] == "ΔΔ"
