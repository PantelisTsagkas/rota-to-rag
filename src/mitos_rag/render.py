"""Turn a raw Mitos procedure (JSON) into clean Greek Markdown for RAG indexing.

One procedure becomes one Markdown document with predictable section headings
(## Περιγραφή, ## Δικαιολογητικά, ## Κόστος, ...). Predictable headings make
chunking by section easy later, and every document carries its source URL and
the CC BY-SA attribution so answers can cite the official page.

Usage:
    uv run mitos-render --raw data/raw --out data/docs
"""

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ATTRIBUTION = (
    "Πηγή: Εθνικό Μητρώο Διοικητικών Διαδικασιών «Μίτος» (mitos.gov.gr), "
    "άδεια CC BY-SA 4.0."
)

_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

# (singular, plural) for each ISO 8601 duration part, in Greek.
_DURATION_UNITS: dict[str, tuple[str, str]] = {
    "years": ("έτος", "έτη"),
    "months": ("μήνας", "μήνες"),
    "weeks": ("εβδομάδα", "εβδομάδες"),
    "days": ("ημέρα", "ημέρες"),
    "hours": ("ώρα", "ώρες"),
    "minutes": ("λεπτό", "λεπτά"),
    "seconds": ("δευτερόλεπτο", "δευτερόλεπτα"),
}


def humanize_duration(value: str | None) -> str | None:
    """'P10D' -> '10 ημέρες', 'PT1H30M' -> '1 ώρα 30 λεπτά'. Unknown formats pass through."""
    if not value:
        return None
    match = _DURATION_PATTERN.match(value)
    if match is None:
        return value
    parts: list[str] = []
    for unit, (singular, plural) in _DURATION_UNITS.items():
        amount = match.group(unit)
        if amount is None or int(amount) == 0:
            continue
        parts.append(f"{int(amount)} {singular if int(amount) == 1 else plural}")
    return " ".join(parts) if parts else None


def duration_range(minimum: str | None, maximum: str | None) -> str | None:
    low, high = humanize_duration(minimum), humanize_duration(maximum)
    if low and high and low != high:
        return f"{low} έως {high}"
    return low or high


