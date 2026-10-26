"""HTTP API (PRD Phase 5): `uv run mitos-api`, then `POST /ask` with `{"question": "..."}`.

It also serves the static Greek UI in `web/` at `/`, same origin, so no CORS is needed
locally. Where the UI lives in AWS is a Phase 6 decision.

The answer is the same pipeline as `mitos-ask` (Sonnet 4.6 + Haiku rerank, 009), returned
as structured fields so a UI can show the sources prominently, plus the rendered `text`.

Privacy (PRD 9): questions are never written anywhere. The API runs the assistant with no
response cache, no rerank cache and no query-embedding cache writes, and it logs no
question text: only status, cost and timings.

Limits (PRD 9: a Budget alarm is an alert, not a spending cap):

- a question is 3 to `MAX_QUESTION_CHARS` characters,
- `PER_IP_PER_MINUTE` questions per client IP,
- `DAILY_QUESTIONS` for the whole API per UTC day. A browser question measured $0.056,
  so the default 15 is at most about $0.84 a day, about $26 a month, inside the $40 monthly
  account cap (decision 013),
- one question at a time. Bedrock quotas are per model and `Pacer` lives in one process, so
  the API runs a single worker. A question that would wait more than `BUSY_TIMEOUT_S` for the
  one in progress gets a Greek "busy" reply instead of queueing. The lock is taken before the
  limits count anything, so a "busy" reply costs the visitor none of their questions.

On Lambda (013) `MITOS_LIMITS_TABLE` names a DynamoDB table, and the limits and the lock move
there (`dynamo.py`), because Lambda instances share no memory. Behind CloudFront the socket
address is CloudFront's: `MITOS_CLIENT_IP_HEADER=cloudfront-viewer-address` reads the visitor's.

Every refusal and failure is a Greek message in `{"error": ...}`.

When the browser goes away (the UI aborts its request when the user goes back), the paid
model calls not yet started are skipped: the rerank and answer calls check for a disconnect
first. A call already running cannot be interrupted and is still billed.

`POST /ask/stream` takes the same question under the same limits and answers in
newline-delimited JSON: `{"type": "found", "titles": [...]}` once the procedures are found,
`{"type": "text", "text": ...}` pieces of the answer as Claude writes it, then one last
`{"type": "done", ...}` with every `/ask` field, or `{"type": "error", "error": ...}`.
Streamed text is provisional: the checker, the warning and the sources come with "done".
A refusal (length, limits, busy) is still a plain JSON error with its status code.

Usage:
    uv run mitos-api                      # UI at http://127.0.0.1:8000
    curl -s localhost:8000/ask -H 'content-type: application/json' -d '{"question": "διαβατηριο"}'
"""

import argparse
import asyncio
import json
import logging
import queue
import os
import re
import threading
import time
import urllib.parse
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import anyio.from_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from mitos_rag.answer import (
    ANSWER_MODELS,
    ATTRIBUTION,
    DEFAULT_INDEX,
    DEFAULT_MODEL,
    WARNING,
    Answer,
    Assistant,
    Cancelled,
    build_assistant,
    plain_dashes,
)
from mitos_rag.dynamo import DynamoLease, DynamoLimits
from mitos_rag.embed import REGION
from mitos_rag.feedback import ANSWER_ID_PATTERN, REASONS, DynamoFeedback, FeedbackStore, MemoryFeedback

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 300
PER_IP_PER_MINUTE = int(os.environ.get("MITOS_PER_IP_PER_MINUTE", "3"))
DAILY_QUESTIONS = int(os.environ.get("MITOS_DAILY_QUESTIONS", "15"))
BUSY_TIMEOUT_S = 20.0
LIMITS_TABLE = os.environ.get("MITOS_LIMITS_TABLE", "")
CLIENT_IP_HEADER = os.environ.get("MITOS_CLIENT_IP_HEADER", "")
# The Lambda timeout: a lease outlives any answer, so only a dead Lambda's lease expires.
LEASE_S = int(os.environ.get("MITOS_LEASE_S", "150"))
# Test generation took up to 15.9 s non-streaming (009); a stuck call must not hold the lock.
MODEL_TIMEOUT_S = 45.0

