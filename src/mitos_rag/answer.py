"""Grounded answers (PRD 6.4 to 6.6): `uv run mitos-ask "ερώτηση"`.

One question goes through these steps:

1. Embed the question with Cohere (`search_query`). Questions already in the query
   cache cost nothing; a new one is one Bedrock call, about $0.000002.
2. Find the nearest chunks in S3 Vectors (`vectorstore.py`). Procedures rank by
   their best chunk.
3. Rerank (decision 009, `--rerank haiku`, the default): Haiku 4.5 reorders the
   dense top 30 procedures by title and alternative titles (`rerank.py`, 008), and
   its top 5 become the candidates. `--rerank none` keeps the dense top 5, which
   reproduces decisions 006 and 007 from the cache.
4. Build the evidence: the candidates' facts rows (`facts.jsonl`) and their best
   chunks, within a character budget. Claude reads Greek at about 1.5 characters
   per token (CountTokens, 2026-10-17), so whole documents do not fit. A facts row
   over `FACTS_ROW_CHARS` (6 procedures, mostly fines registries) is capped (011).
5. Claude on Bedrock (Sonnet 4.6 by default since 009, `--model` picks another) writes a
   structured answer (`output_config.format`):
   status, Greek text, cited procedures and the typed facts it used.
6. Code checks the answer (`validate.py`) and adds the official links, dates,
   attribution and disclaimer. The model never writes links.

Responses are cached on disk by a hash of the full request, so rerunning or
rescoring the eval never pays twice.
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from mitos_rag.chunk import Chunk, load_chunks
from mitos_rag.corpus import strip_boilerplate
from mitos_rag.embed import MODELS, REGION, Embedder, VectorCache, text_hash
from mitos_rag.pacing import Pacer
from mitos_rag.rerank import CANDIDATES as RERANK_CANDIDATES
from mitos_rag.rerank import HAIKU_MODEL_ID, HAIKU_REQUESTS_PER_MINUTE, HaikuReranker, load_cards
from mitos_rag.validate import Validation, validate
from mitos_rag.vectorstore import S3VectorsRetriever, chunk_key, procedure_of

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    """A Claude model on Bedrock eu-central-1, through its regional (eu.) inference profile."""

    name: str
    model_id: str
    count_tokens_model_id: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    requests_per_minute: int


# Prices per million tokens from the AWS Price List API, eu-central-1. Requests per minute are
# this account's cross-region quotas (GetServiceQuota), checked on 2026-10-17.
ANSWER_MODELS: dict[str, ModelSpec] = {
    "haiku-4.5": ModelSpec(
        "haiku-4.5",
        "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        1.10,
        5.50,
        10,
    ),
    "sonnet-4.6": ModelSpec(
        "sonnet-4.6",
        "eu.anthropic.claude-sonnet-4-6",
        "anthropic.claude-sonnet-4-6",
        3.30,
        16.50,
        10,
    ),
}
DEFAULT_MODEL = "sonnet-4.6"  # chosen on 2026-10-17 (009)
MAX_TOKENS = 2000

TOP_PROCEDURES = 5
EVIDENCE_CHARS = 12_000  # about 8k tokens of Greek
# A facts row in the prompt (decision 011). p99 is 2,421 characters; 6 of 4,406 rows are over
# 6,000, and the two largest fines registries are about 58,000 (99 and 98 cost rows). An
# over-budget row first gets its long cost descriptions trimmed (description p99 is about
# 1,300), then loses cost rows from the end, and says so. Rows under budget are unchanged.
FACTS_ROW_CHARS = 6_000
TRIMMED_DESCRIPTION_CHARS = 1_000

DEFAULT_INDEX = "mitos-rag-vectors/chunks-cohere-v3-20261014"
# The top 100 chunks span as few as 16 procedures on dev, so the rerank path asks for up
# to 300 and stops paging at 30 procedures (300 chunks covered 30 on every dev and test question).
RERANK_TOP_K_CHUNKS = 300
RERANKERS = ("haiku", "none")
DEFAULT_RERANKER = "haiku"

SYSTEM_PROMPT = """You answer questions from citizens about Greek public administration procedures. Use only the evidence from the Mitos registry (mitos.gov.gr) in the user message: the <candidates>, <facts> and <evidence> blocks. Never use outside knowledge. If the evidence does not answer the question, say so.

