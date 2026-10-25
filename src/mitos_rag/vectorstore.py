"""Chunk embeddings in Amazon S3 Vectors (decision 005).

The vector bucket and index come from Terraform (`terraform/project`); this
module only writes and queries vectors. Each chunk is one vector keyed
`<procedure_id>:<chunk_index>`, so a query result names its procedure without
asking for metadata. `procedure_id` is also stored as filterable metadata for
later filters.

QueryVectors is approximate nearest neighbour search over chunks. A procedure
scores by its best chunk among the top K returned, like `DenseRetriever`; on
dev, cutting the exact ranking to the top 30, 100 or 1,000 chunks changed no
metric (005).

Usage:
    uv run mitos-index --bucket mitos-rag --index chunks-cohere-v3-20261014
"""

import argparse
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

from mitos_rag.chunk import Chunk, load_chunks
from mitos_rag.embed import REGION, VectorCache, text_hash

logger = logging.getLogger(__name__)

# PutVectors takes up to 500 vectors, but 500 is about 11 MB of JSON: from a home
# connection that outlasted the server's wait (RequestTimeoutException, 005).
PUT_BATCH = 100
PAGE_SIZE = 100  # QueryVectors maximum results per page
TOP_K = 100


def chunk_key(chunk: Chunk) -> str:
    return f"{chunk.procedure_id}:{chunk.chunk_index}"


def procedure_of(key: str) -> str:
    return key.rsplit(":", 1)[0]


def put_bytes(chunks: list[Chunk], dimension: int) -> int:
    """Roughly what S3 Vectors bills as bytes put: float32 data, key and metadata."""
    return sum(4 * dimension + len(chunk_key(c).encode()) + len(c.procedure_id.encode()) + 12 for c in chunks)


def wire_floats(vector: np.ndarray) -> list[float]:
    """float32 values with 9 significant digits: enough to rebuild the same float32, a third less JSON."""
    return [float(f"{value:.9g}") for value in vector.astype(np.float32).tolist()]


def existing_keys(client: Any, bucket: str, index: str) -> set[str]:
    """Keys already in the index, to resume an interrupted upload (1,000 keys per ListVectors call)."""
    keys: set[str] = set()
    for page in client.get_paginator("list_vectors").paginate(vectorBucketName=bucket, indexName=index):
        keys.update(vector["key"] for vector in page["vectors"])
    return keys


def _batches(chunks: list[Chunk], size: int) -> Iterator[list[Chunk]]:
    for start in range(0, len(chunks), size):
        yield chunks[start : start + size]


def upload(
    client: Any,
    bucket: str,
    index: str,
    chunks: list[Chunk],
    documents: VectorCache,
    skip: frozenset[str] = frozenset(),
) -> int:
    """Put every chunk vector whose key is not in `skip`. PutVectors overwrites by key, so reruns are safe."""
    missing = sum(chunk.content_hash not in documents for chunk in chunks)
    if missing:
        raise ValueError(f"{missing} of {len(chunks)} chunks have no embedding yet: finish mitos-embed first")
    keys = [chunk_key(chunk) for chunk in chunks]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate chunk keys")
    todo = [chunk for chunk in chunks if chunk_key(chunk) not in skip]
    written = 0
    for batch in _batches(todo, PUT_BATCH):
        vectors = [
            {
                "key": chunk_key(chunk),
                "data": {"float32": wire_floats(documents.vectors[chunk.content_hash])},
                "metadata": {"procedure_id": chunk.procedure_id},
            }
            for chunk in batch
        ]
        client.put_vectors(vectorBucketName=bucket, indexName=index, vectors=vectors)
        written += len(batch)
        logger.info("put %d / %d vectors", written, len(todo))
    return written


class S3VectorsRetriever:
    def __init__(self, client: Any, bucket: str, index: str, queries: VectorCache, top_k: int = TOP_K) -> None:
        self.client = client
        self.bucket = bucket
        self.index = index
        self.queries = queries
        self.top_k = top_k

    def nearest_chunks(
        self, vector: np.ndarray, top_k: int | None = None, enough_procedures: int | None = None
    ) -> list[tuple[str, float]]:
        """(key, cosine similarity) for the approximate top K chunks, following pages.

        With `enough_procedures`, paging stops once that many distinct procedures are found:
        the reranker needs a number of procedures, and chunks of one procedure cluster.
        """
        top_k = top_k or self.top_k
        request: dict[str, Any] = {
            "vectorBucketName": self.bucket,
            "indexName": self.index,
            "queryVector": {"float32": vector.astype(np.float32).tolist()},
            "topK": top_k,
            "returnDistance": True,
        }
        found: list[tuple[str, float]] = []
        procedures: set[str] = set()
        while True:
            response = self.client.query_vectors(**request)
            if response.get("distanceMetric", "cosine") != "cosine":
                raise ValueError(f"index uses {response['distanceMetric']}, expected cosine")
            found += [(hit["key"], 1.0 - float(hit["distance"])) for hit in response["vectors"]]
            procedures.update(procedure_of(hit["key"]) for hit in response["vectors"])
            token = response.get("nextToken")
            if not token or len(found) >= top_k:
                return found
            if enough_procedures is not None and len(procedures) >= enough_procedures:
                return found
            request["nextToken"] = token

    def rank(self, query: str, exclude: frozenset[str] = frozenset()) -> list[tuple[str, float]]:
        key = text_hash(query)
        if key not in self.queries:
            raise ValueError(f"question not embedded, rerun mitos-embed: {query}")
        best: dict[str, float] = {}
        for chunk, similarity in self.nearest_chunks(self.queries.matrix([key])[0]):
            pid = procedure_of(chunk)
            if pid not in exclude and similarity > best.get(pid, -np.inf):
                best[pid] = similarity
        return sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload cached chunk embeddings to an S3 Vectors index.")
    parser.add_argument("--bucket", required=True, help="vector bucket name (Terraform output)")
    parser.add_argument("--index", required=True, help="vector index name (Terraform output)")
    parser.add_argument("--model", default="cohere")
    parser.add_argument("--docs", type=Path, default=Path("data/docs"))
    parser.add_argument("--embeddings", type=Path, default=Path("data/embeddings"))
    parser.add_argument("--dry-run", action="store_true", help="only print the count and estimated bytes put")
    parser.add_argument("--resume", action="store_true", help="skip keys already in the index (ListVectors calls)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    chunks = load_chunks(args.docs)
    documents = VectorCache(args.embeddings / args.model / "document")
    dimension = len(next(iter(documents.vectors.values())))
    estimate = put_bytes(chunks, dimension)
    print(f"{len(chunks)} vectors x {dimension} dims, about {estimate / 1e9:.3f} GB put")
    if args.dry_run:
        return

    import boto3

    client = boto3.client("s3vectors", region_name=REGION)
    skip = frozenset(existing_keys(client, args.bucket, args.index)) if args.resume else frozenset()
    if args.resume:
        print(f"{len(skip)} vectors already in the index, skipping them")
    written = upload(client, args.bucket, args.index, chunks, documents, skip)
    print(f"put {written} vectors into {args.bucket}/{args.index}")


if __name__ == "__main__":
    main()