ERRORS = {
    "length": f"Γράψτε μια ερώτηση από 3 έως {MAX_QUESTION_CHARS} χαρακτήρες.",
    "rate": "Κάνατε πολλές ερωτήσεις σε λίγο χρόνο. Δοκιμάστε ξανά σε ένα λεπτό.",
    "daily": "Συμπληρώθηκε το ημερήσιο όριο ερωτήσεων. Δοκιμάστε ξανά αύριο.",
    "busy": "Η υπηρεσία απαντά σε άλλη ερώτηση. Δοκιμάστε ξανά σε λίγα δευτερόλεπτα.",
    "provider": "Η υπηρεσία δεν είναι διαθέσιμη αυτή τη στιγμή. Δοκιμάστε ξανά αργότερα.",
    "feedback": "Η αξιολόγηση δεν καταχωρίστηκε.",
}


# The procedure's own page on the Mitos site. `id.mitos.gov.gr` (the record's `url`) was down
# with a 502 for every id on 2026-10-22 while these pages answered. The page name is the
# record's namespace and title with spaces as underscores, and "/" stays literal.
MITOS_PAGE_BASE = "https://mitos.gov.gr/index.php/"

# Pages that failed `scripts/check_pages.py` against our snapshot: Mitos renamed 921332 and moved
# 990784 to the ΠΑ namespace after our download (2026-10-14). These link to the id address, which
# redirects to the right page. Re-run the check after every data refresh and update this set.
PAGE_LINK_FALLBACK = frozenset({"921332", "990784"})


def page_url(title: str, ns: str = "ΔΔ") -> str:
    return MITOS_PAGE_BASE + urllib.parse.quote(f"{ns}:" + title.replace(" ", "_"))


def procedure_link(row: dict[str, Any]) -> str:
    if row["procedure_id"] in PAGE_LINK_FALLBACK:
        return row["source_url"]
    return page_url(row["title"], row.get("ns", "ΔΔ"))


# ---------- "at a glance" tiles, read straight from the facts row, never from the model ----------
# A tile is left out whenever the row cannot give one plain answer: a wrong tile next to a
# right answer is worse than no tile.

# Online services are often given in minutes (PT15M); seconds never appear except PT0S.
DURATION_PATTERN = re.compile(r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")
# Accusative forms ("ισχύει για 1 μήνα", "έως 3 μήνες"), the only case the tiles use.
DURATION_UNITS = (
    ("έτος", "έτη"), ("μήνα", "μήνες"), ("εβδομάδα", "εβδομάδες"), ("ημέρα", "ημέρες"), ("ώρα", "ώρες"), ("λεπτό", "λεπτά"),
)
CHANNEL_WORDS = {"digital": "ηλεκτρονικά", "in_person": "αυτοπροσώπως", "post": "ταχυδρομικά", "email": "με email"}


def duration_el(value: str | None) -> str | None:
    """An ISO 8601 date duration (P1Y, P6M, P50D) in Greek, or None for anything else."""
    match = DURATION_PATTERN.match(value or "")
    if not match or not any(match.groups()):
        return None
    parts = [
        f"{int(count)} {one if int(count) == 1 else many}"
        for count, (one, many) in zip(match.groups(), DURATION_UNITS, strict=True)
        if count is not None and int(count) > 0
    ]
    return " και ".join(parts) or None


def euros_el(amount: float) -> str:
    """Greek money style: 80 €, 62,50 €."""
    return f"{amount:.0f} €" if amount == int(amount) else f"{amount:.2f} €".replace(".", ",")


def cost_el(row: dict[str, Any]) -> str | None:
    """'Δωρεάν', one amount, or the range of one fee and its alternatives ('58 έως 80 €').
    None when the fees add up, depend on the case, or have an unknown amount (fines are not fees)."""
    if row.get("is_free"):
        return "Δωρεάν"
    costs = [cost for cost in row.get("costs") or [] if cost.get("cost_type") != "Πρόστιμο"]
    if not costs or len([cost for cost in costs if cost.get("alternative_of") is None]) != 1:
        return None
    lows, highs = [], []
    for cost in costs:
        low, high = cost.get("amount_min"), cost.get("amount_max")
        if cost.get("calculation") != ["Εφάπαξ"] or low is None or high is None or low > high:
            return None
        lows.append(low)
        highs.append(high)
    low, high = min(lows), max(highs)
    return euros_el(low) if low == high else f"{euros_el(low).removesuffix(' €')} έως {euros_el(high)}"


def channels_el(row: dict[str, Any]) -> str | None:
    words = [CHANNEL_WORDS[channel] for channel in CHANNEL_WORDS if channel in (row.get("submission_channels") or [])]
    if not words:
        return None
    text = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} ή {words[-1]}"
    return text[0].upper() + text[1:]


