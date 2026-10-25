from pathlib import Path

import numpy as np
import pytest

from mitos_rag.chunk import Chunk
from mitos_rag.dense import DenseRetriever, reciprocal_rank_fusion
from mitos_rag.embed import VectorCache, text_hash


def cache(tmp_path: Path, name: str, rows: dict[str, list[float]]) -> VectorCache:
    store = VectorCache(tmp_path / name)
    store.add(list(rows), np.array(list(rows.values()), dtype=np.float32))
    return store


def chunk(pid: str, index: int, text: str) -> Chunk:
    return Chunk(pid, "Κόστος", index, text)


def test_dense_ranks_procedures_by_their_best_chunk(tmp_path: Path) -> None:
    chunks = [chunk("a", 0, "a0"), chunk("a", 1, "a1"), chunk("b", 0, "b0")]
    documents = cache(
        tmp_path,
        "document",
        {c.content_hash: v for c, v in zip(chunks, [[0.0, 1.0], [10.0, 1.0], [1.0, 1.0]])},
    )
    queries = cache(tmp_path, "query", {text_hash("ερώτηση"): [1.0, 0.0]})

    ranking = DenseRetriever(chunks, documents, queries).rank("ερώτηση")

    assert [pid for pid, _ in ranking] == ["a", "b"]
    assert ranking[0][1] == pytest.approx(10 / np.sqrt(101))  # cosine, not raw dot product
    assert DenseRetriever(chunks, documents, queries).rank("ερώτηση", exclude=frozenset({"a"}))[0][0] == "b"


def test_dense_refuses_missing_embeddings(tmp_path: Path) -> None:
    chunks = [chunk("a", 0, "a0")]
    empty = VectorCache(tmp_path / "none")
    with pytest.raises(ValueError):
        DenseRetriever(chunks, empty, empty)


def test_rrf_rewards_agreement_between_rankings() -> None:
    keyword = [("a", 9.0), ("b", 5.0), ("c", 1.0)]
    dense = [("b", 0.9), ("c", 0.8), ("a", 0.1)]

    fused = reciprocal_rank_fusion([keyword, dense], k=60)

    assert [pid for pid, _ in fused] == ["b", "a", "c"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_rrf_keeps_items_found_by_one_ranking_only() -> None:
    fused = reciprocal_rank_fusion([[("a", 1.0)], [("b", 1.0)]], k=60)

    assert [pid for pid, _ in fused] == ["a", "b"]  # equal scores break ties on id
