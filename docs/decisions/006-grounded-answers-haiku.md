# 006: Grounded answers with Claude Haiku 4.5

Status: built and measured on dev, 2026-10-17. **Chosen: Haiku 4.5** over Haiku 5.5 on 2026-10-17. The test split has not been run for answers.

## Model choice

Prices are from the AWS Price List API for eu-central-1, per million tokens, regional `eu.` profile, checked on 2026-10-17.

| | Haiku 4.5 | Haiku 5.5 |
|---|---|---|
| Price (input / output) | $1.10 / $5.50 | $0.11 / $0.55 |
| Account quota in eu-central-1 | 5M tokens/min, 10 requests/min | 0 (needs an increase request) |
| Structured output (`output_config.format`) on Bedrock | yes (legacy InvokeModel integration) | no (the Messages-API Bedrock endpoint does not list it) |
| Marketplace subscription | active | a new one is created on the first call |
| Greek tokenization | 1.5 characters per token (CountTokens, 4 documents) | Bedrock CountTokens does not support it |

Haiku 4.5 works today with structured output. Haiku 5.5 is 10 times cheaper per token, but it is blocked on a quota request, and without structured output it would need a tool-use design. Re-evaluate it on this same eval once its quota exists.

## Pipeline (`answer.py`, `validate.py`)

1. **Embed the question** with Cohere `search_query`. The query cache makes repeat questions free.
2. **Find candidates.** S3 Vectors returns the top 100 chunks, and the 5 procedures with the best chunks become the candidates (005).
3. **Assemble the evidence** within 12,000 characters:
   - the candidates' `facts.jsonl` rows (min > max amounts become null),
   - each candidate's best chunk, then more chunks by similarity.

   Whole documents do not fit: at 1.5 characters per token, the passport document alone is 20,355 tokens.
4. **Generate** with Haiku 4.5 through the Anthropic SDK's `AnthropicBedrock` client:
   - JSON schema output, temperature 0 (via `extra_body`; SDK 1.x dropped sampling arguments), `max_tokens` 2,000.
   - The schema extends PRD 6.5. Each item in `facts_used` is typed (cost, duration, validity, online, other) and carries its procedure id.
   - `clarifying_question` and `options` cover the PRD 6.6 ambiguous case.
5. **Validate in code.** The model's answer is checked, not trusted:
   - cited ids must be among the candidates,
   - typed facts must match their facts row,
   - every euro amount in the text, including both ends of a range, must be a cost of a cited procedure,
   - every numeral duration in the text ("5 ημέρες", "2-3 ημέρες", "10 χρόνια") must be a deadline, estimated duration or validity of a cited procedure. Ages ("14 ετών") and durations written as words are not checked,
   - "free" counts only when `is_free` is true,
   - links written by the model are removed.

   A failure adds a Greek warning to the answer.
6. **Render.** Code adds the source links, the `last_updated` dates, the attribution and the not-legal-advice line.

Responses are cached on disk under a hash of the full request, so re-scoring is free. `mitos-answer-eval --dry-run` counts every prompt with CountTokens (free) and quotes the cost before a run.

## Results (dev, 20 items)

Two runs. v1 is the first prompt. v2 adds three rules:
- assume the ordinary citizen's case and only mention special-group procedures,
- answer all procedures when several apply,
- never add amounts together.

The checker was also fixed between runs, and v1 was re-scored with the fixed checker for free. After v2, the text duration check and citation precision were added, and v2 was re-scored from the cache. v1 has no precision figure.

| metric | v1 | v2 |
|---|---|---|
| status accuracy | 0.70 | 0.70 |
| answered (15) / ambiguous (3) / not_found (2) | 0.67 / 0.67 / 1.00 | 0.67 / 0.67 / 1.00 |
| retrieval hit@5 (gold among the candidates) | 0.89 | 0.89 |
| citation relevant (lenient: at least one cited id is right), when retrieved | 1.00 | 1.00 |
| citation precision (strict: share of cited ids that are right), when retrieved | not scored | 0.82 |
| invalid citations (ids not retrieved) | 0 | 0 |
| expected facts correct | 4 of 5 | 3 of 5 |
| unsupported claims (costs, durations, validity, euro amounts and durations in the text) | 4 | 2 |
| cost per question | $0.0137 | $0.0139 |
| mean tokens in / out | 10,847 / 314 | 10,984 / 327 |
| generation latency p50 / max (non-streaming, full answer) | 5.0 s / 7.6 s | 4.0 s / 6.9 s |

