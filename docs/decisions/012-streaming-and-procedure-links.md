# 012: Streaming answers, a free warm-up, and links to the procedure page

Status: adopted, 2026-10-22 (streaming and the warm-up). The PRD 9 latency target change is proposed, not adopted: it rests on one measured question. Paid calls: 2 questions to measure streaming, about $0.09 (one answered in full, one stopped mid-answer). Project spend is about $5.28.

## The problem

Two real questions through the browser on 2026-10-22 took 22.2 s and 16.0 s, and the screen showed only a spinner until the end. The new per-stage log line (below) showed where the time goes:

| question | embed | retrieve | rerank | Sonnet answer | output tokens | total |
|---|---|---|---|---|---|---|
| short, one procedure | 1.30 s | 0.51 s | 2.39 s | 6.91 s | 475 | 11.1 s |
| two-part (driver training, documents), streamed | 0.18 s | 0.60 s | 2.85 s | 15.19 s | 822 | 18.8 s |

Sonnet 4.6 writes about 55 to 70 output tokens a second, so the answer's length sets the wait. Greek costs about twice the tokens of English, and two-part questions get long answers. The search and rerank before it take about 3.5 to 4 s.

Separately, every source link failed: `id.mitos.gov.gr` returned 502 for every procedure again (it did on the morning of 2026-10-22 too).

## Streaming: `POST /ask/stream`

- Same question, same limits, same lock as `/ask`. A refusal (length, rate, daily, busy) is still a plain JSON error with its status code. `/ask` is unchanged.
- The answer is newline-delimited JSON: `found` (the top 3 candidate titles, once rerank is done), `text` (pieces of `answer_text` as Claude writes them), then `done` with every `/ask` field, or `error` with a Greek message.
- `answer_text` is read out of the structured output while it is being written (`AnswerTextStream`). `status` is first in the schema and `answer_text` second, so text appears a few tokens after the first token. Checked at every split point on all 103 cached answers, with raw Greek and with `\uXXXX` escapes.
- Streamed text is provisional. The checker, the warning, the sources and a possible status change only exist once the JSON is complete, so `done` rebuilds the whole card. A wrong amount can be on screen for a few seconds before its warning appears.
- The request is byte-identical to the non-streaming one, so cache keys do not change. Dev re-scored from cache: 0 fresh calls, results identical to the 009/010 file apart from timings.
- A user who leaves while the answer streams: `stop` is checked every 0.5 s (`STOP_CHECK_S`) and the stream is closed. Measured once: the client left at 7.8 s, the server stopped at 8.3 s. Whether Bedrock stops generating, and billing, when the connection closes is not verified.

## Measured (n = 1, client side, the two-part question above)

| event | time |
|---|---|
| `found` titles on screen | 3.3 s |
| first answer text | 4.7 s |
| Sonnet's first token after its call started | 1.09 s (server side) |
| whole answer | about 18.8 s, unchanged |

Total time is the same. The wait before anything useful appears drops from the whole answer (16 to 22 s) to about 5 s.

**Proposed PRD 9 change, not yet decided:** "first answer text under 6 s at p95, warm" instead of "first token under 3 s at p95". The 3 s target is out of reach with the Haiku reranker (decided 2026-10-22 to keep it). A p95 needs more than one sample: the per-stage log line collects them for free from real use.

## Warm-up at startup

`warm_up` makes one free CountTokens call through the boto Bedrock client before the server accepts questions. It loads the AWS credentials (an SSO token refresh took about 1 s) and opens a connection. Measured at startup: 1.03 s. The Anthropic SDK's Bedrock client has no `count_tokens` ("not supported in Bedrock yet"), so it is not warmed, but it reads the same refreshed SSO token. The first question after the warm-up embedded in 0.18 s against 1.2 to 1.3 s before, but that is one sample taken 11 s after start: pooled connections can close during an idle gap. The log line shows the embed time on every question, so the real gain will show there. A failed warm-up only logs.

## Per-stage log line

`ask status=... cost_usd=... total_s=... embed_s=... retrieve_s=... rerank_s=... generate_s=... first_token_s=... input_tokens=... output_tokens=... warning=...`. Still no question text.

## Links to the procedure page

- The main link is now the procedure's page on the Mitos site, `https://mitos.gov.gr/index.php/ΔΔ:<title with spaces as _>` (`page_url` in the API, built from the facts title). `id.mitos.gov.gr` stays as a small "Μόνιμος σύνδεσμος" second link, because it is the permanent id and a title can be renamed.
- All 4,406 facts titles equal the record's wiki title (`title.el`, namespace ΔΔ). 29 hand-picked pages, including titles with `/`, `&`, `%` and `+`, returned 200, and a made-up title returns 404. A polite check of all 4,406 pages (one a second) was running when this was written.
- The model prompt and `source_url` are unchanged, so no cached answer changes.

## Not done

- No real streamed answer had been watched in the browser when this was written: the UI was checked with Playwright against a fake streaming assistant, and the real stream with a client script.
- A half-written `**bold**` shows its asterisks until the closing marks arrive.
- Shorter answers (fewer output tokens) would cut the total time but change the prompt, so they need a paid dev re-run and a decision.
