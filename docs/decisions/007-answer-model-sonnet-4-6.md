# 007: Sonnet 4.6 as the answer model, A/B against Haiku 4.5

Status: measured on dev, 2026-10-17. **The goal was a stronger model than Haiku 4.5**, and Sonnet 4.6 was chosen for this A/B. Not adopted yet: the default in `answer.py` stays Haiku 4.5 until a decision is made. The test split has not been run for answers.

## Which models this account can call

The quotas come from GetServiceQuota (ListServiceQuotas shows 0 for some of them), eu-central-1, cross-region (`eu.`) profiles. Prices are from the AWS Price List API (`AmazonBedrockFoundationModels`), eu-central-1, per million tokens. Both were checked on 2026-10-17.

| model | tokens/min | requests/min | input / output | usable today |
|---|---|---|---|---|
| Haiku 4.5 (006) | 5M | 10 | $1.10 / $5.50 | yes |
| Sonnet 4.6 | 6M | 10 | $3.30 / $16.50 | yes |
| Opus 4.6 | 3M | 5 | $5.50 / $27.50 | yes |
| Sonnet 5.5 | 0 | | $2.20 / $11.00 | no, needs a quota request |
| Opus 5.5 | 0 | | $4.40 / $22.00 | no, needs a quota request |

Every 5.x model, including Haiku 5.5, Sonnet 5 and Opus 5, has a 0 token quota. The last increase request took 3 days and granted 10 requests per minute instead of the 10,000 asked (CLAUDE.md). Sonnet 4.6 accepted `output_config.format` and `temperature` in this run, so the pipeline from 006 runs unchanged. Opus 4.6 supports both according to the docs, but it has not been tried.

## What changed in code

- `answer.py` has a `ModelSpec` table (`ANSWER_MODELS`) with the model id, the CountTokens id, prices and the request quota. `--model` picks one in `mitos-ask` and `mitos-answer-eval`, and Haiku 4.5 stays the default.
- The model id is part of the request, so it is part of the response cache key. Haiku's cached answers stay valid, and the two models never share an entry.
- The eval writes `data/eval/answers-<split>-<model>.json`, so runs no longer overwrite each other. The Haiku v2 results are still in `answers-dev.json`.

Same prompt, evidence, schema, temperature 0 and `max_tokens` as 006 v2. Thinking is off, as it is for Haiku.

## Results (dev, 20 items)

| metric | Haiku 4.5 (006 v2) | Sonnet 4.6 |
|---|---|---|
| status accuracy, honest (q15 counted wrong, see below) | 0.70 | 0.80 |
| status accuracy as scored | 0.70 | 0.85 |
| answered (15) / ambiguous (3) / not_found (2) | 0.67 / 0.67 / 1.00 | 0.87 / 0.67 / 1.00 |
| citation precision (strict), when retrieved | 0.82 | 0.81 |
| invalid citations | 0 | 0 |
| expected facts correct | 3 of 5 | 4 of 5 |
| unsupported claims flagged by the checker | 2 | 10 (9 of them are correct, see below) |
| mean tokens in / out | 10,984 / 327 | 10,985 / 500 |
| cost per question | $0.014 | $0.045 |
| generation latency p50 / max (non-streaming) | 4.0 s / 6.9 s | 7.2 s / 17.1 s |

Per item, against Haiku v2:

- **Fixed (2).** q18 ("passport from the internet") now answers "no, in person", with the fees and the online loss report. But its prose says passports are applied for at "Αστυνομία, ΚΕΠ κ.λπ.", and ΚΕΠ appears nowhere in 144400: an unchecked prose error, not caught because the checker reads only numbers and typed facts. q22 (newborn) now answers the name declaration and the birth allowance instead of asking "born in Greece or abroad", and adds one line for births abroad, as the prompt asks.
- **Fixed on paper, wrong in fact (1).** q15 ("ποσο κανει η ανανεωση του διπλωματος", driving licence renewal cost) is a retrieval miss: the licence procedure is not among the candidates (004). Haiku asked a question. Sonnet answered confidently from 232177, the validation of a **foreign officer's** diploma, with its 18 € and 60.50 € fees. The status scores as correct because `status_correct` compares only the status and the expected status is "answered", but the answer is wrong and confident. Counted honestly, Sonnet gets 16 of 20 (0.80) against Haiku's 14 of 20 (0.70). This is the most important finding here: a stronger model trusts weak evidence more.
- **Still over-asking (2).** q06 still offers refugee travel documents next to the Greek passport. q21 still asks which document, partly because the ID-card procedure was not retrieved.
- **Unchanged misses.** q24 (pension documents) is still a retrieval miss and still says not found.

The checker flags are mostly false alarms. Sonnet writes longer answers with more numbers taken from the procedure text, and the amount check only accepts amounts that are registry cost fields. Every new flag was checked by hand against `data/docs`:

| item | flagged | in the source text |
|---|---|---|
| q02 | 30 € | debts "άνω των τριάντα (30) ευρώ" (439993) |
| q11 | 30 €, 2 months | the 30 € print fee, "το νωρίτερο δύο μήνες πριν τη λήξη" (664541) |
| q18 | 13 months | the 13-month emergency passport (144400) |
| q22 | 2,000 €, 1,000 €, 4,000 €, 40,000 €, 3 months | "2.000 ευρώ ... δύο ισόποσες δόσεις των 1.000", "για δίδυμα 4.000€", "40.000,00 ευρώ", "εντός τριμήνου από τη γέννηση" (525354) |

All 9 are correct. q11's "ψηφιακά (και μέσω ΚΕΠ)" was also checked and is in 664541. The tenth flag, q04 "online: yes", is the same facts-row gap that Haiku hit in 006. As built, the warning would show on 5 of Sonnet's 17 answered or ambiguous dev answers, so users would learn to ignore it.

## Cost

Dry run with CountTokens against Sonnet 4.6: free, 208,163 input tokens for 20 prompts, quote $0.92 at 700 output tokens each. Actual: **$0.89** for 20 fresh calls. **Project spend is about $3.19.** The first Sonnet 4.6 call created a second AWS Marketplace subscription on the account, next to Haiku 4.5's. Running the full dev split costs about $0.89 on Sonnet 4.6 and $0.28 on Haiku 4.5.

## Open questions

- **Adopt Sonnet 4.6, or keep Haiku 4.5.** Sonnet fixes 2 over-asked items for real and gets one more fact right. It costs 3.2 times as much per question, and its p50 latency is 7.2 s against a 3 s first-token target, so streaming matters even more. With 20 items, a 2-item gain is a signal, not proof.
- **Weak evidence (q15).** With Sonnet, a retrieval miss can become a confident wrong answer. Options: a prompt rule ("answered only if a candidate is the procedure asked about, not a similar one for another group"), a code rule on the top candidate's score, or fixing the retrieval miss itself.
- **The checker scope.** Either accept amounts and durations that appear in a cited procedure's text (still grounded, weaker than a registry field), or tell the model to quote only registry costs. Without one of these, the warning fires on correct answers.
- **Scoring.** `status_correct` should require a relevant citation for answered items, so a q15-style answer stops counting as correct. Both runs can be re-scored from the cache for free.
- **Em dashes.** 4 of Sonnet's 20 answers contain "—" (Haiku: 0). CLAUDE.md bans them in user-facing text: if Sonnet is adopted, add a prompt line or replace them in `render`.
- **Opus 4.6** is also usable today, at about $0.07 per question and 5 requests per minute. Not run.