def get_section(metadata: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return a list section such as `process_evidences`.

    All 4,406 records in the 2026-10-14 snapshot place these lists directly
    under `metadata`, but an early inspected response nested them under
    `metadata.process`, so we check both locations. One record (275554) sends a
    dict keyed by number ({"1": {...}}) instead of a list; we accept that too.
    """
    value = metadata.get(key)
    if value is None:
        value = (metadata.get("process") or {}).get(key)
    if isinstance(value, dict):
        value = [value[k] for k in sorted(value, key=lambda k: (len(k), k)) if isinstance(value[k], dict)]
    return value if isinstance(value, list) else []


def _join(values: list[str] | None) -> str:
    return ", ".join(v for v in (values or []) if v)


# About 700 procedures write lists in MediaWiki style: "#" or "*" per item, "##" or
# "**" for a nested item, "#:" for a continuation line, with or without a space.
# A line starting "## " would become a fake Markdown heading and split a section.
_WIKI_LIST = re.compile(r"^([#*]+)(:?)\s*")
_MARKDOWN_BOLD = re.compile(r"^\*\*[^*]+\*\*")
# 256 procedures carry stray "<br />" tags; no other HTML appears in the snapshot.
_HTML_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _clean_line(line: str) -> str:
    """Turn one line's MediaWiki list marker into a Markdown bullet."""
    line = line.strip()
    match = _WIKI_LIST.match(line)
    if not match or _MARKDOWN_BOLD.match(line):
        return line
    depth = len(match.group(1)) - 1
    rest = line[match.end() :]
    if match.group(2):
        return "  " * (depth + 1) + rest
    return "  " * depth + "- " + rest


def clean_text(text: str | None) -> str:
    """Source free text with MediaWiki list markers and <br /> converted; blank lines kept."""
    text = _HTML_BREAK.sub("\n", text or "").strip()
    return "\n".join(_clean_line(line) for line in text.splitlines())


def _continue(text: str | None, indent: str) -> str:
    """Keep multi-line source text inside its list item: drop blank lines, indent the rest."""
    lines = [line for line in clean_text(text).splitlines() if line.strip()]
    return f"\n{indent}".join(lines)


def _glance_points(glance: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return the `value` list of a glance entry such as `service_points`."""
    item = glance.get(key)
    value = item.get("value") if isinstance(item, dict) else None
    return [point for point in value if isinstance(point, dict)] if isinstance(value, list) else []


def _format_points(points: list[dict[str, Any]]) -> str:
    """'ΚΕΠ (https://www.kep.gov.gr/), Εισαγγελίες'"""
    return ", ".join(
        f"{point['title']} ({point['url']})" if point.get("url") else point["title"]
        for point in points
        if point.get("title")
    )


def _format_cost(cost: dict[str, Any]) -> str:
    minimum, maximum = cost.get("evidence_cost_min"), cost.get("evidence_cost_max")
    if minimum is None and maximum is None:
        amount = ""
    elif minimum is not None and maximum is not None and minimum > maximum:
        # 71 fines in 242765 and 376453 read "500 to 1": the source
        # appears to truncate thousands. Leave the amount to the description.
        amount = ""
    elif minimum is None:
        amount = f"έως {maximum} €"
    elif minimum == maximum or maximum is None:
        amount = f"{minimum} €"
    else:
        amount = f"{minimum} έως {maximum} €"

    line = f"- {cost.get('evidence_cost_type', 'Κόστος')}"
    if amount:
        line += f": {amount}"
    if cost.get("evidence_cost_description"):
        line += f" ({_continue(cost['evidence_cost_description'], '  ')})"
    if cost.get("evidence_cost_alternative_of"):
        line += f" [εναλλακτικά του #{cost['evidence_cost_alternative_of']}]"
    details: list[str] = []
    if cost.get("evidence_cost_payment_type"):
        details.append(f"Τρόπος πληρωμής: {_join(cost['evidence_cost_payment_type'])}")
    if cost.get("evidence_cost_code"):
        details.append(f"Κωδικός παραβόλου: {cost['evidence_cost_code']}")
    if cost.get("evidence_cost_url"):
        details.append(f"Σύνδεσμος: {cost['evidence_cost_url']}")
    if details:
        line += "\n  " + "\n  ".join(details)
    return line


def _format_evidence(evidence: dict[str, Any]) -> str:
    number = evidence.get("evidence_num_id", "")
    line = f"{number}. {_continue(evidence.get('evidence_description', ''), '   ')}"
    tags: list[str] = []
    if evidence.get("evidence_submission_type"):
        tags.append(f"Υποβολή: {_join(evidence['evidence_submission_type'])}")
    if evidence.get("evidence_owner"):
        tags.append(f"Αφορά: {_join(evidence['evidence_owner'])}")
    if evidence.get("evidence_alternative_of"):
        tags.append(f"εναλλακτικά του #{evidence['evidence_alternative_of']}")
    if evidence.get("evidence_prerequisite"):
        tags.append(f"Προϋπόθεση: {_join(evidence['evidence_prerequisite'])}")
    if tags:
        line += f" ({'; '.join(tags)})"
    if evidence.get("evidence_note"):
        line += f"\n   Σημείωση: {_continue(evidence['evidence_note'], '   ')}"
    if evidence.get("evidence_related_url"):
        line += f"\n   Σύνδεσμος: {evidence['evidence_related_url']}"
    return line


def _format_step(step: dict[str, Any], prefix: str) -> str:
    number = step.get(f"{prefix}_num_id", "")
    line = f"{number}. {_continue(step.get(f'{prefix}_title'), '   ')}"
    tags: list[str] = []
    if step.get(f"{prefix}_official"):
        tags.append(f"Ποιος: {step[f'{prefix}_official']}")
    duration = duration_range(step.get(f"{prefix}_duration_min"), step.get(f"{prefix}_duration_max"))
    if duration:
        tags.append(f"Διάρκεια: {duration}")
    if tags:
        line += f" ({'; '.join(tags)})"
    if step.get(f"{prefix}_description"):
        line += f"\n   {_continue(step[f'{prefix}_description'], '   ')}"
    if step.get(f"{prefix}_note"):
        line += f"\n   Σημείωση: {_continue(step[f'{prefix}_note'], '   ')}"
    if step.get(f"{prefix}_url"):
        line += f"\n   Σύνδεσμος: {step[f'{prefix}_url']}"
    return line


def _format_rule(rule: dict[str, Any]) -> str:
    reference = f"{rule.get('rule_type', '')} {rule.get('rule_decision_number', '')}"
    if rule.get("rule_decision_year"):
        reference += f"/{rule['rule_decision_year']}"
    if rule.get("rule_article"):
        reference += f", άρθρο {rule['rule_article']}"
    if rule.get("rule_gazette_doc_number"):
        reference += f" (ΦΕΚ {rule.get('rule_gazette_doc_issue', '')} {rule['rule_gazette_doc_number']})".replace("  ", " ")
    line = f"- {reference.strip()}"
    if rule.get("rule_description"):
        line += f": {_continue(rule['rule_description'], '  ')}"
    if rule.get("rule_url"):
        line += f" {rule['rule_url']}"
    return line


def render_service_markdown(service: dict[str, Any]) -> str:
    """Render one procedure (the `data` object from /v1/services/{id})."""
    metadata: dict[str, Any] = service.get("metadata") or {}
    process: dict[str, Any] = metadata.get("process") or {}
    glance: dict[str, Any] = metadata.get("glance") or {}
    title = (service.get("title") or {}).get("el") or process.get("official_title") or str(service.get("id"))

    lines: list[str] = [f"# {title}", ""]
    lines.append(f"- Κωδικός Μίτος: {service.get('id')}")
    if service.get("url"):
        lines.append(f"- Επίσημη σελίδα: {service['url']}")
    if service.get("last_updated"):
        lines.append(f"- Τελευταία ενημέρωση: {service['last_updated']}")
    if process.get("status"):
        # e.g. "Υπό μεταβολή θεσμικού πλαισίου": the rules are about to change.
        lines.append(f"- Κατάσταση: {process['status']}")
    lines.append(f"- {ATTRIBUTION}")
    lines.append("")

    def add_section(heading: str, body: list[str]) -> None:
        body = [b for b in body if b]
        if body:
            lines.extend([f"## {heading}", "", *body, ""])

    add_section("Άλλες ονομασίες", [_join(process.get("alternative_titles"))])
    add_section("Περιγραφή", [clean_text(process.get("description"))])

    # Durations come from the source fields. glance.estimated_time is derived from
    # them and is missing for some procedures, and the glance document count
    # ("2 έως 13 + (3)") is not readable, so neither is used.
    at_a_glance: list[str] = []
    for key in ("cost", "duration_steps_digital"):
        item = glance.get(key)
        if isinstance(item, dict) and item.get("value"):
            at_a_glance.append(f"- {item.get('title', key)}: {item['value']}")
    # humanize_duration returns None for zero durations such as "P0D".
    deadline = humanize_duration(process.get("deadline_duration"))
    if deadline:
        at_a_glance.append(f"- Προθεσμία διεκπεραίωσης: {deadline}")
    estimated = duration_range(
        process.get("estimated_implementation_duration_min"),
        process.get("estimated_implementation_duration_max"),
    )
    if estimated:
        at_a_glance.append(f"- Εκτιμώμενος χρόνος διεκπεραίωσης: {estimated}")
    validity = humanize_duration(process.get("validity_duration"))
    if validity:
        at_a_glance.append(f"- Διάρκεια ισχύος: {validity}")
    if process.get("provided_to"):
        at_a_glance.append(f"- Απευθύνεται σε: {_join(process['provided_to'])}")
    if process.get("evidence_identification_type"):
        at_a_glance.append(f"- Ταυτοποίηση: {_join(process['evidence_identification_type'])}")
    if process.get("life_events"):
        at_a_glance.append(f"- Γεγονότα ζωής: {_join(process['life_events'])}")
    add_section("Με μια ματιά", at_a_glance)

    where: list[str] = []
    service_points = _glance_points(glance, "service_points")
    if service_points:
        label = glance["service_points"].get("title") or "Σημεία εξυπηρέτησης"
        where.append(f"- {label}: {_format_points(service_points)}")
    point_titles = {point.get("title") for point in service_points}
    directories = [d for d in process.get("provision_org_directory") or [] if d not in point_titles]
    if directories:
        where.append(f"- Υπηρεσίες: {_join(directories)}")
    remote_points = _glance_points(glance, "remote_service_points")
    if remote_points:
        where.append(f"- Εξ αποστάσεως: {_format_points(remote_points)}")
    # The glance list also says "Δεν παρέχεται ψηφιακά" when there is no online route.
    digital_points = _glance_points(glance, "digital_service_points") or [
        {"title": location.get("provision_digital_location_title"), "url": location.get("provision_digital_location_url")}
        for location in get_section(metadata, "process_provision_digital_locations")
    ]
    if digital_points:
        where.append(f"- Ψηφιακά: {_format_points(digital_points)}")
    add_section("Πού γίνεται", where)

    add_section(
        "Προϋποθέσεις",
        [
            f"{c.get('conditions_num_id', '')}. {_continue(c.get('conditions_name'), '   ')}"
            + (f" {c['conditions_url']}" if c.get("conditions_url") else "")
            for c in get_section(metadata, "process_conditions")
        ],
    )
    add_section("Δικαιολογητικά", [_format_evidence(e) for e in get_section(metadata, "process_evidences")])
    add_section("Κόστος", [_format_cost(c) for c in get_section(metadata, "process_evidences_cost")])

    how: list[str] = []
    if process.get("application_submission_type"):
        how.append(f"- Τρόπος υποβολής: {_join(process['application_submission_type'])}")
    if process.get("delivery_type"):
        how.append(f"- Τρόπος παραλαβής: {_join(process['delivery_type'])}")
    has_text = bool(process.get("application_description") or process.get("application_note"))
    # The trailing newline leaves a blank line between the list and the text below it.
    application: list[str] = ["\n".join(how) + ("\n" if has_text else "")] if how else []
    if process.get("application_description"):
        application.append(clean_text(process["application_description"]))
    if process.get("application_note"):
        application.append(clean_text(process["application_note"]))
    add_section("Αίτηση", application)

    add_section("Βήματα", [_format_step(s, "step") for s in get_section(metadata, "process_steps")])
    add_section(
        "Ψηφιακά βήματα",
        [_format_step(s, "step_digital") for s in get_section(metadata, "process_steps_digital")],
    )
    add_section("Παρατηρήσεις", [clean_text(process.get("remarks"))])
    add_section("Νομοθεσία", [_format_rule(r) for r in get_section(metadata, "process_rules")])
    add_section(
        "Χρήσιμοι σύνδεσμοι",
        [
            f"- {link.get('useful_link_title', '')}: {link.get('useful_link_url', '')}"
            for link in get_section(metadata, "process_useful_links")
        ],
    )

    return "\n".join(lines).rstrip() + "\n"


def render_directory(raw_dir: Path, out_dir: Path) -> int:
    """Render every <raw_dir>/services/*.json to <out_dir>/<id>.md. Returns the count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted((raw_dir / "services").glob("*.json")):
        service = json.loads(path.read_text(encoding="utf-8"))
        (out_dir / f"{path.stem}.md").write_text(render_service_markdown(service), encoding="utf-8")
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Render downloaded Mitos procedures to Markdown.")
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--out", type=Path, default=Path("data/docs"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    count = render_directory(args.raw, args.out)
    logger.info("Rendered %d procedures to %s", count, args.out)


if __name__ == "__main__":
    main()
