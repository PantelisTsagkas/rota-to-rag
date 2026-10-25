# 008: Rerank the dense candidates with Haiku 4.5

Status: retrieval measured on dev, 2026-10-17. The experiment ran both rerankers. The Haiku reranker is now in `mitos-ask`, with answer results in [009](009-rerank-in-the-pipeline.md). The test split has not been run.

## The problem is ranking, not chunking

Free checks on the cached Cohere vectors (local exact search, dev, 18 labelled questions):

- The right procedure is always among the candidates: hit@20 is 1.00 and hit@8 0.94, against 0.89 at hit@5. The gap is ordering within the top few.
- The top scores are a near tie. For "ποσο κανει η ανανεωση του διπλωματος", the top 8 procedures score between 0.665 and 0.707.
- Ranking procedures by their title chunk, alone or blended with the best chunk, is no better (hit@5 0.67 to 0.89, MRR 0.55 to 0.69), so re-chunking would only reshuffle the tie.

Two causes, both in the corpus and the questions rather than in the chunks:

1. **Everyday words.** Citizens write "δίπλωμα" where the registry has "άδεια οδήγησης", and "χαρτιά" where it has "δικαιολογητικά". q24 ("χαρτια για συνταξη") ranks "Χορήγηση Χαρτιού ... στα Πολιτικά Κόμματα" first.
2. **Look-alike procedures for special groups.** "Ανανέωση" appears in 94 titles, and "Άδεια διαμονής" in 58. In q15 the residence-permit renewals for foreigners push the driving licence renewal to rank 6. In q06 refugee travel documents rank above the Greek passport.

## The experiment (`rerank.py`, `mitos-rerank-eval`)

The dense top 30 procedures are reordered by a reranker. Each candidate is its title and alternative titles (at most 400 characters).

- **haiku**: Claude Haiku 4.5, structured output, temperature 0. It returns the 10 best ids in order, and the rest follow in dense order. The prompt has general rules only: citizens use everyday words, assume an ordinary adult citizen in Greece unless the question names a special group, and cover each part of a question that asks about several things. It has no synonyms from the eval set.
- **cohere**: Cohere Rerank 3.5 through InvokeModel (`api_version` 2), all 30 candidates.

Responses are cached in `data/rerank/cache`, so re-scoring is free.

## Results (dev, 18 questions with labels)

| | dense (now) | + Haiku 4.5 | + Cohere Rerank 3.5 |
|---|---|---|---|
| hit@5 | 0.89 | **1.00** | 0.89 |
| MRR | 0.70 | **1.00** | 0.76 |
| every expected procedure in the top 5 | 13 of 18 | 14 of 18 | 13 of 18 |
| the same, for the 4 multi-procedure questions | 0 of 4 | 0 of 4 | 1 of 4 |
| misses at 5 (rank) | q15 (6), q24 (13) | none | q06 (9), q15 (13) |
| cost for the run | free | $0.064 | $0.036 |
| quota in eu-central-1 | | 10 requests/min, shared with the answer calls | 3 requests/min |

Haiku put an expected or acceptable procedure at rank 1 for all 18 questions. In q15 it put the driving licence renewal first, in q06 the Greek passport, and in q24 the public-sector old-age pension. Cohere Rerank helped some questions (q14, q20, q24) but buried q06 and q15 further, because it matches words and knows nothing about the ordinary citizen.

**What Haiku does not fix: questions that need several procedures.**
- q21 (lost ID card and passport): the ID-card replacement 880224 is still not in the top 5, and refugee travel documents take places 3 and 4, against the prompt's own rule.
- q22 (newborn): the top 5 becomes the birth record, the birth certificate and citizenship. The name declaration and the birth allowance, which dense search had found, drop out.

None of the arms covers these questions: dense scores 0 of 4. Adding the dense top 3 to Haiku's top 5 was checked for free and changes nothing (still 14 of 18), so it is not adopted. These questions need their own fix, for example splitting the question into its parts before retrieval.

## Caveats

- **18 questions, written by Claude** (`eval/README.md`). An MRR of 1.00 from a Claude reranker on Claude-written questions may be optimistic. The held-out test split (10 questions) is the real check: run it once, after adoption.
- The experiment reranks the local exact top 30. In the pipeline the candidates come from S3 Vectors, whose order differs slightly (005), so the cache will not cover the first pipeline run.
- Latency was not measured. One more Haiku call with about 2,900 input and 100 output tokens should add roughly 1 to 2 s.

## Cost

- The dry run with CountTokens was free: 51,566 Haiku input tokens, quoted at $0.11 for both arms.
- Actual: Haiku $0.064 and Cohere $0.036, so **$0.10**. **Project spend is about $3.29.**
- The first Cohere Rerank call created a Marketplace subscription. In production, Haiku reranking adds about $0.004 per question.

## Open questions

- **Adopt the Haiku reranker in `mitos-ask`:** S3 Vectors top 30, then Haiku, then the top 5 to the answer model. Then run the answer eval on dev, about $0.35 with Haiku answering and $0.96 with Sonnet. Then run the test split once.
- **The answer model** (007) is worth re-deciding after that run. The q15 confident wrong answer came from a retrieval miss, and the reranker removes that miss.
- **Multi-procedure questions** (q21, q22): a later step.
- **Cohere Rerank** is not adopted. Its subscription can stay unused, since it costs nothing without calls.