Write answer_text in simple Greek Markdown. Put the most useful information first. Use bullet lists for documents and steps. Keep it short.

Do not write links, URLs or a list of sources. The application adds the official links and dates.

Costs, durations, validity and whether a procedure can be done online come from <facts>. A null amount means the amount is unknown, never zero. When a cost depends on the case (for example age or renewal vs first issue), name the case. If the question does not say which case applies, list the cases. List each fee separately with its own amount and never add amounts together: totals are not in the registry, so they cannot be checked.

For every cost, duration, validity period and online statement in answer_text, add an entry to facts_used with the procedure_id and a typed value:
- cost: the amount in euro as a plain number, for example "71" or "62.5".
- duration: the ISO 8601 value exactly as in <facts> (deadline, estimated_min or estimated_max), for example "P5D".
- validity: the ISO 8601 value of validity in <facts>.
- online: "yes" or "no", from can_apply_online.
- other: anything else important you relied on, as a short Greek phrase.

status:
- answered: the candidates contain the procedure (or procedures) that answer the question. procedure_ids lists the procedures you used. clarifying_question is "" and options is empty.
  - Assume the ordinary case for a citizen in Greece. When one candidate is the general procedure and others are for special groups (diplomats, refugees, seafarers, births abroad), answer for the general one and add one line saying special cases exist.
  - When the question needs several procedures that all apply (for example two lost documents, or the steps after a birth), answer for all of them.
- ambiguous: only when the candidates are different procedures that an ordinary citizen could equally mean, and answering the wrong one would mislead (for example a first ID card vs a lost ID card vs a renewal). Write one short clarifying question in Greek, and 2 to 4 options, each a short Greek label with its procedure_id. answer_text says in one sentence why you ask and must not repeat the question.
- not_found: no candidate answers the question, or the question is not about a public administration procedure. answer_text starts with "Δεν βρήκα αυτή την πληροφορία στο Μητρώο Μίτος." and adds one sentence. procedure_ids may list up to 3 candidates worth suggesting, or none.

missing_facts lists, in short Greek, what the question asks that the evidence does not contain.

