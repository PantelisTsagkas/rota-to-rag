"""Okapi BM25 over pre-tokenised documents.

Written out rather than taken from a library: it is short, has no dependencies,
and the baseline should be easy to read. Parameters are the textbook defaults
and are not tuned on the eval set.
"""

import math
from collections import Counter, defaultdict


class BM25:
    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_lengths = [len(tokens) for tokens in documents]
        self.avg_length = sum(self.doc_lengths) / len(documents) if documents else 0.0
        # term -> [(doc index, term frequency)]
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for index, tokens in enumerate(documents):
            for term, frequency in Counter(tokens).items():
                self.postings[term].append((index, frequency))
        count = len(documents)
        # The "+ 1" form of IDF never goes negative, even for very common terms.
        self.idf = {
            term: math.log((count - len(postings) + 0.5) / (len(postings) + 0.5) + 1)
            for term, postings in self.postings.items()
        }

    def scores(self, query: list[str]) -> dict[int, float]:
        """Score of every document that shares at least one term with the query."""
        result: dict[int, float] = defaultdict(float)
        for term in set(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for index, frequency in self.postings[term]:
                length_norm = 1 - self.b + self.b * self.doc_lengths[index] / self.avg_length
                result[index] += idf * frequency * (self.k1 + 1) / (frequency + self.k1 * length_norm)
        return dict(result)