def time_el(row: dict[str, Any]) -> str | None:
    """'Έως 7 ημέρες', '2 έως 3 ημέρες', or '10 ημέρες έως 1 μήνα'."""
    low_iso, high_iso = row.get("estimated_min"), row.get("estimated_max")
    low, high = duration_el(low_iso), duration_el(high_iso)
    if high is None:
        return None
    if low is None or low == high:
        return f"Έως {high}"
    low_match, high_match = DURATION_PATTERN.match(low_iso), DURATION_PATTERN.match(high_iso)
    low_units = [index for index, count in enumerate(low_match.groups()) if count]
    high_units = [index for index, count in enumerate(high_match.groups()) if count]
    if len(low_units) == 1 and low_units == high_units:  # one unit, the same in both
        return f"{int(low_match.groups()[low_units[0]])} έως {high}"
    return f"{low} έως {high}"


def at_a_glance(row: dict[str, Any]) -> list[dict[str, str]]:
    tiles = [
        ("Κόστος", cost_el(row)),
        ("Αίτηση", channels_el(row)),
        ("Διάρκεια", time_el(row)),
        ("Ισχύει για", duration_el(row.get("validity"))),
    ]
    return [{"label": label, "value": value} for label, value in tiles if value]


def apply_online(row: dict[str, Any]) -> dict[str, str] | None:
    """Where to apply online: only when the procedure takes digital applications, https only.
    `online_urls` alone is not enough: some rows list an information portal there."""
    if row.get("can_apply_online") is not True:
        return None
    for url in row.get("online_urls") or []:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname:
            return {"url": url, "host": parsed.hostname.removeprefix("www.")}
    return None


class AskRequest(BaseModel):
    question: str


class FeedbackRequest(BaseModel):
    answer_id: str
    helpful: bool
    reason: str | None = None


class Limits:
    """Per-IP and daily limits, in memory: one worker, so one process holds the counts."""

    def __init__(
        self,
        per_ip_per_minute: int = PER_IP_PER_MINUTE,
        daily: int = DAILY_QUESTIONS,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], str] = lambda: datetime.now(UTC).date().isoformat(),
    ) -> None:
        self.per_ip_per_minute = per_ip_per_minute
        self.daily = daily
        self.clock = clock
        self.today = today
        self._recent: dict[str, deque[float]] = {}
        self._day = ""
        self._count = 0
        self._lock = threading.Lock()

    def check(self, ip: str) -> str | None:
        """The error key if this question is refused, else None (and it is counted)."""
        with self._lock:
            now = self.clock()
            recent = self._recent.setdefault(ip, deque())
            while recent and now - recent[0] >= 60.0:
                recent.popleft()
            if len(recent) >= self.per_ip_per_minute:
                return "rate"
            if self.today() != self._day:
                self._day, self._count = self.today(), 0
            if self._count >= self.daily:
                return "daily"
            recent.append(now)
            self._count += 1
            return None


