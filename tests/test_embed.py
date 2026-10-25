import io
import json
from pathlib import Path

import numpy as np
import pytest
from botocore.exceptions import ClientError, SSOTokenLoadError

from mitos_rag.embed import MODELS, AuthExpiredError, Embedder, VectorCache, embed_missing, request_body, text_hash


class FakeBedrock:
    """Answers invoke_model like Bedrock; can fail the first calls with given error codes."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.calls: list[dict] = []
        self.errors = list(errors or [])

    def invoke_model(self, modelId: str, body: str, contentType: str, accept: str) -> dict:
        if self.errors:
            raise self.errors.pop(0)
        request = json.loads(body)
        self.calls.append(request)
        texts = request["texts"] if "texts" in request else [request["inputText"]]
        vectors = [[float(len(text)), 1.0] for text in texts]
        payload = {"embeddings": vectors} if "texts" in request else {"embedding": vectors[0]}
        return {
            "body": io.BytesIO(json.dumps(payload).encode()),
            "ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": str(10 * len(texts))}},
        }


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "InvokeModel")


def embedder(model: str, client: FakeBedrock, sleeps: list[float] | None = None) -> Embedder:
    clock = iter(range(0, 10_000))
    return Embedder(MODELS[model], client, sleep=(sleeps if sleeps is not None else []).append, clock=lambda: float(next(clock)))


def test_cohere_uses_document_and_query_types() -> None:
    assert request_body(MODELS["cohere"], ["α"], "document")["input_type"] == "search_document"
    assert request_body(MODELS["cohere"], ["α"], "query")["input_type"] == "search_query"
    assert request_body(MODELS["titan"], ["α"], "query") == {"inputText": "α", "dimensions": 1024, "normalize": True}


def test_embed_returns_vectors_and_billed_tokens() -> None:
    vectors, tokens = embedder("cohere", FakeBedrock()).embed(["αβ", "γ"], "document")

    assert vectors.shape == (2, 2)
    assert vectors[0, 0] == 2.0
    assert tokens == 20


def test_batch_size_is_enforced() -> None:
    with pytest.raises(ValueError):
        embedder("titan", FakeBedrock()).embed(["α", "β"], "document")
    with pytest.raises(ValueError):
        embedder("cohere", FakeBedrock()).embed(["α"] * 97, "document")


def test_throttling_is_retried_with_backoff() -> None:
    client = FakeBedrock(errors=[client_error("ThrottlingException"), client_error("ThrottlingException")])
    sleeps: list[float] = []

    vectors, _ = embedder("titan", client, sleeps).embed(["α"], "document")

    assert vectors.shape == (1, 2)
    assert 1.0 in sleeps and 2.0 in sleeps


@pytest.mark.parametrize(
    "error",
    [client_error("ExpiredTokenException"), client_error("UnrecognizedClientException"), SSOTokenLoadError(error_msg="x")],
)
def test_expired_credentials_stop_the_run(error: Exception) -> None:
    with pytest.raises(AuthExpiredError):
        embedder("titan", FakeBedrock(errors=[error])).embed(["α"], "document")


def test_other_errors_are_not_retried() -> None:
    client = FakeBedrock(errors=[client_error("ValidationException")])
    with pytest.raises(ClientError):
        embedder("titan", client).embed(["α"], "document")


def test_requests_are_paced_to_the_quota() -> None:
    sleeps: list[float] = []
    cohere = embedder("cohere", FakeBedrock(), sleeps)  # 20 per minute: 3 s apart

    cohere.embed(["α"], "document")
    cohere.embed(["β"], "document")

    assert sleeps and sleeps[0] == pytest.approx(2.0)  # the fake clock advanced 1 s between calls


def test_large_requests_are_paced_to_the_token_quota() -> None:
    sleeps: list[float] = []
    cohere = embedder("cohere", FakeBedrock(), sleeps)

    cohere.embed(["α"] * 96, "document")  # the fake bills 960 tokens: 0.19 s of quota, under 3 s
    cohere._last_tokens = 30_000  # a real 96-chunk batch: 6 s of a 300k per minute quota
    cohere.embed(["β"], "document")

    assert sleeps[-1] == pytest.approx(5.0)


def test_embed_missing_batches_caches_and_resumes(tmp_path: Path) -> None:
    texts = [f"κείμενο {i}" for i in range(200)] + ["κείμενο 0"]  # one duplicate
    usage = tmp_path / "usage.jsonl"
    client = FakeBedrock()

    cache = VectorCache(tmp_path / "document")
    assert embed_missing(embedder("cohere", client), cache, texts, "document", usage) == 200
    assert [len(call["texts"]) for call in client.calls] == [96, 96, 8]

    reloaded = VectorCache(tmp_path / "document")
    assert len(reloaded.vectors) == 200
    assert reloaded.matrix([text_hash("κείμενο 1")]).shape == (1, 2)
    assert embed_missing(embedder("cohere", client), reloaded, texts, "document", usage) == 0
    assert len(client.calls) == 3
    assert sum(json.loads(line)["tokens"] for line in usage.read_text().splitlines()) == 2000


def test_interrupted_run_keeps_what_was_embedded(tmp_path: Path) -> None:
    client = FakeBedrock()
    titan = embedder("titan", client)
    calls = 0
    original = client.invoke_model

    def fail_on_third(**kwargs: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise client_error("ExpiredTokenException")
        return original(**kwargs)

    client.invoke_model = fail_on_third  # type: ignore[method-assign]
    with pytest.raises(AuthExpiredError):
        embed_missing(titan, VectorCache(tmp_path), ["α", "β", "γ", "δ"], "document", tmp_path / "usage.jsonl")

    assert len(VectorCache(tmp_path).vectors) == 2
    assert np.allclose(VectorCache(tmp_path).matrix([text_hash("α")]), [[1.0, 1.0]])