Use only procedure_ids that appear in <candidates>."""

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["answered", "ambiguous", "not_found"]},
        "answer_text": {"type": "string"},
        "procedure_ids": {"type": "array", "items": {"type": "string"}},
        "facts_used": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["cost", "duration", "validity", "online", "other"]},
                    "value": {"type": "string"},
                    "procedure_id": {"type": "string"},
                },
                "required": ["type", "value", "procedure_id"],
                "additionalProperties": False,
            },
        },
        "missing_facts": {"type": "array", "items": {"type": "string"}},
        "clarifying_question": {"type": "string"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "procedure_id": {"type": "string"}},
                "required": ["label", "procedure_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "status",
        "answer_text",
        "procedure_ids",
        "facts_used",
        "missing_facts",
        "clarifying_question",
        "options",
    ],
    "additionalProperties": False,
}

ATTRIBUTION = (
    "Πηγή: Εθνικό Μητρώο Διοικητικών Διαδικασιών «Μίτος» (mitos.gov.gr), άδεια CC BY-SA 4.0. "
    "Οι πληροφορίες είναι ενημερωτικές και δεν αποτελούν νομική συμβουλή: επιβεβαιώστε τις στην επίσημη σελίδα."
)
WARNING = (
    "Προσοχή: δεν μπόρεσα να επιβεβαιώσω ορισμένα στοιχεία της απάντησης (ποσά, προθεσμίες ή τρόπο υποβολής) "
    "με τα στοιχεία του Μίτος. Ελέγξτε τα στην επίσημη σελίδα."
)
FACT_KEYS = ("deadline", "estimated_min", "estimated_max", "validity", "can_apply_online", "is_free", "status")


class GenerationError(RuntimeError):
    pass


def load_facts(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return {row["procedure_id"]: row for row in map(json.loads, handle)}


def _row_chars(row: dict[str, Any]) -> int:
    return len(json.dumps(row, ensure_ascii=False))


def cap_costs(row: dict[str, Any], budget: int = FACTS_ROW_CHARS) -> dict[str, Any]:
    """Fit an over-budget facts row (011). The checker still sees the full row and doc text."""
    if _row_chars(row) <= budget:
        return row
    costs = []
    for cost in row["costs"]:
        description = cost.get("description") or ""
        if len(description) > TRIMMED_DESCRIPTION_CHARS:
            cost = {**cost, "description": description[:TRIMMED_DESCRIPTION_CHARS].rstrip() + "…"}
        costs.append(cost)
    total = len(costs)
    capped = {**row, "costs": costs}
    while _row_chars(capped) > budget and len(capped["costs"]) > 1:
        shown = capped["costs"][:-1]
        capped = {
            **row,
            "costs": shown,
            "costs_shown": len(shown),
            "costs_total": total,
            "costs_note": f"Εμφανίζονται {len(shown)} από {total} χρεώσεις. Οι υπόλοιπες είναι στην επίσημη σελίδα της διαδικασίας.",
        }
    return capped


def prompt_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """The facts row as the model sees it. min > max amounts are unknown (CLAUDE.md data notes)."""
    costs = []
    for cost in facts.get("costs", []):
        low, high = cost.get("amount_min"), cost.get("amount_max")
        if low is not None and high is not None and low > high:
            low = high = None
        costs.append(
            {
                "num_id": cost.get("num_id"),
                "cost_type": cost.get("cost_type"),
                "amount_min": low,
                "amount_max": high,
                "description": cost.get("description"),
                "alternative_of": cost.get("alternative_of"),
            }
        )
    row = {"procedure_id": facts["procedure_id"], "title": facts["title"], "costs": costs}
    row.update({key: facts.get(key) for key in FACT_KEYS})
    row["submission_types"] = facts.get("submission_types", [])
    return cap_costs(row)


@dataclass(frozen=True)
class Candidate:
    procedure_id: str
    title: str
    score: float


@dataclass
class Evidence:
    question: str
    candidates: list[Candidate]
    chunks: list[Chunk]
    facts: dict[str, dict[str, Any]]

    @property
    def candidate_ids(self) -> list[str]:
        return [candidate.procedure_id for candidate in self.candidates]


def best_scores(hits: list[tuple[str, float]]) -> dict[str, float]:
    """Each procedure's best chunk similarity, best procedure first."""
    best: dict[str, float] = {}
    for key, score in sorted(hits, key=lambda hit: -hit[1]):
        best.setdefault(procedure_of(key), score)
    return best


def dense_order(hits: list[tuple[str, float]], facts: dict[str, dict[str, Any]]) -> list[str]:
    """Procedures by best chunk, only those with a facts row (the prompt needs one)."""
    return [pid for pid in best_scores(hits) if pid in facts]


def build_evidence(
    question: str,
    hits: list[tuple[str, float]],
    chunks_by_key: dict[str, Chunk],
    facts: dict[str, dict[str, Any]],
    top: int = TOP_PROCEDURES,
    budget: int = EVIDENCE_CHARS,
    order: list[str] | None = None,
) -> Evidence:
    """The top procedures (by best chunk, or in `order` when a reranker gave one); every
    candidate gets its best chunk, then the budget fills by similarity. Scores stay dense cosine."""
    hits = sorted(hits, key=lambda hit: -hit[1])
    best = best_scores(hits)
    if order is None:
        ranked = dense_order(hits, facts)[:top]
    else:
        ranked = [pid for pid in dict.fromkeys(order) if pid in best and pid in facts][:top]
    candidates = [Candidate(pid, facts[pid]["title"], best[pid]) for pid in ranked]

    chosen: list[str] = []
    used = 0
    first_chunk = {pid: next(key for key, _ in hits if procedure_of(key) == pid) for pid in ranked}
    for key in [first_chunk[pid] for pid in ranked] + [key for key, _ in hits]:
        if key in chosen or procedure_of(key) not in first_chunk or key not in chunks_by_key:
            continue
        size = len(chunks_by_key[key].text)
        if used + size > budget and key not in first_chunk.values():
            continue
        chosen.append(key)
        used += size
    order = {pid: rank for rank, pid in enumerate(ranked)}
    selected = sorted((chunks_by_key[key] for key in chosen), key=lambda c: (order[c.procedure_id], c.chunk_index))
    return Evidence(question, candidates, selected, {pid: facts[pid] for pid in ranked})