class Lock(Protocol):
    def acquire(self, timeout: float = ...) -> bool: ...
    def release(self) -> None: ...


def client_ip(request: Request, header: str = "") -> str:
    """The visitor's address. With `header` (CloudFront-Viewer-Address, "ip:port", IPv6 too),
    that header without its port; CloudFront sets it, so a visitor cannot. Else the socket's."""
    if header:
        value = request.headers.get(header, "")
        return value.rpartition(":")[0] or "unknown"
    return request.client.host if request.client else "unknown"


def payload(answer: Answer) -> dict[str, Any]:
    """Structured fields for a UI. `text` is the same rendered answer `mitos-ask` prints."""
    cleaned, facts = answer.cleaned, answer.evidence.facts

    def procedure(pid: str) -> dict[str, Any]:
        row = facts[pid]
        return {
            "procedure_id": pid,
            "title": row["title"],
            "url": row["source_url"],
            "page_url": procedure_link(row),
            "last_updated": row["last_updated"][:10],
            "at_a_glance": at_a_glance(row),
            "apply_online": apply_online(row),
        }

    ambiguous = cleaned["status"] == "ambiguous"
    return {
        "status": cleaned["status"],
        "answer": plain_dashes(cleaned["answer_text"].strip()),
        "clarifying_question": plain_dashes(cleaned["clarifying_question"].strip()) if ambiguous else None,
        "options": [
            {"label": plain_dashes(opt["label"]), **procedure(opt["procedure_id"])}
            for opt in cleaned["options"]
            if opt["procedure_id"] in facts
        ]
        if ambiguous
        else [],
        "sources": [procedure(pid) for pid in dict.fromkeys(cleaned["procedure_ids"]) if pid in facts],
        "warning": None if answer.validation.ok else WARNING,
        "attribution": ATTRIBUTION,
        "text": answer.text,
        "timings_s": {key: round(value, 3) for key, value in answer.timings_s.items()},
    }


