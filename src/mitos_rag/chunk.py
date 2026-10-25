"""Split sections into chunks small enough for every embedding model we compare.

Cohere Embed Multilingual v3 accepts at most 512 tokens (and 2,048 characters)
per text. A 2,000-character Greek chunk was measured just over 512 Cohere
tokens, so a chunk is at most 1,500 characters, title prefix included. Titan V2
takes far more, but both models get the same chunks so the comparison is fair
(PRD 7.1).

A section that fits stays one chunk. A longer one is split on line boundaries
(list items, paragraphs), and each new chunk repeats the last lines of the
previous one, up to `overlap` characters. Every chunk starts with the procedure
title, the alternative titles and the section heading (PRD 6.3).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from mitos_rag.corpus import ALTERNATIVE_TITLES, split_sections, strip_boilerplate

MAX_CHARS = 1500
OVERLAP = 200
MAX_PREFIX = 400

_SENTENCE_END = re.compile(r"(?<=[.;!·])\s+")


@dataclass(frozen=True)
class Chunk:
    procedure_id: str
    section: str
    chunk_index: int  # order within the procedure
    text: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _hard_wrap(text: str, budget: int) -> list[str]:
    """Split one over-long line: at sentence ends, then at spaces, then anywhere."""
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > budget:
            cut = sentence.rfind(" ", 0, budget)
            cut = cut if cut > 0 else budget
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    return pieces


def split_text(body: str, budget: int, overlap: int = OVERLAP) -> list[str]:
    """Pack lines into windows of at most `budget` characters, with line overlap."""
    if len(body) <= budget:
        return [body]
    lines: list[str] = []
    for line in body.splitlines():
        if line.strip():
            lines.extend(_hard_wrap(line, budget) if len(line) > budget else [line])

    windows: list[list[str]] = [[]]
    size = 0  # characters in the current window, counting one newline per line
    for line in lines:
        if windows[-1] and size + len(line) + 1 > budget:
            # Carry whole trailing lines of the full window, up to `overlap`
            # characters, as long as the new line still fits after them. A last
            # line longer than `overlap` means no overlap for that split.
            carried: list[str] = []
            carried_size = 0
            for previous in reversed(windows[-1]):
                added = len(previous) + 1
                if carried_size + added > overlap or carried_size + added + len(line) + 1 > budget:
                    break
                carried.insert(0, previous)
                carried_size += added
            windows.append(carried)
            size = carried_size
        windows[-1].append(line)
        size += len(line) + 1
    return ["\n".join(window) for window in windows if window]


def chunk_markdown(procedure_id: str, markdown: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[Chunk]:
    title, header, sections = split_sections(strip_boilerplate(markdown))
    alternatives = next((body for heading, body in sections if heading == ALTERNATIVE_TITLES), "")
    prefix = "\n".join(part for part in (title, alternatives) if part)[:MAX_PREFIX]

    chunks: list[Chunk] = []
    for piece in split_text(header, max_chars, overlap):
        chunks.append(Chunk(procedure_id, "header", len(chunks), piece))
    for heading, body in sections:
        if heading == ALTERNATIVE_TITLES:
            continue
        lead = f"{prefix}\n\n## {heading}\n\n"
        for piece in split_text(body, max_chars - len(lead), overlap):
            chunks.append(Chunk(procedure_id, heading, len(chunks), lead + piece))
    return chunks


def load_chunks(docs_dir: Path, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(docs_dir.glob("*.md")):
        chunks.extend(chunk_markdown(path.stem, path.read_text(encoding="utf-8"), max_chars, overlap))
    return chunks
