# 002: BM25 baseline, granularity and normalisation

Status: measured 2026-10-14 on the dev split (18 scored items, 2 out-of-scope). Normalisation **adopted**. Granularity: **section**, chosen on 2026-10-14. Option 2 (stemmer first) was tried and rejected in 003.

Snapshot: the 2026-10-14 corpus from 001 (4,406 procedures). Command: `uv run mitos-eval --split dev`. Per-question output goes to `data/eval/bm25-dev.json` (git-ignored). The test split was not run.

## Setup

- **BM25**: Okapi, k1 = 1.5, b = 0.75, written out in `bm25.py`. Textbook defaults, not tuned: with 18 items, tuning would fit the noise.
- **Tokens**: `\w+` after casefolding, which already maps final sigma ς to σ. *Raw* stops there. *Normalised* also strips accents and diaeresis (`text.py`). So raw vs normalised measures accents alone. No stemming and no stopword list.
- **Granularity**:
  - *Doc* indexes each rendered Markdown file as one unit.
  - *Section* makes one unit per `##` section, each prefixed with the title and the alternative titles (PRD 6.3). The header block (title, id, dates) is a unit too. That gives 43,776 units.
  - A procedure's score is its **best** unit (max, not sum), so five matching sections count once (PRD 6.4). Ties break on procedure id.
- **Corpus**: *all* procedures, or *external*, which drops the 667 internal ones from 001. The filter removes no gold or acceptable id.
- **Boilerplate**: the attribution line is in every doc. Indexed as-is, "μητρωο", "διαδικασιων" and "εθνικο" occur in all 4,406 docs and get zero weight, which hurts every "ποινικό μητρώο" query. It is now stripped before indexing. Effect on normalised runs: doc hit@5 went from 0.22 to 0.28 and section MRR from 0.40 to 0.43. No other config changed.
- **Metrics** are defined in `eval/README.md`. One item is worth 0.056 of hit@5, so differences under about 0.1 are within noise.

## Results (dev)

| config | hit@1 | hit@5 | hit@10 | MRR | recall@5 | recall@10 |
|---|---|---|---|---|---|---|
| doc/raw/all | 0.06 | 0.11 | 0.11 | 0.08 | 0.12 | 0.12 |
| doc/raw/external | 0.06 | 0.11 | 0.11 | 0.08 | 0.12 | 0.12 |
| doc/normalised/all | 0.17 | 0.28 | 0.39 | 0.22 | 0.24 | 0.35 |
| doc/normalised/external | 0.22 | 0.28 | 0.39 | 0.26 | 0.24 | 0.35 |
| section/raw/all | 0.06 | 0.06 | 0.06 | 0.06 | 0.06 | 0.06 |
| section/raw/external | 0.06 | 0.06 | 0.06 | 0.06 | 0.06 | 0.06 |
| **section/normalised/all** | **0.39** | **0.50** | **0.56** | **0.43** | **0.47** | **0.50** |
| section/normalised/external | 0.39 | 0.50 | 0.56 | 0.44 | 0.47 | 0.50 |

hit@5 by category, normalised, all procedures:

| category (n) | doc | section |
|---|---|---|
| exact_lookup (3) | 0.67 | 1.00 |
| natural_greek (5) | 0.20 | 0.60 |
| fact_extraction (5) | 0.20 | 0.40 |
| multi_procedure (2) | 0.00 | 0.00 |
| ambiguous (3) | 0.33 | 0.33 |

The MVP target is hit@5 ≥ 0.85 (PRD 8.3). The best BM25 config is at 0.50.

## Findings

1. **Accent folding is not optional.** Without it, hit@5 is 0.06 to 0.11, because every question is typed without accents and every document has them. With it, hit@5 is 0.50. Adopted.
2. **Section beats doc.** It is equal or better on every metric, roughly double on hit@1 and MRR. Gains are clear on q02 (rank 47 to 1), q20 (89 to 1), q24 (197 to 23) and q25 (175 to 8). In a short section prefixed with the title, the title words carry real weight. In a whole document, length normalisation dilutes them. Section is worse only on q06, q18, q21 and q22, which are misses at both granularities. At hit@5, four items are hits only under section (q02, q07, q10, q20) and none only under doc. With n = 18 that is a lead, not statistical significance.
3. **The internal filter changes nothing measurable.** hit@5, hit@10 and recall are identical. Doc hit@1 moves by one item (0.17 to 0.22), MRR moves by 0.01 to 0.04, and missed gold ids move up a few ranks. It stays an option (001).
4. **BM25 scores cannot detect out-of-scope questions.** At section level, the weather question (q28) and the accountant question (q29) both score 16.3 at top-1. The median answered question scores 15.5. No threshold separates them. `not_found` needs another signal: dense similarity, or the decision step (PRD 6.4 step 4).