def user_message(evidence: Evidence) -> str:
    lines = ["<candidates>"]
    lines += [f"{rank}. {c.procedure_id} | {c.title}" for rank, c in enumerate(evidence.candidates, start=1)]
    lines += ["</candidates>", "", "<facts>"]
    lines += [json.dumps(prompt_facts(evidence.facts[pid]), ensure_ascii=False) for pid in evidence.candidate_ids]
    lines += ["</facts>", "", "<evidence>"]
    for chunk in evidence.chunks:
        lines.append(f'<chunk procedure_id="{chunk.procedure_id}" section="{chunk.section}">')
        lines.append(chunk.text)
        lines.append("</chunk>")
    lines += ["</evidence>", "", f"<question>{evidence.question}</question>"]
    return "\n".join(lines)


def request_params(user: str, model: ModelSpec = ANSWER_MODELS[DEFAULT_MODEL]) -> dict[str, Any]:
    """Everything that shapes the response, so the cache key covers it."""
    return {
        "model": model.model_id,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
        # SDK 1.x dropped sampling parameters from create(); Haiku 4.5 and Sonnet 4.6 still
        # honour them, and temperature 0 keeps eval reruns comparable.
        "extra_body": {"temperature": 0},
    }


@dataclass
class Generation:
    output: dict[str, Any]
    input_tokens: int
    output_tokens: int
    latency_s: float
    cached: bool
    model: ModelSpec = ANSWER_MODELS[DEFAULT_MODEL]
    # Seconds to the first streamed text, only when streamed. Not stored in the cache.
    first_token_s: float | None = None

    @property
    def cost_usd(self) -> float:
        model = self.model
        return (self.input_tokens * model.input_usd_per_mtok + self.output_tokens * model.output_usd_per_mtok) / 1e6


class Generator:
    """Calls Claude through the Anthropic SDK's Bedrock client, paced to the quota, with a disk cache."""

    def __init__(
        self,
        client: Any,
        cache_dir: Path | None,
        model: ModelSpec = ANSWER_MODELS[DEFAULT_MODEL],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        pacer: Pacer | None = None,
    ) -> None:
        self.client = client
        self.cache_dir = cache_dir
        self.model = model
        self.clock = clock
        self.pacer = pacer or Pacer(model.requests_per_minute, sleep, clock)

    def _cache_path(self, params: dict[str, Any]) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def generate(self, user: str, on_text: Callable[[str], None] | None = None) -> Generation:
        """With `on_text`, a fresh call streams and passes on each piece of the JSON as it is
        written. The request is the same either way, so streaming never changes a cache key."""
        params = request_params(user, self.model)
        path = self._cache_path(params)
        if path is not None and path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            return Generation(
                stored["output"], stored["input_tokens"], stored["output_tokens"], stored["latency_s"], True, self.model
            )

        self.pacer.wait()
        start = self.clock()
        first_token: float | None = None
        if on_text is None:
            response = self.client.messages.create(**params)
        else:
            with self.client.messages.stream(**params) as stream:
                for piece in stream.text_stream:
                    if first_token is None:
                        first_token = self.clock() - start
                    on_text(piece)
                response = stream.get_final_message()
        latency = self.clock() - start

        if response.stop_reason != "end_turn":
            raise GenerationError(f"stop_reason {response.stop_reason}: no complete answer")
        text = "".join(block.text for block in response.content if block.type == "text")
        try:
            output = json.loads(text)
        except json.JSONDecodeError as error:
            raise GenerationError(f"response is not JSON: {text[:200]}") from error
        generation = Generation(
            output, response.usage.input_tokens, response.usage.output_tokens, latency, False, self.model, first_token
        )
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            stored = {
                "output": output,
                "input_tokens": generation.input_tokens,
                "output_tokens": generation.output_tokens,
                "latency_s": latency,
                "model": self.model.model_id,
            }
            path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        return generation


JSON_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
ANSWER_TEXT_START = re.compile(r'"answer_text"\s*:\s*"')


