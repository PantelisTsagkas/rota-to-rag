"""Structured facts for each procedure, read straight from Mitos fields (PRD 6.1).

The LLM must not be the only thing that reads costs and deadlines. These facts
come from clean source fields, so answer validation can check every cost and
duration against them. A fact the source does not give is None (or an empty
tuple), never a guess.

Output: one JSON object per line in <out>, with provenance (PRD 5.3).

Usage:
    uv run mitos-facts --raw data/raw --out data/facts/facts.jsonl
"""

import argparse
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mitos_rag.render import get_section

logger = logging.getLogger(__name__)

FREE_OF_CHARGE = "Παρέχεται χωρίς κόστος"

# application_submission_type value -> channels. Values seen in the real data
# (2026-10-14); anything else maps to "other" so it is visible, not dropped.
SUBMISSION_CHANNELS: dict[str, tuple[str, ...]] = {
    "Κατάθεση από τον αιτούντα (ψηφιακή)": ("digital",),
    "Κατάθεση από τον αιτούντα (email)": ("email",),
    "Κατάθεση από τον αιτούντα (δια ζώσης ή ταχυδρομικά)": ("in_person", "post"),
    "Κατάθεση από τον αιτούντα (αποκλειστικά δια ζώσης)": ("in_person",),
    "Κατάθεση από τον αιτούντα (αποκλειστικά με courier)": ("courier",),
    "Επίδειξη από τον αιτούντα": ("in_person",),
    "Αυτεπάγγελτη (χειροκίνητα)": ("ex_officio",),
    "Αυτεπάγγελτη (ψηφιακά)": ("ex_officio",),
}


@dataclass(frozen=True)
class Cost:
    num_id: int | None
    cost_type: str | None  # e-Παράβολο, Τέλος, Πρόστιμο (a fine, not a fee), ...
    amount_min: float | None
    amount_max: float | None
    calculation: tuple[str, ...]  # Εφάπαξ is fixed; Ποσοτικό, Ποσοστό, Βάσει ... depend on the case
    description: str | None
    alternative_of: int | None  # this cost replaces cost #N in some cases (e.g. under 14)
    code: str | None


@dataclass(frozen=True)
class ProcedureFacts:
    procedure_id: str
    title: str
    source_url: str
    last_updated: str | None
    ingested_at: str
    content_hash: str
    status: str | None
    is_free: bool | None  # True only when the source says free and lists no cost
    costs: tuple[Cost, ...]
    deadline: str | None  # ISO 8601 duration, legal processing deadline
    estimated_min: str | None
    estimated_max: str | None
    validity: str | None
    submission_types: tuple[str, ...]  # raw source values
    submission_channels: tuple[str, ...]  # normalised: digital, email, in_person, post, ...
    can_apply_online: bool | None  # None when the source gives no submission type
    online_urls: tuple[str, ...]
    document_count: int
    provided_to: tuple[str, ...]


def content_hash(service: dict[str, Any]) -> str:
    canonical = json.dumps(service, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def extract_costs(metadata: dict[str, Any]) -> tuple[Cost, ...]:
    return tuple(
        Cost(
            num_id=_as_int(cost.get("evidence_cost_num_id")),
            cost_type=cost.get("evidence_cost_type"),
            amount_min=_as_float(cost.get("evidence_cost_min")),
            amount_max=_as_float(cost.get("evidence_cost_max")),
            calculation=tuple(cost.get("evidence_cost_calculation") or ()),
            description=(cost.get("evidence_cost_description") or "").strip() or None,
            alternative_of=_as_int(cost.get("evidence_cost_alternative_of")),
            code=cost.get("evidence_cost_code"),
        )
        for cost in get_section(metadata, "process_evidences_cost")
    )


def submission_channels(submission_types: tuple[str, ...]) -> tuple[str, ...]:
    channels: list[str] = []
    for value in submission_types:
        for channel in SUBMISSION_CHANNELS.get(value, ("other",)):
            if channel not in channels:
                channels.append(channel)
    return tuple(channels)


def extract_facts(service: dict[str, Any], ingested_at: str) -> ProcedureFacts:
    metadata: dict[str, Any] = service.get("metadata") or {}
    process: dict[str, Any] = metadata.get("process") or {}
    glance: dict[str, Any] = metadata.get("glance") or {}
    procedure_id = str(service.get("id"))

    costs = extract_costs(metadata)
    glance_cost = (glance.get("cost") or {}).get("value")
    # Three procedures say "free" in the glance but list costs: trust the cost rows.
    if costs:
        is_free: bool | None = False
    elif glance_cost == FREE_OF_CHARGE:
        is_free = True
    else:
        is_free = None

    submission_types = tuple(process.get("application_submission_type") or ())
    channels = submission_channels(submission_types)
    can_apply_online = ("digital" in channels) if submission_types else None

    return ProcedureFacts(
        procedure_id=procedure_id,
        title=(service.get("title") or {}).get("el") or process.get("official_title") or procedure_id,
        # Built by our code from the id, so a citation link never depends on the model.
        source_url=f"https://id.mitos.gov.gr/{procedure_id}",
        last_updated=service.get("last_updated"),
        ingested_at=ingested_at,
        content_hash=content_hash(service),
        status=process.get("status"),
        is_free=is_free,
        costs=costs,
        deadline=process.get("deadline_duration"),
        estimated_min=process.get("estimated_implementation_duration_min"),
        estimated_max=process.get("estimated_implementation_duration_max"),
        validity=process.get("validity_duration"),
        submission_types=submission_types,
        submission_channels=channels,
        can_apply_online=can_apply_online,
        online_urls=tuple(
            location["provision_digital_location_url"]
            for location in get_section(metadata, "process_provision_digital_locations")
            if location.get("provision_digital_location_url")
        ),
        document_count=len(get_section(metadata, "process_evidences")),
        provided_to=tuple(process.get("provided_to") or ()),
    )


def build_facts(raw_dir: Path, out_path: Path) -> int:
    """Write facts for every <raw_dir>/services/*.json to a JSONL file. Returns the count."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8") as out_file:
        for path in sorted((raw_dir / "services").glob("*.json")):
            service = json.loads(path.read_text(encoding="utf-8"))
            # The downloader only writes a file when it fetches it, so its
            # modification time is when we ingested this version.
            ingested_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
            facts = extract_facts(service, ingested_at=ingested_at)
            out_file.write(json.dumps(asdict(facts), ensure_ascii=False) + "\n")
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract structured facts from downloaded Mitos procedures.")
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--out", type=Path, default=Path("data/facts/facts.jsonl"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    count = build_facts(args.raw, args.out)
    logger.info("Wrote facts for %d procedures to %s", count, args.out)


if __name__ == "__main__":
    main()
