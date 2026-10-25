"""Load rendered procedures as retrieval units: whole documents or sections.

A unit is a piece of text that belongs to one procedure. Retrieval scores units,
then ranks procedures by their best unit, so five sections of one procedure
count as one result (PRD 6.4).
"""

import json
from dataclasses import dataclass
from pathlib import Path

from mitos_rag.render import ATTRIBUTION

ALTERNATIVE_TITLES = "Άλλες ονομασίες"


@dataclass(frozen=True)
class Unit:
    procedure_id: str
    section: str  # "doc" for a whole document, "header" for the block above the first section
    text: str


def strip_boilerplate(markdown: str) -> str:
    """Drop the attribution line. It is in every doc, so it only adds noise: without
    this, 'μητρωο' appears in all 4,406 docs and gets zero weight in BM25."""
    return "\n".join(line for line in markdown.splitlines() if line != f"- {ATTRIBUTION}")


def split_sections(markdown: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Split a rendered doc into (title, header block, [(heading, body)]).

    Safe to split on '## ' lines: decision 001 found no stray headings left in
    the rendered corpus.
    """
    lines = markdown.splitlines()
    title = lines[0].removeprefix("# ").strip() if lines and lines[0].startswith("# ") else ""
    header: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            header.append(line)
    return title, "\n".join(header).strip(), [(heading, "\n".join(body).strip()) for heading, body in sections]


def section_units(procedure_id: str, markdown: str) -> list[Unit]:
    """One unit per section, each prefixed with the title and alternative titles (PRD 6.3)."""
    title, header, sections = split_sections(markdown)
    alternatives = next((body for heading, body in sections if heading == ALTERNATIVE_TITLES), "")
    prefix = "\n".join(part for part in (title, alternatives) if part)
    units = [Unit(procedure_id, "header", header)]
    for heading, body in sections:
        if heading == ALTERNATIVE_TITLES:
            continue
        units.append(Unit(procedure_id, heading, f"{prefix}\n\n## {heading}\n\n{body}"))
    return units


def load_units(docs_dir: Path, granularity: str) -> list[Unit]:
    """granularity: 'doc' (one unit per procedure) or 'section'."""
    if granularity not in {"doc", "section"}:
        raise ValueError(f"unknown granularity: {granularity}")
    units: list[Unit] = []
    for path in sorted(docs_dir.glob("*.md")):
        markdown = strip_boilerplate(path.read_text(encoding="utf-8"))
        if granularity == "doc":
            units.append(Unit(path.stem, "doc", markdown))
        else:
            units.extend(section_units(path.stem, markdown))
    return units


def is_internal(service: dict) -> bool:
    """Internal to the administration (decision 001): Εσωστρεφής, or Εσωτερική is the only trigger."""
    process = (service.get("metadata") or {}).get("process") or {}
    return process.get("type") == "Εσωστρεφής" or process.get("trigger") == ["Εσωτερική"]


def internal_procedure_ids(raw_dir: Path) -> set[str]:
    ids: set[str] = set()
    for path in sorted((raw_dir / "services").glob("*.json")):
        if is_internal(json.loads(path.read_text(encoding="utf-8"))):
            ids.add(path.stem)
    return ids