## Why the misses miss

The table covers the 9 dev items with no relevant procedure in the top 5 (section, normalised, all). "In gold" lists the question tokens that appear anywhere in the gold document after normalisation.

| item | question | rank | in gold | cause |
|---|---|---|---|---|
| q06 | τι χαρτια θελω για διαβατηριο | 86 | για, διαβατηριο | vocabulary: χαρτια (0 docs) vs δικαιολογητικά; διαβατηριο is in 307 docs |
| q08 | εχασα την ταυτοτητα μου τι κανω | 1,919 | την | vocabulary: εχασα vs απώλεια. Inflection: the gold says ταυτότητας, never ταυτότητα |
| q14 | ποσο κοστιζει διαβατηριο για παιδι 10 χρονων | 61 | διαβατηριο, για, 10 | vocabulary: κοστιζει, παιδι, χρονων are rare or absent; the doc says Κόστος, έως 14 ετών |
| q15 | ποσο κανει η ανανεωση του διπλωματος | 56 | η, ανανεωση, του | vocabulary: δίπλωμα vs άδεια οδήγησης (noted on the item) |
| q18 | μπορω να βγαλω διαβατηριο απο το ιντερνετ | 117 | να, διαβατηριο, απο, το | vocabulary: βγαλω, ιντερνετ appear in 0 docs; the registry says έκδοση and ψηφιακά |
| q21 | εχασα ταυτοτητα και διαβατηριο μαζι | 48 | (best gold) | vocabulary: εχασα. Inflection: 880224 only has ταυτότητας (rank 2,810) |
| q22 | γεννηθηκε το μωρο μας τι πρεπει να κανουμε | 883 | το, πρεπει, να | vocabulary: μωρο (0 docs), γεννηθηκε vs γέννηση, ονοματοδοσία |
| q24 | χαρτια για συνταξη | 23 | για, συνταξη | inflection: σύνταξης in titles. Vocabulary: χαρτια. Also ambiguous by design |
| q25 | ταυτοτητα | 8 | | inflection: 5 of the 6 ID card procedures never contain ταυτότητα, only ταυτότητας |

Two causes. Several items have both:

- **Vocabulary gap** (8 of 9): everyday words that never appear in the registry (χαρτια, μωρο, εχασα, δίπλωμα, ιντερνετ). Keyword search cannot fix this. Dense embeddings are the planned answer (Phase 3).
- **Inflection** (4 of 9): the question uses the nominative and the document uses the genitive (ταυτότητα vs ταυτότητας, σύνταξη vs σύνταξης). A Greek stemmer, or character n-grams, could fix this inside BM25.

The noise of question words (ποσο, για, να, το) also shows up, but no item misses because of it alone.

## Options

1. **Granularity.** Section has the better numbers. Building Phase 3 on sections means the chunker is the PRD 6.3 design: split very long sections with overlap. It also keeps 512-token embedding models in play, but only with that splitting: no whole doc fits in 512 tokens (001), and the two biggest section types already have medians above 512 before the title prefix (Βήματα 674, Δικαιολογητικά 565). Doc granularity would need an 8k-context model to embed whole docs, and it is behind on BM25. The dev set is small, so treat "section" as a strong lead, not proof.
2. **Next stage to measure against this baseline.** Each would get its own before/after entry:
   - (a) Dense embeddings plus RRF (PRD Phase 3). This targets the larger cause, vocabulary.
   - (b) A Greek stemmer in the BM25 tokenizer. This targets inflection and is cheap to try.

   Both can be measured. Doing (b) first gives hybrid retrieval a stronger keyword side.

   Update: (b) was tried and made section retrieval worse (003), so (a) is next.
3. **Internal filter.** No evidence either way on this dev set. Keep it off until civil-servant questions are in the set.
