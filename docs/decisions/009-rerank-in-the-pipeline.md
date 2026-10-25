# 009: The Haiku reranker in `mitos-ask`, with both answer models

Status: measured on dev, 2026-10-17. The reranker was wired in and both answer models were run. The reranker is on by default (`--rerank haiku`). **Chosen (2026-10-17): Sonnet 4.6 for answers, with the Haiku reranker**, and it is now the default. `render` replaces em dashes with plain dashes. The checker scope was decided in 010. **The answer test split was run once on 2026-10-22, after 010 was committed** (results below).

## What changed in code

- `mitos-ask` and `mitos-answer-eval` take `--rerank haiku|none`. With `haiku`, S3 Vectors pages until it has 30 distinct procedures (up to 300 chunks), Haiku 4.5 reranks them by title and alternative titles (`rerank.py`, 008), and the reranked top 5 go to the answer model. Evidence chunks still fill the budget by similarity, and candidate scores stay dense cosine.
- **Why 300 chunks:** the top 100 chunks span as few as 16 procedures on dev, because one procedure's chunks cluster. A local exact check showed 300 chunks cover the exact top 30 on every dev and test question. Paging stops early at 30 procedures, so most questions need one or two pages (S3 Vectors p50 0.14 s).
- **`--rerank none` is byte-identical to 006 and 007.** All 40 cached answers (20 per model) still hit the response cache, checked before any paid call.
- **One quota for Haiku.** Bedrock quotas are per model. With Haiku answering, reranking and answering share one `Pacer` at 10 requests per minute, so a demo with Haiku answers serves about 5 questions per minute. With Sonnet answering, each model has its own 10.
- The dry run stays free. It counts the rerank prompts with CountTokens, uses a cached rerank order when one exists and the dense top 5 as a proxy otherwise, and quotes rerank and answer costs separately.
- Results go to `answers-<split>-<model>-rerank-haiku.json`, next to the 006 and 007 files. The summary adds `rerank_cost_usd`, `total_cost_usd` and the rerank latency.

## Results (dev, 20 items)

| metric | Haiku (006 v2) | Sonnet (007) | Haiku + rerank | Sonnet + rerank |
|---|---|---|---|---|
| retrieval hit@5 (18 labelled) | 0.89 | 0.89 | **1.00** | **1.00** |
| status accuracy as scored | 0.70 | 0.85 | 0.85 | **1.00** |
| status accuracy, honest | 0.70 | 0.80 (q15) | 0.85 | 0.95 (q22) |
| answered (15) / ambiguous (3) / not_found (2) | 0.67 / 0.67 / 1.00 | 0.87 / 0.67 / 1.00 | 0.80 / 1.00 / 1.00 | 1.00 / 1.00 / 1.00 |
| citation precision (strict), all items | 0.77 | 0.76 | 0.82 | 0.82 |
| citation precision (strict), when retrieved | 0.82 | 0.81 | 0.82 | 0.82 |
| invalid citations | 0 | 0 | 0 | 0 |
| expected facts correct | 3 of 5 | 4 of 5 | 3 of 5 | **5 of 5** |
| unsupported claims flagged | 2 | 10 | 5 | 8 |
| answers with an em dash | 0 | 4 | 0 | 2 |
| cost per question, answer + rerank | $0.014 | $0.045 | $0.014 + $0.0035 | $0.045 + $0.0035 |
| answer latency p50, non-streaming | 4.0 s | 7.2 s | 4.9 s | 11.7 s |

Citation precision did not improve. The rise from 0.77 to 0.82 overall comes only from the two retrieval misses going away; on the items that were already retrieved it is flat (0.82 against 0.82 and 0.81).

Rerank latency, measured with 5 fresh uncached calls: p50 **1.7 s**, max 3.3 s. Sonnet's answer p50 went from 7.2 s to 11.7 s, with about the same prompt and only 14% more output (572 against 500 tokens). That looks like Bedrock latency on the day, not the pipeline, but it is one run.

