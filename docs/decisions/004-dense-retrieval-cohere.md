# 004: Dense retrieval with Cohere Embed Multilingual v3

Status: measured 2026-10-14 on dev, confirmed once on the held-out test split. **Adopted (2026-10-14): dense-only Cohere retrieval for Phase 4.** It meets the hit@5 target on dev and test (0.89 on both). Titan is skipped for now. Equal-weight hybrid (RRF) trades hit@5 for hit@1 and is not recommended (see finding 3). Titan V2 was not run (see "Titan").

## Setup

- **Chunks** (`chunk.py`): one chunk per section. Sections over 1,500 characters are split on line boundaries, with up to 200 characters of line overlap. 13,514 of the 21,536 split points carry overlap. The others fall where the last line alone is longer than 200 characters. Every chunk starts with the title, the alternative titles (cut at 400 characters) and the section heading. That makes 65,312 chunks and about 54M characters.
- **Why 1,500 characters**: Cohere v3 takes at most 512 tokens per text. A 2,000-character Greek chunk failed with `truncate: NONE`. At 1,500 characters, two probes were run:
  - The 3 most link- and law-heavy chunks failed. Their Latin/digit share was 44 to 58%.
  - In 40 chunks sampled from those of 1,000+ characters (42% of all chunks), 1 failed. Its Latin/digit share was only 7%, so the cause of that one is unknown.

  That suggests roughly 1% of the corpus goes over the limit, with wide uncertainty at n = 40. The run uses `truncate: END`, so those chunks keep their start (title, heading, first lines) and lose their tail.
- **Model**: `cohere.embed-multilingual-v3`, eu-central-1, on-demand, 1,024 dimensions. Chunks use `search_document`, questions `search_query`. The account allows 20 requests per minute and 300k tokens per minute. The run took 60 minutes.
- **Retrieval** (`dense.py`): exact cosine over all chunks, and a procedure scores by its best chunk. Hybrid is reciprocal rank fusion (k = 60, untuned) of the BM25 section/normalised ranking from 002 and the dense ranking. The fusion happens at procedure level, so BM25 can run on sections while dense runs on chunks.
- **Cost**: 16,675,850 billed tokens for the 65,302 chunks and 30 questions, at $0.10 per million: **$1.67**. Cohere averages about 3.2 characters per token on this corpus.
- Commands: `uv run mitos-embed --model cohere`, then `uv run mitos-eval --split dev --dense cohere`.

## Results (dev, 18 scored items)

| config | hit@1 | hit@5 | hit@10 | MRR | recall@5 | recall@10 |
|---|---|---|---|---|---|---|
| BM25 section/normalised (002) | 0.39 | 0.50 | 0.56 | 0.43 | 0.47 | 0.50 |
| **dense/cohere** | **0.56** | **0.89** | **0.94** | **0.70** | **0.79** | **0.88** |
| hybrid/cohere (RRF) | 0.61 | 0.78 | 0.89 | 0.69 | 0.65 | 0.76 |

hit@5 by category:

| category (n) | BM25 | dense | hybrid |
|---|---|---|---|
| exact_lookup (3) | 1.00 | 1.00 | 1.00 |
| natural_greek (5) | 0.60 | 1.00 | 0.60 |
| fact_extraction (5) | 0.40 | 0.80 | 0.80 |
| multi_procedure (2) | 0.00 | 1.00 | 0.50 |
| ambiguous (3) | 0.33 | 0.67 | 1.00 |

The MVP target is hit@5 ≥ 0.85 (PRD 8.3). Dense meets it on dev. Dev has 18 items, so this needs confirming on the held-out test split and on more questions.

## Findings

1. **Embeddings close most of the vocabulary gap.** Of the 8 misses that 002 tagged as vocabulary, 6 are now in the top 5. Examples:
   - q06 χαρτια για διαβατηριο: rank 86 to 4.
   - q08 εχασα την ταυτοτητα: rank 1,919 to 1.
   - q18 διαβατηριο απο το ιντερνετ: rank 117 to 3.
   - q22 γεννηθηκε το μωρο μας: rank 883 to 2.

   q15 and q24 still miss (below). The one inflection-only miss, q25 ταυτοτητα, went from rank 8 to 1.
2. **Two dense misses remain:**
   - q15 "ποσο κανει η ανανεωση του διπλωματος", rank 6. Residence permit renewals (άδεια διαμονής) outrank it. The question never says οδήγηση.
   - q24 "χαρτια για συνταξη", rank 13. The top hit is "Χορήγηση Χαρτιού ... στα Πολιτικά Κόμματα", which matches the word χαρτί itself.
