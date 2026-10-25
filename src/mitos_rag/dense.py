"""Dense retrieval over cached chunk embeddings, and reciprocal rank fusion.

Exact (brute force) cosine search: 65k chunks x 1,024 dims is one matrix
product per question, no index needed (PRD 6.4). A procedure scores by its best
chunk, like the BM25 section retriever.
"""

from pathlib import Path

import numpy as np

from mitos_rag.chunk import Chunk
from mitos_rag.embed import VectorCache, text_hash

RRF_K = 60  # the usual default from Cormack et al. (2009); not tuned


def _unit_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


class DenseRetriever:
    def __init__(self, chunks: list[Chunk], documents: VectorCache, queries: VectorCache) -> None:
        missing = sum(chunk.content_hash not in documents for chunk in chunks)
        if missing:
            raise ValueError(f"{missing} of {len(chunks)} chunks have no embedding yet: finish mitos-embed first")
        self.procedure_ids = sorted({chunk.procedure_id for chunk in chunks})
        position = {pid: index for index, pid in enumerate(self.procedure_ids)}
        self.chunk_procedure = np.array([position[chunk.procedure_id] for chunk in chunks])
        self.matrix = _unit_rows(documents.matrix([chunk.content_hash for chunk in chunks]))
        self.queries = queries

    @classmethod
    def from_directory(cls, chunks: list[Chunk], model_dir: Path) -> "DenseRetriever":
        return cls(chunks, VectorCache(model_dir / "document"), VectorCache(model_dir / "query"))

    def rank(self, query: str, exclude: frozenset[str] = frozenset()) -> list[tuple[str, float]]:
        key = text_hash(query)
        if key not in self.queries:
            raise ValueError(f"question not embedded, rerun mitos-embed: {query}")
        vector = _unit_rows(self.queries.matrix([key]))[0]
        similarities = self.matrix @ vector
        best = np.full(len(self.procedure_ids), -np.inf, dtype=np.float32)
        np.maximum.at(best, self.chunk_procedure, similarities)
        ranking = [
            (pid, float(score)) for pid, score in zip(self.procedure_ids, best.tolist()) if pid not in exclude
        ]
        return sorted(ranking, key=lambda pair: (-pair[1], pair[0]))


def reciprocal_rank_fusion(rankings: list[list[tuple[str, float]]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Sum of 1 / (k + rank) over the rankings each procedure appears in."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, (pid, _) in enumerate(ranking, start=1):
            fused[pid] = fused.get(pid, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
