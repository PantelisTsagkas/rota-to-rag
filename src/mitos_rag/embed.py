"""Embed chunks and eval questions with a Bedrock embedding model, with a resumable cache.

Models compared (decision 004), both on-demand in eu-central-1:

- titan: Amazon Titan Text Embeddings V2, one text per request, 1,024 dims.
- cohere: Cohere Embed Multilingual v3, up to 96 texts per request, 1,024 dims.
  Chunks use input_type search_document, questions search_query.

Vectors are stored by content hash under `<out>/<model>/<kind>/shard-NNNNN.npz`,
flushed every few hundred texts, so an interrupted run resumes where it
stopped. Billed tokens (the `x-amzn-bedrock-input-token-count` header) are
logged to `<out>/<model>/usage.jsonl`.

The local AWS profile is an SSO role. When its session expires the run stops
with a clear message instead of retrying: log in again and rerun.

Usage:
    uv run mitos-embed --model cohere
    caffeinate -i uv run mitos-embed --model titan    # long run: keep the Mac awake
"""

import argparse
import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from botocore.exceptions import (
    ClientError,
    NoCredentialsError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

from mitos_rag.chunk import load_chunks

logger = logging.getLogger(__name__)

REGION = "eu-central-1"
Kind = Literal["document", "query"]
FLUSH_EVERY = 500

_RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException", "InternalServerException"}
_AUTH_CODES = {"ExpiredToken", "ExpiredTokenException", "UnrecognizedClientException", "InvalidClientTokenId"}
_AUTH_EXCEPTIONS = (NoCredentialsError, SSOTokenLoadError, TokenRetrievalError, UnauthorizedSSOTokenError)


class AuthExpiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    key: str
    model_id: str
    batch_size: int
    requests_per_minute: int  # the account's applied quotas, checked 2026-10-14
    tokens_per_minute: int


MODELS: dict[str, ModelSpec] = {
    "titan": ModelSpec("titan", "amazon.titan-embed-text-v2:0", batch_size=1, requests_per_minute=60, tokens_per_minute=300_000),
    "cohere": ModelSpec("cohere", "cohere.embed-multilingual-v3", batch_size=96, requests_per_minute=20, tokens_per_minute=300_000),
}


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_body(spec: ModelSpec, texts: list[str], kind: Kind) -> dict[str, Any]:
    if spec.key == "titan":
        (text,) = texts
        return {"inputText": text, "dimensions": 1024, "normalize": True}
    # NONE would reject the ~1-3% of link- and law-heavy chunks over 512 tokens
    # (decision 004); END keeps their beginning, which holds the title and heading.
    return {
        "texts": texts,
        "input_type": "search_document" if kind == "document" else "search_query",
        "truncate": "END",
    }


def parse_vectors(spec: ModelSpec, payload: dict[str, Any]) -> list[list[float]]:
    if spec.key == "titan":
        return [payload["embedding"]]
    return payload["embeddings"]


class Embedder:
    """Calls one model, paced to its request quota, retrying only on throttling."""

    def __init__(
        self,
        spec: ModelSpec,
        client: Any,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = 8,
    ) -> None:
        self.spec = spec
        self.client = client
        self.sleep = sleep
        self.clock = clock
        self.max_retries = max_retries
        self.min_interval = 60.0 / spec.requests_per_minute
        self._last_request: float | None = None
        self._last_tokens = 0

    def _pace(self) -> None:
        """Space requests to stay under both the request and the token quota."""
        if self._last_request is not None:
            interval = max(self.min_interval, 60.0 * self._last_tokens / self.spec.tokens_per_minute)
            wait = interval - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)
        self._last_request = self.clock()

    def embed(self, texts: list[str], kind: Kind) -> tuple[np.ndarray, int]:
        """Vectors for up to `batch_size` texts, and the billed input tokens."""
        if not 0 < len(texts) <= self.spec.batch_size:
            raise ValueError(f"{self.spec.key} takes 1 to {self.spec.batch_size} texts per request")
        body = json.dumps(request_body(self.spec, texts, kind))
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                response = self.client.invoke_model(
                    modelId=self.spec.model_id, body=body, contentType="application/json", accept="application/json"
                )
            except _AUTH_EXCEPTIONS as error:
                raise AuthExpiredError(str(error)) from error
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code", "")
                if code in _AUTH_CODES:
                    raise AuthExpiredError(str(error)) from error
                if code in _RETRYABLE and attempt < self.max_retries:
                    delay = min(60.0, 2.0**attempt)
                    logger.warning("%s: %s, retrying in %.0f s", self.spec.key, code, delay)
                    self.sleep(delay)
                    continue
                raise
            payload = json.loads(response["body"].read())
            headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
            tokens = int(headers.get("x-amzn-bedrock-input-token-count", 0))
            self._last_tokens = tokens
            vectors = parse_vectors(self.spec, payload)
            if len(vectors) != len(texts):
                raise RuntimeError(f"{self.spec.key} returned {len(vectors)} vectors for {len(texts)} texts")
            return np.asarray(vectors, dtype=np.float32), tokens
        raise RuntimeError("unreachable")


