import json
from pathlib import Path
from typing import Any

import boto3
import numpy as np
import pytest
from botocore.stub import ANY, Stubber

from mitos_rag.chunk import Chunk
from mitos_rag.embed import VectorCache, text_hash
from mitos_rag import vectorstore
from mitos_rag.vectorstore import S3VectorsRetriever, chunk_key, existing_keys, procedure_of, upload, wire_floats


def s3vectors_client() -> Any:
    # A real client checked against the service model by Stubber; it never reaches the network.
    return boto3.client(
        "s3vectors", region_name="eu-central-1", aws_access_key_id="test", aws_secret_access_key="test"
    )


def cache(tmp_path: Path, name: str, rows: dict[str, list[float]]) -> VectorCache:
    store = VectorCache(tmp_path / name)
    store.add(list(rows), np.array(list(rows.values()), dtype=np.float32))
    return store


def chunk(pid: str, index: int) -> Chunk:
    return Chunk(pid, "Κόστος", index, f"{pid}{index}")


def test_keys_name_the_procedure() -> None:
    assert chunk_key(chunk("123456", 7)) == "123456:7"
    assert procedure_of("123456:7") == "123456"


def test_upload_puts_vectors_in_batches_with_procedure_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vectorstore, "PUT_BATCH", 2)
    chunks = [chunk("a", 0), chunk("a", 1), chunk("b", 0)]
    documents = cache(tmp_path, "document", {c.content_hash: [float(i), 1.0] for i, c in enumerate(chunks)})
    client = s3vectors_client()
    with Stubber(client) as stub:
        first = [
            {"key": "a:0", "data": {"float32": [0.0, 1.0]}, "metadata": {"procedure_id": "a"}},
            {"key": "a:1", "data": {"float32": [1.0, 1.0]}, "metadata": {"procedure_id": "a"}},
        ]
        second = [{"key": "b:0", "data": {"float32": [2.0, 1.0]}, "metadata": {"procedure_id": "b"}}]
        for vectors in (first, second):
            stub.add_response(
                "put_vectors", {}, {"vectorBucketName": "bucket", "indexName": "index", "vectors": vectors}
            )
        assert upload(client, "bucket", "index", chunks, documents) == 3
        stub.assert_no_pending_responses()


def test_wire_floats_rebuild_the_same_float32() -> None:
    vector = np.random.default_rng(0).standard_normal(1024).astype(np.float32) * 0.05
    assert np.array_equal(np.array(wire_floats(vector), dtype=np.float32), vector)
    assert len(json.dumps(wire_floats(vector))) < 0.75 * len(json.dumps(vector.tolist()))


def test_upload_resumes_by_skipping_existing_keys(tmp_path: Path) -> None:
    chunks = [chunk("a", 0), chunk("a", 1)]
    documents = cache(tmp_path, "document", {c.content_hash: [float(i), 1.0] for i, c in enumerate(chunks)})
    client = s3vectors_client()
    with Stubber(client) as stub:
        stub.add_response(
            "list_vectors",
            {"vectors": [{"key": "a:0"}], "nextToken": "next"},
            {"vectorBucketName": "bucket", "indexName": "index"},
        )
        stub.add_response(
            "list_vectors", {"vectors": []}, {"vectorBucketName": "bucket", "indexName": "index", "nextToken": "next"}
        )
        stub.add_response(
            "put_vectors",
            {},
            {
                "vectorBucketName": "bucket",
                "indexName": "index",
                "vectors": [{"key": "a:1", "data": {"float32": [1.0, 1.0]}, "metadata": {"procedure_id": "a"}}],
            },
        )
        skip = frozenset(existing_keys(client, "bucket", "index"))
        assert skip == {"a:0"}
        assert upload(client, "bucket", "index", chunks, documents, skip) == 1
        stub.assert_no_pending_responses()