def error(key: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": ERRORS[key]}, status_code=status_code)


def create_app(
    assistant: Assistant,
    limits: Limits | DynamoLimits | None = None,
    busy_timeout_s: float = BUSY_TIMEOUT_S,
    web_dir: Path | None = None,
    lock: Lock | None = None,
    ip_header: str = "",
    feedback: FeedbackStore | None = None,
) -> FastAPI:
    app = FastAPI(title="Mitos RAG", docs_url=None, redoc_url=None)
    limits = limits or Limits()
    one_at_a_time: Lock = lock or threading.Lock()
    votes: FeedbackStore = feedback or MemoryFeedback()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def admit(question: str, request: Request) -> JSONResponse | None:
        """None when the question may go ahead, and then the caller holds the lock."""
        if not 3 <= len(question) <= MAX_QUESTION_CHARS:
            return error("length", 400)
        if not one_at_a_time.acquire(timeout=busy_timeout_s):
            return error("busy", 503)
        try:
            refused = limits.check(client_ip(request, ip_header))
        except Exception as failure:  # DynamoDB on Lambda: refuse rather than answer uncounted
            one_at_a_time.release()
            logger.error("limits failed: %s", type(failure).__name__)
            return error("provider", 503)
        if refused:
            one_at_a_time.release()
            return error(refused, 429)
        return None

    def log_answer(answer: Answer, start: float) -> None:
        # Per-stage seconds and answer tokens show where the wait goes; still no question text.
        stages = " ".join(f"{key}_s={value:.2f}" for key, value in answer.timings_s.items())
        logger.info(
            "ask status=%s cost_usd=%.4f total_s=%.2f %s input_tokens=%d output_tokens=%d warning=%s",
            answer.status, answer.cost_usd, time.monotonic() - start, stages,
            answer.generation.input_tokens, answer.generation.output_tokens, not answer.validation.ok,
        )

    def answered(answer: Answer) -> dict[str, Any]:
        """The payload plus `answer_id`, under which the answer is recorded for a vote (decision
        014): its status, cited procedures and cost, never the question. None for ambiguous
        (the visitor picks an option instead), or when recording failed: the answer still goes out."""
        body = payload(answer)
        body["answer_id"] = None
        if body["status"] == "ambiguous":
            return body
        answer_id = uuid.uuid4().hex
        try:
            votes.record(answer_id, body["status"], [source["procedure_id"] for source in body["sources"]], answer.cost_usd)
        except Exception as failure:
            logger.error("feedback record failed: %s", type(failure).__name__)
            return body
        body["answer_id"] = answer_id
        return body

    # Under /ask because CloudFront sends only /ask* to the API (decision 013). No lock and no
    # limits: a vote needs the id of a real answer and counts once.
    @app.post("/ask/feedback")
    def give_feedback(body: FeedbackRequest) -> Any:
        valid = ANSWER_ID_PATTERN.match(body.answer_id) and (
            body.reason is None or (body.reason in REASONS and body.helpful is False)
        )
        if not valid:
            return JSONResponse({"error": ERRORS["feedback"]}, status_code=400)
        try:
            counted = votes.vote(body.answer_id, body.helpful, body.reason)
        except Exception as failure:
            logger.error("feedback failed: %s", type(failure).__name__)
            return error("provider", 503)
        if not counted:
            return JSONResponse({"error": ERRORS["feedback"]}, status_code=409)
        logger.info("feedback helpful=%s reason=%s", body.helpful, body.reason)
        return Response(status_code=204)

    # A plain `def`: FastAPI runs it in a worker thread, so the blocking Bedrock calls never
    # stall the event loop, and the lock keeps them one at a time.
    @app.post("/ask")
    def ask(body: AskRequest, request: Request) -> Any:
        question = body.question.strip()
        refused = admit(question, request)
        if refused is not None:
            return refused
        def gone() -> bool:
            return anyio.from_thread.run(request.is_disconnected)

        start = time.monotonic()
        try:
            answer = assistant.ask(question, stop=gone)
        except Cancelled:
            logger.info("ask cancelled: the client left total_s=%.2f", time.monotonic() - start)
            return Response(status_code=499)  # nobody is listening; nginx's "client closed request"
        except Exception as failure:  # the model, Bedrock or S3 Vectors: never show a stack trace
            logger.error("ask failed: %s", type(failure).__name__)
            return error("provider", 503)
        finally:
            one_at_a_time.release()
        log_answer(answer, start)
        return answered(answer)

    # Also a plain `def`, so waiting for the lock never blocks the event loop. The answer runs
    # in its own thread, which releases the lock, and its events reach the response through a
    # queue. When the browser leaves, Starlette cancels the response, its `finally` sets `left`,
    # and the worker skips the paid calls not yet started.
    @app.post("/ask/stream")
    def ask_stream(body: AskRequest, request: Request) -> Any:
        question = body.question.strip()
        refused = admit(question, request)
        if refused is not None:
            return refused
        loop = anyio.from_thread.run_sync(asyncio.get_running_loop)
        events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        left = threading.Event()

        def gone() -> bool:
            if left.is_set():
                return True
            try:
                return asyncio.run_coroutine_threadsafe(request.is_disconnected(), loop).result(timeout=5)
            except Exception:
                return False

        def work() -> None:
            start = time.monotonic()
            try:
                answer = assistant.ask(question, stop=gone, on_event=events.put)
            except Cancelled:
                logger.info("ask cancelled: the client left total_s=%.2f", time.monotonic() - start)
            except Exception as failure:  # the model, Bedrock or S3 Vectors: never a stack trace
                logger.error("ask failed: %s", type(failure).__name__)
                events.put({"type": "error", "error": ERRORS["provider"]})
            else:
                log_answer(answer, start)
                events.put({"type": "done", **answered(answer)})
            finally:
                one_at_a_time.release()
                events.put(None)

        threading.Thread(target=work, name="ask-stream", daemon=True).start()

        async def lines() -> Any:
            try:
                while True:
                    event = await anyio.to_thread.run_sync(events.get, abandon_on_cancel=True)
                    if event is None:
                        return
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            finally:
                left.set()

        # no-transform: a proxy must not buffer or compress the stream.
        return StreamingResponse(
            lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store, no-transform"}
        )

    # Mounted last: Starlette matches in order, and a mount at `/` placed first would catch
    # `POST /ask` and answer 405.
    if web_dir is not None:
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def warm_up(assistant: Assistant) -> None:
    """A free call before the first question: CountTokens through the boto Bedrock client loads
    the AWS credentials (an SSO token refresh took about a second on 2026-10-22) and opens a
    connection. The Anthropic SDK's Bedrock client has no count_tokens ("not supported in
    Bedrock yet"), but it reads the same refreshed SSO token. A failure only logs."""
    if assistant.embedder is None:
        return
    start = time.monotonic()
    model = assistant.generator.model
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 1, "messages": [{"role": "user", "content": "γεια"}]}
    try:
        assistant.embedder.client.count_tokens(
            modelId=model.count_tokens_model_id, input={"invokeModel": {"body": json.dumps(body)}}
        )
    except Exception as failure:
        logger.warning("warm-up skipped: %s", type(failure).__name__)
        return
    logger.info("warm-up done in %.2f s", time.monotonic() - start)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Serve /ask over HTTP (one worker, no question storage).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--docs", type=Path, default=Path("data/docs"))
    parser.add_argument("--facts", type=Path, default=Path("data/facts/facts.jsonl"))
    parser.add_argument("--embeddings", type=Path, default=Path("data/embeddings"))
    parser.add_argument("--vector-index", default=DEFAULT_INDEX, help="BUCKET/INDEX in S3 Vectors")
    parser.add_argument("--model", choices=sorted(ANSWER_MODELS), default=DEFAULT_MODEL)
    parser.add_argument("--web", type=Path, default=Path("web"), help="static UI served at /")
    parser.add_argument("--no-web", action="store_true", help="API only: on AWS the UI is on S3 behind CloudFront")
    parser.add_argument(
        "--no-access-log", action="store_true",
        help="on AWS: uvicorn's access line carries the visitor's IP (it trusts X-Forwarded-For from the adapter)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    assistant = build_assistant(
        args.docs, args.facts, args.embeddings, args.vector_index,
        cache_dir=None, model=ANSWER_MODELS[args.model], rerank_cache=None,
        persist_queries=False, timeout_s=MODEL_TIMEOUT_S, max_retries=1,
    )
    limits: Limits | DynamoLimits = Limits()
    lock: Lock | None = None
    feedback: FeedbackStore = MemoryFeedback()
    if LIMITS_TABLE:
        import boto3

        dynamodb = boto3.client("dynamodb", region_name=REGION)
        limits = DynamoLimits(dynamodb, LIMITS_TABLE, PER_IP_PER_MINUTE, DAILY_QUESTIONS)
        lock = DynamoLease(dynamodb, LIMITS_TABLE, LEASE_S)
        feedback = DynamoFeedback(dynamodb, LIMITS_TABLE)
    warm_up(assistant)
    app = create_app(
        assistant, limits, web_dir=None if args.no_web else args.web, lock=lock, ip_header=CLIENT_IP_HEADER,
        feedback=feedback,
    )
    # uvicorn's access log prints paths only, never request bodies. On Lambda it is off: the
    # per-question log line above has what is needed, without the visitor's address.
    uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=not args.no_access_log)


if __name__ == "__main__":
    main()