class AnswerTextStream:
    """Reads `answer_text` out of the answer JSON while it is still being written.

    `status` comes first in ANSWER_SCHEMA and `answer_text` second, so the text can be shown
    almost from the first token. An escape cut in half by a chunk boundary waits for the rest."""

    def __init__(self) -> None:
        self.buffer = ""
        self.position: int | None = None  # just after the opening quote of the value
        self.done = False

    def feed(self, piece: str) -> str:
        """The newly readable part of `answer_text`, decoded."""
        self.buffer += piece
        if self.done:
            return ""
        if self.position is None:
            match = ANSWER_TEXT_START.search(self.buffer)
            if match is None:
                return ""
            self.position = match.end()
        text, out, i = self.buffer, [], self.position
        while i < len(text):
            char = text[i]
            if char == '"':
                self.done = True
                break
            if char != "\\":
                out.append(char)
                i += 1
                continue
            if i + 1 >= len(text):
                break
            if text[i + 1] != "u":
                out.append(JSON_ESCAPES[text[i + 1]])
                i += 2
                continue
            if i + 6 > len(text):
                break
            code = int(text[i + 2 : i + 6], 16)
            if 0xD800 <= code < 0xDC00:  # a surrogate pair is one character
                if i + 12 > len(text):
                    break
                low = int(text[i + 8 : i + 12], 16)
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                i += 12
                continue
            out.append(chr(code))
            i += 6
        self.position = i
        return "".join(out)


def plain_dashes(text: str) -> str:
    """No em dashes in user-facing text (CLAUDE.md). Sonnet 4.6 writes them (007, 009)."""
    return text.replace("\u2014", "-")


def render(cleaned: dict[str, Any], validation: Validation, facts: dict[str, dict[str, Any]]) -> str:
    """The text a user sees. Links, dates, attribution and the warning come from code."""
    parts = [plain_dashes(cleaned["answer_text"].strip())]
    if cleaned["status"] == "ambiguous":
        parts.append(plain_dashes(cleaned["clarifying_question"].strip()))
        parts.append("\n".join(f"{n}. {plain_dashes(opt['label'])}" for n, opt in enumerate(cleaned["options"], start=1)))
    if not validation.ok:
        parts.append(WARNING)
    cited = cleaned["procedure_ids"] + [opt["procedure_id"] for opt in cleaned["options"]]
    if cited:
        heading = "Ίσως σας ενδιαφέρουν:" if cleaned["status"] == "not_found" else "Πηγές:"
        sources = [
            f"- {facts[pid]['title']}: {facts[pid]['source_url']} (τελευταία ενημέρωση {facts[pid]['last_updated'][:10]})"
            for pid in dict.fromkeys(cited)
            if pid in facts
        ]
        parts.append("\n".join([heading, *sources]))
    parts.append(ATTRIBUTION)
    return "\n\n".join(parts)


@dataclass
class Answer:
    question: str
    status: str
    text: str
    evidence: Evidence
    output: dict[str, Any]
    cleaned: dict[str, Any]
    validation: Validation
    generation: Generation
    timings_s: dict[str, float] = field(default_factory=dict)
    rerank: dict[str, Any] | None = None  # reranker name, cost and whether it was a fresh call

    @property
    def cost_usd(self) -> float:
        """Answer plus rerank cost for this question."""
        return self.generation.cost_usd + (self.rerank["cost_usd"] if self.rerank else 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "status": self.status,
            "text": self.text,
            "candidates": [c.__dict__ for c in self.evidence.candidates],
            "evidence_chunks": [chunk_key(c) for c in self.evidence.chunks],
            "output": self.output,
            "cleaned": self.cleaned,
            "validation": self.validation.as_dict(),
            "input_tokens": self.generation.input_tokens,
            "output_tokens": self.generation.output_tokens,
            "cost_usd": self.generation.cost_usd,
            "cached": self.generation.cached,
            "rerank": self.rerank,
            "timings_s": self.timings_s,
        }


FOUND_TITLES = 3  # candidate titles shown while the answer is being written
# While the answer streams, how often `stop` is asked whether the asker is still there.
STOP_CHECK_S = 0.5


class Cancelled(Exception):
    """The asker left (the API saw the browser disconnect), so the paid calls not yet made are
    skipped. A call already in progress cannot be interrupted and is still billed."""


def never() -> bool:
    return False