def test_upload_refuses_missing_embeddings(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        upload(s3vectors_client(), "bucket", "index", [chunk("a", 0)], VectorCache(tmp_path / "none"))


def test_rank_scores_procedures_by_best_chunk_and_follows_pages(tmp_path: Path) -> None:
    queries = cache(tmp_path, "query", {text_hash("ερώτηση"): [1.0, 0.0]})
    client = s3vectors_client()
    request = {
        "vectorBucketName": "bucket",
        "indexName": "index",
        "queryVector": {"float32": [1.0, 0.0]},
        "topK": 4,
        "returnDistance": True,
    }
    with Stubber(client) as stub:
        stub.add_response(
            "query_vectors",
            {
                "distanceMetric": "cosine",
                "vectors": [{"key": "a:1", "distance": 0.1}, {"key": "b:0", "distance": 0.2}],
                "nextToken": "page-2",
            },
            request,
        )
        stub.add_response(
            "query_vectors",
            {"distanceMetric": "cosine", "vectors": [{"key": "a:0", "distance": 0.5}, {"key": "c:3", "distance": 0.6}]},
            {**request, "nextToken": "page-2"},
        )
        ranking = S3VectorsRetriever(client, "bucket", "index", queries, top_k=4).rank("ερώτηση")
        stub.assert_no_pending_responses()

    assert [pid for pid, _ in ranking] == ["a", "b", "c"]
    assert ranking[0][1] == pytest.approx(0.9)  # cosine similarity = 1 - distance, best chunk wins


def test_rank_excludes_procedures_and_refuses_unembedded_questions(tmp_path: Path) -> None:
    queries = cache(tmp_path, "query", {text_hash("ερώτηση"): [1.0, 0.0]})
    client = s3vectors_client()
    with Stubber(client) as stub:
        stub.add_response(
            "query_vectors",
            {"distanceMetric": "cosine", "vectors": [{"key": "a:0", "distance": 0.1}, {"key": "b:0", "distance": 0.2}]},
            {"vectorBucketName": "bucket", "indexName": "index", "queryVector": ANY, "topK": 100, "returnDistance": True},
        )
        retriever = S3VectorsRetriever(client, "bucket", "index", queries)
        assert retriever.rank("ερώτηση", exclude=frozenset({"a"})) == [("b", pytest.approx(0.8))]
    with pytest.raises(ValueError):
        retriever.rank("άλλη ερώτηση")


def test_rank_refuses_a_non_cosine_index(tmp_path: Path) -> None:
    queries = cache(tmp_path, "query", {text_hash("ερώτηση"): [1.0, 0.0]})
    client = s3vectors_client()
    with Stubber(client) as stub:
        stub.add_response("query_vectors", {"distanceMetric": "euclidean", "vectors": []}, None)
        with pytest.raises(ValueError):
            S3VectorsRetriever(client, "bucket", "index", queries).rank("ερώτηση")


def test_nearest_chunks_stops_paging_once_enough_procedures_are_found(tmp_path: Path) -> None:
    client = s3vectors_client()
    request = {
        "vectorBucketName": "bucket",
        "indexName": "index",
        "queryVector": {"float32": [1.0, 0.0]},
        "topK": 300,
        "returnDistance": True,
    }
    with Stubber(client) as stub:
        stub.add_response(
            "query_vectors",
            {"distanceMetric": "cosine", "vectors": [{"key": "a:0", "distance": 0.1}, {"key": "a:1", "distance": 0.2}], "nextToken": "p2"},
            request,
        )
        stub.add_response(
            "query_vectors",
            {"distanceMetric": "cosine", "vectors": [{"key": "b:0", "distance": 0.3}], "nextToken": "p3"},
            {**request, "nextToken": "p2"},
        )
        retriever = S3VectorsRetriever(client, "bucket", "index", VectorCache(tmp_path / "q"))
        hits = retriever.nearest_chunks(np.array([1.0, 0.0]), top_k=300, enough_procedures=2)
        stub.assert_no_pending_responses()  # page 3 was never asked for

    assert [key for key, _ in hits] == ["a:0", "a:1", "b:0"]