Per item:

- **The reranker fixed both retrieval misses.** q15 (driving licence renewal cost) now has the licence procedure: Sonnet answers it correctly from 664541, so 007's confident wrong answer (a foreign officer's diploma) is gone. q24 (pension documents) moves from not found to the expected clarifying question in both models.
- **Over-asking fixed by better candidates.** q06 (passport documents) and q21 (lost ID card and passport) are answered by both models, citing every expected procedure. The refugee travel documents no longer reach the top 5.
- **Haiku still over-asks.** q15 asks "Greek renewal or exchange of an EU licence", and q16 (days for a criminal record) asks "criminal record or a certificate of non-prosecution". q16 was answered correctly in 006, so this is a regression, from a different set of candidates. q22 (newborn) asks too.
- **q22 is still unsolved, and the scorer hides it.** Sonnet answers with the birth record 106576, which is acceptable, so the status counts as correct. It also cites the birth certificate and two procedures for births abroad, and misses both expected procedures: the name declaration 955846 and the AMKA for minors 137618. Counted honestly, Sonnet gets 19 of 20. Multi-procedure questions need their own step (008).
- **007's prose error is gone.** q18 no longer says passports are applied for at ΚΕΠ. Prose is still unchecked, so this is luck, not a guarantee.

## The checker flags

Each of the 13 flags was checked by hand against `data/docs`:

| item | flagged | what the source says | right? |
|---|---|---|---|
| q02 (both) | 30 € | debts "άνω των τριάντα (30) ευρώ" (439993) | yes |
| q04 (both) | online: yes | the facts row has `can_apply_online` null; the text describes a gov.gr application | not checkable, the warning is right to show |
| q06, q18 (Sonnet) | 13 months | the 13-month emergency passport (144400) | yes |
| q11 (Sonnet) | 30 € (typed and in text), 2 months | the 30 € print fee, "δύο μήνες πριν τη λήξη" (664541) | yes |
| q15 (Sonnet) | 30 € | the same 30 € print fee (664541) | yes |
| q08 (Haiku) | 19,5 €, 14,5 €, 3 days | "Κόστος: Από 14,5 έως 19,5 €", "3 ημέρες έως 7 ημέρες" (880224) | numbers yes, but "Σύνολο: περίπου 19,5 €" is a total, which the prompt forbids |

So 11 of 13 are correct numbers from the procedure text, 1 (q04) is a facts-row gap, and 1 (Haiku q08) breaks the no-totals rule with a number that happens to be in the registry. A free simulation on the cached results: if an amount or duration that appears in a cited procedure's text counted as grounded, 8 of the 10 amount and duration flags across the two reranked runs would clear. The other 2 are parser gaps: "δύο μήνες" written as a word, and a "14,5 έως 19,5 €" range. As built, the warning shows on correct answers, and users would learn to ignore it.

## Cost

- Dry runs with CountTokens: free. The quote was $0.31 for Haiku answers, $0.92 for Sonnet answers and $0.08 for 19 fresh rerank calls, so $1.30 in total.
- Actual: Haiku run $0.294 (answers $0.227, rerank $0.067), Sonnet run $0.748 (answers only, every rerank came from the cache), and the 5-call latency check $0.018. The latency check was not in the approved plan; it stayed inside the approved $1.33. **$1.06 in total. Project spend is about $4.35.**
- Four answers per model were free: their reranked top 5 matched the dense top 5, so the 006 and 007 cache entries hit.
- In use, a question costs about $0.018 with Haiku answering and about $0.049 with Sonnet. 1,000 questions a month would be about $18 or $49.

## Recommendation

**Sonnet 4.6 for answers, with the Haiku reranker.** It is the only arm that gets every status and every expected fact right on dev, and the reranker removed the weak-evidence risk found in 007. It also keeps the two calls on separate quotas (10 questions per minute, not 5). The costs: 2.7 times the price per question, and 13 to 14 s end to end non-streaming, so Phase 5 streaming is required, not optional. Em dashes need a fix in `render` or the prompt. Twenty Claude-written questions are a signal, not proof: the test split is the check.