class VectorCache:
    """Vectors by content hash, stored as numbered .npz shards."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.vectors: dict[str, np.ndarray] = {}
        self._pending_hashes: list[str] = []
        self._pending_vectors: list[np.ndarray] = []
        self._shards = 0
        if directory.exists():
            for shard in sorted(directory.glob("shard-*.npz")):
                data = np.load(shard)
                self.vectors.update(zip(data["hashes"].tolist(), data["vectors"]))
                self._shards += 1

    def __contains__(self, key: str) -> bool:
        return key in self.vectors

    def add(self, hashes: list[str], vectors: np.ndarray) -> None:
        for key, vector in zip(hashes, vectors):
            self.vectors[key] = vector
            self._pending_hashes.append(key)
            self._pending_vectors.append(vector)

    @property
    def pending(self) -> int:
        return len(self._pending_hashes)

    def flush(self) -> None:
        if not self._pending_hashes:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self._shards += 1
        path = self.directory / f"shard-{self._shards:05d}.npz"
        np.savez(path, hashes=np.array(self._pending_hashes), vectors=np.stack(self._pending_vectors))
        self._pending_hashes, self._pending_vectors = [], []

    def matrix(self, hashes: list[str]) -> np.ndarray:
        return np.stack([self.vectors[key] for key in hashes])


def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def embed_missing(embedder: Embedder, cache: VectorCache, texts: list[str], kind: Kind, usage_log: Path) -> int:
    """Embed every text whose hash is not cached yet. Returns the number embedded."""
    by_hash = {text_hash(text): text for text in texts}
    missing = [key for key in by_hash if key not in cache]
    logger.info("%s, %s texts: %d cached, %d to embed", embedder.spec.key, kind, len(by_hash) - len(missing), len(missing))
    started = time.monotonic()
    done = 0
    tokens_since_flush = 0
    try:
        for batch in _batches(missing, embedder.spec.batch_size):
            vectors, tokens = embedder.embed([by_hash[key] for key in batch], kind)
            cache.add(batch, vectors)
            done += len(batch)
            tokens_since_flush += tokens
            if cache.pending >= FLUSH_EVERY:
                _flush(cache, usage_log, kind, tokens_since_flush)
                tokens_since_flush = 0
                rate = done / (time.monotonic() - started)
                logger.info("%d / %d, about %.0f min left", done, len(missing), (len(missing) - done) / rate / 60)
    finally:
        _flush(cache, usage_log, kind, tokens_since_flush)
    return done


def _flush(cache: VectorCache, usage_log: Path, kind: Kind, tokens: int) -> None:
    count = cache.pending
    cache.flush()
    if count:
        usage_log.parent.mkdir(parents=True, exist_ok=True)
        with usage_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "texts": count, "tokens": tokens}) + "\n")


def eval_questions(eval_dir: Path) -> list[str]:
    """Dev and test questions. Embedding the test questions is not tuning on them."""
    questions: list[str] = []
    for split in ("dev", "test"):
        for line in (eval_dir / f"{split}.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                questions.append(json.loads(line)["question"])
    return questions


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed chunks and eval questions with a Bedrock model.")
    parser.add_argument("--model", choices=sorted(MODELS), required=True)
    parser.add_argument("--docs", type=Path, default=Path("data/docs"))
    parser.add_argument("--eval-dir", type=Path, default=Path("eval"))
    parser.add_argument("--out", type=Path, default=Path("data/embeddings"))
    parser.add_argument("--limit", type=int, default=None, help="embed at most N chunks (for a trial run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import boto3

    spec = MODELS[args.model]
    embedder = Embedder(spec, boto3.client("bedrock-runtime", region_name=REGION))
    model_dir = args.out / spec.key
    usage_log = model_dir / "usage.jsonl"
    texts = [chunk.text for chunk in load_chunks(args.docs)]
    if args.limit is not None:
        texts = texts[: args.limit]
    try:
        embed_missing(embedder, VectorCache(model_dir / "query"), eval_questions(args.eval_dir), "query", usage_log)
        embed_missing(embedder, VectorCache(model_dir / "document"), texts, "document", usage_log)
    except AuthExpiredError as error:
        raise SystemExit(
            f"AWS credentials expired or missing ({error}).\n"
            "Run `aws sso login --profile jet-admin`, then rerun the same command: it resumes from the cache."
        ) from error


if __name__ == "__main__":
    main()
