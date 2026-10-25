# 001: Corpus profile and what it means for retrieval granularity

Status: measured 2026-10-14. The granularity choice is **open**. The Phase 2 BM25 baseline is in 002: section leads on every metric.

Snapshot: 4,406 of 4,406 procedures downloaded 2026-10-14. Scripts: `scripts/field_coverage.py` (fields), `scripts/doc_sizes.py` (sizes).

## Layout

All 4,406 records keep their list sections (`process_evidences`, `process_steps`, ...) directly under `metadata`. None nest them under `metadata.process`. `get_section` still checks both places. One record (275554) sends `process_provision_digital_locations` as a numbered dict (`{"1": {...}}`) instead of a list. `get_section` now accepts that shape, and the record's online URL reaches `facts.jsonl`.

## Source text quality, fixed in the renderer

- About 700 procedures write lists in MediaWiki style (`#`, `##`, `*`, `**`, `#:`). Rendered verbatim, `## ...` lines became fake section headings (seen in 197055 and 391033), which would have broken any heading-based chunker. `clean_text` now turns them into Markdown bullets. After re-rendering, a corpus-wide check finds 0 lines starting with `#` other than each doc's title and the 13 known section headings.
- 312 stray `<br />` tags in 256 docs are now line breaks. No other HTML appears in the snapshot.
- 176 cost lines in 132 docs read `None έως X €` (minimum missing). They now read `έως X €`. Five procedures printed `None` for zero durations (`PT0S`), which are now left out.
- 71 fines in 2 procedures (242765 and 376453) have minimum > maximum, for example `500 έως 1 €`. The source appears to truncate thousands. The renderer leaves the amount out and keeps the description, which states the real range. `facts.jsonl` still stores the raw numbers. Any fact validator must treat `amount_min > amount_max` as unknown.

## Who the procedures are for

| `process.type` | Count |
|---|---|
| Εξωστρεφής (outward, to citizens and businesses) | 3,073 |
| Αμφιστρεφής (both) | 705 |
| Εσωστρεφής (internal to the administration) | 623 |
| missing | 5 |

Triggers: 3,422 Αιτούμενη (requested by the person), 515 Αυτεπάγγελτη (started by the administration), 187 Εσωτερική, plus smaller groups. 95 procedures have Εσωτερική as their only trigger. Εσωστρεφής or only-Εσωτερική together come to 667 procedures (15%).

All 26 distinct procedure IDs (gold or acceptable) across the 18 dev questions with labels are Εξωστρεφής with an Αιτούμενη trigger. A filter that drops the 667 internal procedures therefore loses no dev answer. But the dev set is citizen-only, so it cannot show harm either. That makes the filter an option, not a finding. Its likely gain is fewer staff-facing distractors in top-k. Its risk is civil servants asking about their own procedures (PRD user 2).

## Costs

3,194 procedures are free and 1,024 show a euro amount or range at a glance. The rest show "Κυμαινόμενο" (variable). Cost items with a null minimum and a maximum are "up to X". Those with both null are unknown, not zero. `facts.jsonl` keeps the nulls. The passport (144400) has six alternative fees, which is why the PRD example now gives the age and replacement variants.

## Document sizes

Tokens are counted with the bge-m3 tokenizer (XLM-R vocabulary). That is a measuring stick, not a chosen embedding model. Character counts do not depend on any tokenizer. Greek averages 3.24 characters per token with this vocabulary.

| | p50 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| KB | 14.2 | 30.4 | 39.4 | 56.0 | 157.5 |
| characters | 8,404 | 18,060 | 22,946 | 32,658 | 89,533 |
| tokens | 2,594 | 5,565 | 7,047 | 9,908 | 26,665 |

Docs over a token limit: 512: 100%, 1,024: 95.6%, 2,048: 66.2%, 4,096: 21.6%, 8,192: 2.7%. The passport is 9,098 tokens (p99).

Share of all tokens by section:

| Section | Share | p50 | p95 |
|---|---|---|---|
| Βήματα (steps) | 27.4% | 674 | 2,285 |
| Δικαιολογητικά (documents) | 23.1% | 565 | 2,820 |
| Νομοθεσία (legislation) | 13.7% | 306 | 1,157 |
| Προϋποθέσεις (conditions) | 9.8% | 198 | 1,078 |
| all others | 26.0% | | |

Steps and legislation make up 41% of the corpus. Much of that is written for staff (who stamps what, which law applies). Whether it helps or hurts retrieval is a hypothesis to test, not a reason to drop it.

## What this means (options, not a choice)

1. **Embedding model context.** With a 512-token model, every doc needs chunking. With an 8k model (bge-m3 accepts 8,192), 97% of docs fit whole. The embedding model choice therefore constrains granularity, and the other way round.
2. **Generation context and cost.** Five whole docs is about 13k tokens at p50 and about 35k when they are large (p95 per doc). At the checked Haiku 4.5 input price ($1.10 per million, eu-central-1), that is about $0.014 and $0.039 per question. Sonnet 5 doubles that. Section retrieval (for example 8 sections of about 600 tokens) is about 5k tokens, roughly $0.006 on Haiku. These counts use the bge-m3 tokenizer. Claude's count for Greek may differ: check with Bedrock CountTokens once the account is verified.

   **Correction (2026-10-22).** CountTokens measured Claude at about 1.5 characters per token for Greek (decision 006), against 3.24 for bge-m3, so every Claude count above is about 2.2 times too low. Five whole docs are about 28k tokens at p50 and about 76k at p95, roughly $0.031 and $0.083 per question on Haiku 4.5. The section option is about 11k tokens, roughly $0.012, which matches the measured prompts in 006 (about 11k tokens, $0.014). Sonnet 4.6 ($3.30 per million input, decision 007) costs three times the Haiku figures, not twice.
3. **Granularity is decided by the eval, not by these numbers.** Phase 2 runs BM25 at both granularities, whole doc and section with hits mapped back to the procedure. Procedure hit@5 and MRR on the dev set pick the one to build on. Whole-doc BM25 does not depend on the chunker. Section BM25 depends on the heading fix above.
4. **Internal-procedure filter.** Measure it in Phase 2 as a variant (with and without the 667). Adopt it only if dev metrics improve, and once the eval set has a few civil-servant questions.