**The budget limits Sonnet.** The account budget is $40 a month (`terraform/account`). If nothing else spent, that pays for about 800 Sonnet questions a month, or about 2,200 with Haiku. So the public launch or demo-only choice (PRD 13.1) depends on the model, and a public Sonnet deployment needs a daily question cap in the Phase 5 API.

Haiku 4.5 is the cheaper option at $0.018 per question and 6.6 s. It costs 3 statuses and 2 facts on dev, and it still over-asks.

**Checker scope:** keep the registry fields as the strong check, and add a second tier that counts an amount or duration as grounded when it appears in a cited procedure's text. Show the warning only for numbers in neither. Separately, a total in the text (Haiku q08) should stay flagged: the prompt rule exists because totals cannot be checked.

## Test split (held out, 10 items, run once on 2026-10-22)

Sonnet 4.6 + Haiku rerank, scored with the 010 checker. Nothing was tuned on these results, and nothing will be.

| metric | dev (re-scored, 010) | test |
|---|---|---|
| status accuracy | 1.00 | **0.80** |
| answered / ambiguous / not_found | 1.00 / 1.00 / 1.00 | 0.75 (6 of 8) / 1.00 / 1.00 |
| retrieval hit@5 | 1.00 | 0.89 (8 of 9 labelled) |
| citation precision (strict), when retrieved | 0.82 | 0.88 |
| invalid citations | 0 | 0 |
| expected facts correct | 5 of 5 | 2 of 2 |
| every expected procedure cited (multi-procedure answered items) | 1 of 2 | 0 of 1 |
| unsupported claims / answers with the warning | 3 / 2 | **0 / 0** |
| text-grounded numbers | 5 | 15 (14 in q13) |
| cost per question, answer + rerank | n/a (cached) | $0.044 + $0.0036 |
| answer latency p50 / max, non-streaming | n/a (cached) | 8.1 s / 15.9 s |

The two misses:

- **q03 "ΕΝΦΙΑ"** (expected answered, from 314585): Sonnet asks which ENFIA procedure is meant and offers four, the expected one first. For a one-word question that is defensible, but it is scored as wrong against the label, and the label stays.
- **q23 "πεθανε ο πατερας μου τι χαρτια χρειαζομαι"** (expected 406191 and 527543): a retrieval miss. Neither expected procedure reached the five the model saw. Sonnet asks a clarifying question over four inheritance and pension procedures, all related but none expected. It is the multi-procedure, everyday-wording case (q22 on dev) that no step solves yet.

The checker on test: q13 (child benefit) lists the three income brackets and their amounts, 14 numbers, all in procedure 375051's text (lines 41 to 53), none in its facts row. The strict checker would have warned on one of the best answers in the run. The 15th is the 5-year residence rule in the same answer.

Cost: quoted $0.613 by the dry run, actual **$0.481** (answers $0.444, rerank $0.036). The quote used the dense top 5 for every prompt, which pulled the fines procedure 242765 into q03 (45,113 tokens). After reranking, q03's prompt was 10,828 tokens. **Project spend is about $4.83.**

Dev scored 1.00 and test 0.80 on status. Twenty Claude-written dev questions were partly tuned against; ten held-out ones are a small sample. Both point the same way: single-procedure questions work, and multi-procedure everyday questions are the weak spot.

## Open questions

- **Answer test split: done (2026-10-22), see above.**
- **Answer model: decided, Sonnet 4.6.** The dry-run quote was $0.61 because the dense proxy pulled the fines procedure 242765 into q03; the cap on facts rows belongs to Phase 5, designed from the 001 profile.
- **Checker scope: decided, text-grounded second tier (010).**
- **Scoring: done in 010** (`all_expected_cited`, answered items only).
- **Multi-procedure questions** (q22): a later step, for example splitting the question into its parts before retrieval.