class Assistant:
    def __init__(
        self,
        retriever: S3VectorsRetriever,
        embedder: Embedder | None,
        chunks_by_key: dict[str, Chunk],
        facts: dict[str, dict[str, Any]],
        generator: Generator,
        reranker: HaikuReranker | None = None,
        docs_dir: Path | None = None,
        persist_queries: bool = True,
    ) -> None:
        self.retriever = retriever
        self.embedder = embedder
        self.chunks_by_key = chunks_by_key
        self.facts = facts
        self.generator = generator
        self.reranker = reranker
        self.docs_dir = docs_dir
        # False in the API (PRD 9): a new question's vector is used once and never written.
        self.persist_queries = persist_queries

    def procedure_texts(self, procedure_ids: list[str]) -> dict[str, str]:
        """Rendered text of the cited procedures, for the checker's second tier. Whole docs,
        not joined chunks: a hard-wrapped line can split "13 μηνών" across two chunks."""
        if self.docs_dir is None:
            return {}
        texts = {}
        for pid in dict.fromkeys(procedure_ids):
            path = self.docs_dir / f"{pid}.md"
            if path.exists():
                texts[pid] = strip_boilerplate(path.read_text(encoding="utf-8"))
        return texts

    def query_vector(self, question: str) -> np.ndarray:
        queries = self.retriever.queries
        key = text_hash(question)
        if key not in queries:
            if self.embedder is None:
                raise ValueError(f"question not embedded and no embedder given: {question}")
            vectors, _ = self.embedder.embed([question], "query")
            if not self.persist_queries:
                return vectors[0]
            queries.add([key], vectors)
            queries.flush()
        return queries.matrix([key])[0]

    def retrieve(self, question: str) -> tuple[list[tuple[str, float]], dict[str, float]]:
        """Nearest chunks. With a reranker, enough of them for its candidate list."""
        start = time.monotonic()
        vector = self.query_vector(question)
        embedded = time.monotonic()
        if self.reranker is None:
            hits = self.retriever.nearest_chunks(vector)
        else:
            hits = self.retriever.nearest_chunks(vector, RERANK_TOP_K_CHUNKS, RERANK_CANDIDATES)
        retrieved = time.monotonic()
        return hits, {"embed": embedded - start, "retrieve": retrieved - embedded}

    def rerank_candidates(self, hits: list[tuple[str, float]]) -> list[str]:
        return dense_order(hits, self.facts)[:RERANK_CANDIDATES]

    def evidence(
        self, question: str, stop: Callable[[], bool] = never
    ) -> tuple[Evidence, dict[str, float], dict[str, Any] | None]:
        hits, timings = self.retrieve(question)
        order: list[str] | None = None
        rerank: dict[str, Any] | None = None
        if self.reranker is not None:
            if stop():
                raise Cancelled
            calls, cost = self.reranker.fresh_calls, self.reranker.cost_usd
            start = time.monotonic()
            order = self.reranker.rerank(question, self.rerank_candidates(hits))
            fresh = self.reranker.fresh_calls > calls
            # A fresh call is timed without its wait for the quota, like the answer call.
            timings["rerank"] = self.reranker.last_latency_s if fresh else time.monotonic() - start
            rerank = {"name": self.reranker.name, "cost_usd": self.reranker.cost_usd - cost, "cached": not fresh}
        evidence = build_evidence(question, hits, self.chunks_by_key, self.facts, order=order)
        return evidence, timings, rerank

    def ask(
        self,
        question: str,
        stop: Callable[[], bool] = never,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> Answer:
        """`stop` is checked before each paid model call; True raises `Cancelled`.

        With `on_event`, the answer streams: a "found" event with the top candidates' titles
        once retrieval and rerank are done, then "text" events with pieces of `answer_text`.
        Streamed text is provisional: the checker only runs on the finished answer. While it
        streams, `stop` is checked every STOP_CHECK_S, and True closes the stream mid-answer."""
        evidence, timings, rerank = self.evidence(question, stop)
        if stop():
            raise Cancelled
        on_text: Callable[[str], None] | None = None
        if on_event is not None:
            titles = [self.facts[pid]["title"] for pid in evidence.candidate_ids[:FOUND_TITLES] if pid in self.facts]
            on_event({"type": "found", "titles": titles})
            reader = AnswerTextStream()
            last_check = time.monotonic()

            def on_text(piece: str) -> None:
                nonlocal last_check
                if time.monotonic() - last_check >= STOP_CHECK_S:
                    last_check = time.monotonic()
                    if stop():
                        raise Cancelled
                text = reader.feed(piece)
                if text:
                    on_event({"type": "text", "text": plain_dashes(text)})

        generation = self.generator.generate(user_message(evidence), on_text)
        timings["generate"] = generation.latency_s
        if generation.first_token_s is not None:
            timings["first_token"] = generation.first_token_s
        texts = self.procedure_texts(generation.output.get("procedure_ids", []))
        cleaned, validation = validate(generation.output, evidence.candidate_ids, evidence.facts, texts)
        text = render(cleaned, validation, evidence.facts)
        return Answer(
            question, cleaned["status"], text, evidence, generation.output, cleaned, validation, generation, timings, rerank
        )


def build_assistant(
    docs: Path,
    facts_path: Path,
    embeddings: Path,
    vector_index: str,
    cache_dir: Path | None,
    model: ModelSpec = ANSWER_MODELS[DEFAULT_MODEL],
    reranker: str = DEFAULT_RERANKER,
    rerank_cache: Path | None = None,
    persist_queries: bool = True,
    timeout_s: float = 600.0,
    max_retries: int = 4,
) -> Assistant:
    """Wire the real AWS clients. Imports stay here so tests never need credentials.

    The API passes no caches, `persist_queries=False`, a short timeout and fewer retries."""
    import boto3
    from anthropic import AnthropicBedrock

    bucket, index = vector_index.split("/", 1)
    queries = VectorCache(embeddings / "cohere" / "query")
    retriever = S3VectorsRetriever(boto3.client("s3vectors", region_name=REGION), bucket, index, queries)
    embedder = Embedder(MODELS["cohere"], boto3.client("bedrock-runtime", region_name=REGION))
    chunks_by_key = {chunk_key(chunk): chunk for chunk in load_chunks(docs)}
    client = AnthropicBedrock(aws_region=REGION, max_retries=max_retries, timeout=timeout_s)
    pacer = Pacer(model.requests_per_minute)
    generator = Generator(client, cache_dir, model, pacer=pacer)
    haiku: HaikuReranker | None = None
    if reranker == "haiku":
        # Bedrock quotas are per model: with Haiku answering, both calls share one quota.
        shared = pacer if model.model_id == HAIKU_MODEL_ID else Pacer(HAIKU_REQUESTS_PER_MINUTE)
        haiku = HaikuReranker(client, load_cards(docs), rerank_cache, pacer=shared)
    return Assistant(retriever, embedder, chunks_by_key, load_facts(facts_path), generator, haiku, docs, persist_queries)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--docs", type=Path, default=Path("data/docs"))
    parser.add_argument("--facts", type=Path, default=Path("data/facts/facts.jsonl"))
    parser.add_argument("--embeddings", type=Path, default=Path("data/embeddings"))
    parser.add_argument("--vector-index", default=DEFAULT_INDEX, help="BUCKET/INDEX in S3 Vectors")
    parser.add_argument("--cache", type=Path, default=Path("data/answers/cache"), help="response cache (git-ignored)")
    parser.add_argument("--model", choices=sorted(ANSWER_MODELS), default=DEFAULT_MODEL, help="Claude model for answers")
    parser.add_argument("--rerank", choices=RERANKERS, default=DEFAULT_RERANKER, help="none reproduces 006 and 007")
    parser.add_argument("--rerank-cache", type=Path, default=Path("data/rerank/cache/haiku"), help="rerank cache (git-ignored)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Answer a question about a Greek public procedure from Mitos.")
    parser.add_argument("question")
    add_common_arguments(parser)
    parser.add_argument("--json", action="store_true", help="print the full record, not only the answer")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    assistant = build_assistant(
        args.docs,
        args.facts,
        args.embeddings,
        args.vector_index,
        args.cache,
        ANSWER_MODELS[args.model],
        args.rerank,
        args.rerank_cache,
    )
    answer = assistant.ask(args.question)
    if args.json:
        print(json.dumps(answer.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(answer.text)
    g = answer.generation
    rerank = f", rerank {answer.rerank['name']} ${answer.rerank['cost_usd']:.4f}" if answer.rerank else ", no rerank"
    print(
        f"\n[{answer.status}] {g.model.name}, {g.input_tokens} in / {g.output_tokens} out tokens, ${g.cost_usd:.4f}"
        f"{' (cached)' if g.cached else ''}{rerank}, validation {'ok' if answer.validation.ok else 'FAILED'}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