3. **Equal-weight RRF is a tradeoff, not a gain.** Against dense, hybrid is better on hit@1 (0.61 vs 0.56), level on MRR (0.69 vs 0.70), and worse on hit@5 (0.78 vs 0.89) and recall. At hit@5:
   - Hybrid-only hits: q15 (6 to 4) and q24 (13 to 5).
   - Dense-only hits: q06 (4 to 8), q08 (1 to 12), q18 (3 to 6) and q22 (2 to 14).

   The net is 2 items out of 18. Hybrid also lifts q14 (5 to 1) and q21 (2 to 1) within the top 5.

   The dense-only hits are the vocabulary-gap questions, where BM25 ranks the right procedure in the hundreds or thousands. RRF rewards agreement, so a procedure ranked moderately by both retrievers beats one ranked first by dense and nowhere by BM25. Weighted fusion, or using BM25 only for exact lookups, might fix this. Tuning a weight on 18 items would fit noise, so that waits for a bigger eval set.
4. **Dense similarity seemed to separate out-of-scope questions on dev, but not on test (see below).** These are top-1 cosines:
   - Weather question (q28): 0.454.
   - Accountant question (q29): 0.524.
   - Answered items: minimum 0.576, median 0.675.

   On dev, a threshold near 0.55 would separate them. BM25 scores could not do this (002). With only 2 out-of-scope items this is a hint for Phase 4, not a setting.

## Held-out test split (run once, 2026-10-14, as a report)

After dense-only Cohere was adopted, the test split was run once with `uv run mitos-eval --split test --dense cohere`. Nothing was tuned on it.

| config | hit@1 | hit@5 | hit@10 | MRR | recall@5 | recall@10 |
|---|---|---|---|---|---|---|
| BM25 section/normalised | 0.44 | 0.67 | 0.67 | 0.53 | 0.56 | 0.56 |
| **dense/cohere** | **0.89** | **0.89** | **0.89** | **0.89** | **0.83** | **0.89** |
| hybrid/cohere (RRF) | 0.67 | 0.67 | 0.67 | 0.69 | 0.61 | 0.67 |

- **Dense holds at hit@5 0.89** (8 of 9 scored items), the same as dev.
  - It ranks first on q09 "μου εκλεψαν την ταυτοτητα" (BM25: rank 1,368).
  - It ranks first on q12 "αλλαξα σπιτι πως ενημερωνω την εφορια" (BM25: rank 3,783).
- **The one miss is q23** "πεθανε ο πατερας μου τι χαρτια χρειαζομαι", a multi-procedure question. The first expected procedure is at rank 32. The top results are other death-related procedures: bereavement aid, funeral expenses, a pension certificate for funeral costs. They are plausible neighbours, not the labelled survivor's pension and inheritance procedures.
- **Hybrid is worse again** (0.67), which confirms finding 3.
- **The not-found hint from dev does not hold.**
  - The out-of-scope question q30 ("ποσο φορο θα πληρωσω φετος με μισθο 1200", a personal tax calculation) scores 0.560.
  - The lowest answered item scores 0.566.
  - The 0.55 threshold suggested by dev would not catch it. Status detection needs the decision step in Phase 4 (PRD 6.4 step 4), not a similarity cut-off.

Combined dev and test: 24 of 27 scored items have a relevant procedure in the top 5.

## Titan

Titan Text Embeddings V2 was the planned second model. Probe calls showed that it tokenizes Greek at about 1.1 characters per token, against Cohere's 3.2, and AWS lists Greek as a preview language for it. A full run is about 49M tokens:

| | cost | time |
|---|---|---|
| On-demand (60 requests per minute, one text per request) | about $9.80 | about 18 h |
| Batch inference (needs an S3 bucket and an IAM role in Terraform) | about $4.90 | runs on AWS's side |

Cohere already meets the dev target, so Titan is deferred. Run it only if the test split or new questions show Cohere falling short.

The same tokenization makes Titan about 6 times more expensive per question in production: roughly 3 times the tokens at twice the price per token. That matches the full-corpus figures ($9.80 vs $1.67).

## Options

1. **Adopt dense-only Cohere** as the retrieval for Phase 4 (`mitos-ask`). Keep BM25 in the code as the baseline, not in the pipeline.
2. **Confirm on the held-out test split**: done, hit@5 0.89 (see above).
3. **Not measured yet:** per-question latency of the Cohere call. PRD 7.1 asks for quality and latency. Log it when `mitos-ask` exists.
4. **Production note:** each question needs one Cohere call. That is about 10 to 30 tokens, a fraction of a cent, within the 20 requests per minute quota for a demo. The vectors (65k × 1,024 floats, about 270 MB) need a home: the vector store decision (PRD 7.2) comes next.