The first answer was q14 ("ποσο κοστιζει διαβατηριο για παιδι 10 χρονων"): **71 €**, the under-14 fee and not the adult 80 €, with the case explained and validation passing.

## Findings

1. **Not found works.** The weather question and the accountant question both return "Δεν βρήκα αυτή την πληροφορία στο Μητρώο Μίτος". Decision 004 found that a cosine threshold could not separate these on test, so the LLM step was needed.
2. **The citations are valid, but some of them are off target.** The model never cited a procedure it was not given (0 invalid). When retrieval found the right procedure, at least one cited procedure was always relevant. The strict share of cited procedures that are expected or acceptable is 0.82, though, and almost all the misses are clarification options. q06 offers refugee travel documents for "διαβατήριο", and q25 offers two ID procedures that are not in its labels; those labels may be incomplete. PRD 8.2 defines citation correctness as "the cited page supports the answer", and only a human read can fully confirm that.
3. **The checker caught wrong numbers.** v1 summed fees, for example "Σύνολο: 10,5 €" (10 + 0.5). The sums were correct, but no registry field states them, so they cannot be checked. The v2 rule removed them in q20, but q08 still writes "19,50 €" (10 + 9 + 0.5) and gets the warning. No unsupported amount reached a user without a warning.
4. **The model asks for clarification too often.** This is the main generation problem:
   - q06 "τι χαρτια θελω για διαβατηριο" offers the Greek passport next to refugee travel documents, in both runs.
   - q22 (newborn) asks "born in Greece or abroad".
   - q21 (lost ID card and passport) asks which document. The ID-card procedure 880224 was not among the 5 candidates, so this item is also a retrieval limit for multi-procedure questions.
   - v2 fixed q07 (unemployment card) but broke q18 ("μπορω να βγαλω διαβατηριο απο το ιντερνετ"). That question is now ambiguous between loss reporting and a new issue, where v1 answered "no, in person" correctly.

   With 20 items and swings in both directions, further prompt edits would fit noise.
5. **The retrieval misses stay misses.** q15 (licence renewal cost) and q24 (pension documents) never put the right procedure among the candidates (004). The model then says not found or asks. It does not invent an answer.
6. **The facts can be incomplete.** q04 says the procedure is online. The text supports that, but its facts row has no submission types (`can_apply_online` is null), so the claim cannot be verified and the answer carries the warning. That is the intended behaviour for unverifiable claims.

## Against the PRD 8.3 MVP targets (dev only)

| target | dev result |
|---|---|
| procedure hit@5 ≥ 0.85 | met: 0.89 (the right procedure among the 5 candidates) |
| citation correctness 100% on displayed answers | not met as measured: 0 invalid citations, strict precision 0.82. Not human-checked |
| correct not_found on all out-of-scope questions | 2 of 2 on dev. q30, the test item that beat the cosine threshold in 004, has not been through the LLM |
| no wrong cost or deadline | every unsupported cost or duration was flagged with a warning (2 on dev, both fee totals). The test split has not been run |

## Cost

- smoke test: $0.0134
- v1: $0.26 for 19 fresh calls
- v2: $0.28 for 20 calls

**Total for this decision: $0.55. Project spend so far is about $2.30.**

## Open questions

- **The clarification threshold.** Options:
  - a code rule, for example "ambiguous only if the top candidates are close in score and none is the general procedure",
  - a reranking step before generation,
  - accept it: ambiguous answers still list the right procedure as an option.
- **Multi-procedure questions** need more than 5 candidates, or one retrieval per sub-question.
- **Latency.** The PRD target is first token under 3 s at p95. Non-streaming answers take 4 to 7 s, so Phase 5 needs streaming, and structured output streams as JSON.
- **The held-out test split for answers** has not been run. Run it once, after the next pipeline change.
- **Noise.** The q07 fix and the q18 regression may partly be run-to-run variance, even at temperature 0. Any option above needs a paid dev run, about $0.28 each.
- **Privacy (PRD 9) for Phase 5.** The CLI writes every new question's embedding to the query cache, and every answer to the response cache. The API must turn both off or make them short-lived.
