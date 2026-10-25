# PRD: Mitos RAG, an assistant for Greek public procedures

Owner: Pantelis Tsagkas
Status: Draft v2 (2026-10-12), the original plan. Phases 0 to 5 are done and Phase 6 is deployed as a demo (2026-10-25); the README has the current state and results, and `docs/decisions/` the measured choices.
Audience: the owner and Claude Code, who build this together

Changes in v2: structured facts next to RAG, two-stage retrieval, chunk metadata, answer validation, "not found" vs "ambiguous", atomic data refresh, stratified eval with a held-out test set, embedding runtime cost, privacy and cost controls, and a "simple first" build rule.

---

## 0. The one rule for building this

**Start simple. Add a stage only when the evaluation shows it is needed.**

Section 6 describes the target design. Do not build all of it on day one. Each extra stage (reranker, Greeklish, structured validation of a new field, etc.) must be justified by a measured improvement on the eval set (section 8), and the before/after numbers go in `docs/decisions/`.

## 1. Problem

Greek citizens lose time and money on bureaucracy because it is hard to find out, in plain words:

- which procedure applies to their situation,
- which documents they need,
- how much it costs and how long it takes,
- whether they can do it online (gov.gr) or must go in person (ΚΕΠ, ΔΟΥ, police, etc.).

The official answers already exist in the National Registry of Administrative Procedures "Μίτος" (mitos.gov.gr), about 4,400 published procedures. But Mitos is a registry, not a conversation: people must already know the official name of the procedure to find it.

## 2. Goal

A Greek-language assistant where a person asks a question in their own words and gets a short, correct answer, built only from Mitos data, with a link to the official page for every answer.

Example:

> Q: τι χρειαζομαι για διαβατηριο
> A: Για την έκδοση διαβατηρίου χρειάζεσαι δελτίο ταυτότητας, ... Κόστος: e-Παράβολο 80 € (71 € για αιτούντες έως 14 ετών, 58 € για αντικατάσταση λόγω φθοράς ή μεταβολής στοιχείων). Πρέπει να πας αυτοπροσώπως στο Γραφείο Διαβατηρίων.
> Πηγή: Έκδοση Ελληνικού Διαβατηρίου, https://id.mitos.gov.gr/144400

### Secondary goal (portfolio)

This is a flagship Cloud / MLOps portfolio project. The story is not "a RAG chatbot" but **an evaluated, source-grounded public-information system**: it handles imperfect Greek, every fact is traceable to an official source, data refreshes safely, and quality, latency and cost are measured.

## 3. Users

1. **Citizen** (primary): any adult in Greece, non-technical, often on a phone. Asks in Greek, often without accents or with typos ("διαβατηριο", "ταυτοτητα").
2. **Professional helper** (secondary): accountants, lawyers, ΚΕΠ staff who want a fast lookup.

## 4. Scope

### In scope (MVP)

- Answer questions about procedures in the Mitos registry: documents, cost, steps, duration, where to apply, who it is for, legal basis.
- Greek questions and Greek answers.
- Every answer cites one or more Mitos procedures with their official link and last-updated date.
- Two different "can't answer" behaviours (section 6.6): not found, and ambiguous (ask the user a clarifying question).
- Daily incremental, atomic refresh of the data.

### Out of scope (MVP)

- Submitting applications or logging in to gov.gr or TAXISnet on the user's behalf.
- Personal advice (tax, legal). The app explains procedures; it does not decide someone's case.
- Collecting personal data (ΑΦΜ, ΑΜΚΑ, names). The UI warns users not to type them.
- English answers (later; Mitos has partial English titles).
- Greeklish (later, as a separate measured experiment; see 6.2).
- User accounts.

## 5. Data source

### 5.1 Mitos API (verified 2026-10-12)

- Base URL: `https://api.digigov.grnet.gr` (an older client also refers to `https://api.mitos.gov.gr`)
- No API key needed for reading.
- `GET /v1/services?page=N&limit=M`: paginated list. Response has `total` (4,406 on 2026-10-12), `current_page`, `next_page`, `data[]` with `id`, `title.el`, `title.en` (optional), `ns`, `last_updated`.
- `GET /v1/services/{id}`: full procedure. Useful parts of `data.metadata`:
  - `process`: `official_title`, `alternative_titles`, `description`, `remarks`, `deadline_duration` (ISO 8601, e.g. `P10D`), `validity_duration`, `provided_to`, `life_events`, `provision_org_directory`, `evidence_identification_type`, `application_description`, `application_note`, `delivery_type`
  - `process_conditions[]`, `process_evidences[]` (documents), `process_evidences_cost[]` (with numeric `evidence_cost_min` / `evidence_cost_max`), `process_steps[]`, `process_steps_digital[]`, `process_rules[]` (laws, ΦΕΚ), `process_useful_links[]`, `process_provision_digital_locations[]`
  - `glance`: ready-made short facts (number of documents, cost, estimated online time)
- Other endpoints seen in the community client `pyemdd` (not yet used): `/v1/services-extended/{id}`, `/v1/organization?id=`, `/v1/registries/evidence/{id}`, `?lang=en`.
- No formal documentation or published rate limits found. Be polite: sequential requests, a delay between calls, retries with backoff, incremental refresh.
- **Not every procedure has every field.** Missing fields must be represented as "unknown", never guessed.

### 5.2 Licence

Mitos content is published under **CC BY-SA 4.0**:

- **Attribution:** every answer and the UI footer credit Mitos with a link.
- **ShareAlike:** a published processed dataset or derived database must be CC BY-SA 4.0 too. Review this before publishing any dataset.
- The application code is under the MIT licence (`LICENSE`), separate from the data licence.

### 5.3 Provenance kept for every procedure

`procedure_id`, `source_url`, `last_updated` (from Mitos), `ingested_at` (ours), `content_hash` (of the raw JSON), and the raw JSON itself.

### 5.4 Already built (Phase 0, done)

This repository already contains, with 25 passing tests:

- `src/mitos_rag/client.py`: typed API client with pagination, retries, exponential backoff.
- `src/mitos_rag/download.py`: `uv run mitos-download` saves `data/raw/index.jsonl` and `data/raw/services/<id>.json`; incremental by `last_updated`; `--max-services N` for a sample.
- `src/mitos_rag/render.py`: `uv run mitos-render` turns each procedure into one clean Greek Markdown document with fixed `##` sections, the official URL and the licence line.

**First thing to do in the next session:** run the downloader on a real machine (the build sandbox could not reach the API), open 5 to 10 random documents and read them like a citizen would, and fix the renderer if real data differs from the test fixtures. Check especially: costs, documents, durations, digital steps and dates.

## 6. Target design

```
INGESTION (scheduled, separate from the API)
Mitos API -> raw snapshot (S3) -> validate + normalise -> structured facts table
                                                       -> Markdown -> chunks + embeddings
                                                       -> new dataset version -> activate only if all checks pass

QUESTION ANSWERING (API)
question
  -> 1. normalise Greek (accents, final sigma, whitespace)
  -> 2. find candidate PROCEDURES (BM25 + vectors + RRF, deduplicated by procedure)
  -> 3. [optional, only if eval says so] rerank
  -> 4. decide: one clear procedure / several plausible (ambiguous) / none (not found)
  -> 5. fetch the relevant SECTIONS + STRUCTURED FACTS of the chosen procedure(s)
  -> 6. LLM writes the answer from that evidence, as structured output
  -> 7. validate (citations real, numbers match the facts) -> answer + links + dates
```

### 6.1 Structured facts next to RAG

The LLM must not be the only thing that reads costs and deadlines. Mitos already gives many facts as clean fields, so we store them in a table and use them directly:

| Fact | Source field |
|---|---|
| Costs (amount, type, payment methods, code) | `process_evidences_cost[]` |
| Processing deadline | `process.deadline_duration` |
| Validity | `process.validity_duration` |
| Online or not, online links | `process_steps_digital[]`, `process_provision_digital_locations[]` |
| Required documents | `process_evidences[]` |
| Who it is for | `process.provided_to` |
| Legal basis | `process_rules[]` |

- RAG covers the explanations, step details, notes and everyday wording.
- The LLM turns verified facts plus retrieved passages into a short Greek answer.
- Facts the source does not provide are stored as missing, and the answer says "Η πηγή δεν αναφέρει ..." rather than guessing.

### 6.2 Greek text handling

- Normalise for keyword search: lowercase, strip accents, final sigma ς to σ.
- Measure retrieval with and without normalisation on the eval set: the goal is better retrieval of the right procedure without new false matches, not just identical strings.
- If PostgreSQL full-text search is used, test its Greek behaviour explicitly; do not assume default configurations suit Greek.
- Greeklish ("diavatirio") is a later, separate experiment. Do not transliterate every query blindly.

### 6.3 Chunking

- One chunk per `##` section of a procedure (Δικαιολογητικά, Κόστος, Βήματα, ...); very long sections split with overlap.
- Every chunk starts with the procedure title and alternative titles.
- Chunk metadata:

| Field | Purpose |
|---|---|
| `procedure_id` | link to the procedure |
| `procedure_title` | show in results |
| `section_type` | documents, cost, steps, conditions, ... |
| `chunk_index` | order within the procedure |
| `source_url` | citations |
| `last_updated` | freshness |
| `content_hash` | re-embed only changed chunks |

- The full procedure is stored separately, so neighbouring sections can be fetched when needed.

### 6.4 Retrieval: procedures first, then sections

- Stage 1 finds **which procedure** the user means: dense vectors (Cohere Embed Multilingual v3), then **deduplicated by `procedure_id`**. Five chunks from one procedure count as one result. BM25 with RRF was planned here; it was measured and dropped because it lowered hit@5 (decision 004).
- Stage 2 picks **which sections** of the chosen procedure(s) answer the question, plus their structured facts.
- Reranker: not in v1. Add only if it measurably improves the eval enough to justify its latency and cost.
- Vector search: approximate, in Amazon S3 Vectors, top 100 chunks per question (decision 005). A managed store was chosen over exact search. On dev it matches exact search except MRR 0.70 to 0.69, and `mitos-eval --vector-index` keeps comparing the two.

### 6.5 Answer generation and validation

The LLM returns a structured object (internal, not shown to users):

```json
{
  "answer_text": "...",
  "facts_used": [{"fact": "Κόστος 80 € (άνω των 14 ετών)", "procedure_id": "144400", "section": "cost"}],
  "procedure_ids": ["144400"],
  "missing_facts": ["χρόνος έκδοσης"],
  "status": "answered | ambiguous | not_found"
}
```

Then code (not the prompt) checks:

- every cited `procedure_id` was actually retrieved; links are built by our code from the id, never written by the model,
- every cost and duration in the answer matches the structured facts table,
- if a check fails: remove the unsupported claim and show a warning, or fall back to "not found".

Prompt rules still apply: answer only from the evidence, simple Greek, bullet lists for documents and steps, always end with sources and their `last_updated` date, one-line note that this is information and not legal advice.

### 6.6 Two kinds of "can't answer"

- **Not found:** nothing relevant in Mitos. Say so clearly: "Δεν βρήκα αυτή την πληροφορία στο Μητρώο Μίτος", and suggest the closest procedures if any.
- **Ambiguous:** several procedures fit and we can't tell which applies (e.g. first passport vs renewal vs lost passport). Ask one short clarifying question with the options as buttons.

## 7. Decisions (the original options; outcomes noted per section)

Decide each with eval evidence, not reputation. Write each decision and its numbers in `docs/decisions/NNN-title.md`.

### 7.1 Embeddings

**Decided 2026-10-14: Cohere Embed Multilingual v3 on Bedrock, dense only** (`docs/decisions/004-dense-retrieval-cohere.md`). Titan V2 was deferred; the open-weight options were not run, because a query embedding at runtime needs a hosted model.

| Option | For | Watch out |
|---|---|---|
| **BGE-M3** (open source) | Strong multilingual candidate, free to run locally, good model-evaluation story | Large model. At runtime **every user question must be embedded too**, so production needs a server or endpoint running all the time (not a good fit for Lambda). Include that cost in the decision. |
| **multilingual-e5-large** (open weights) | Independent second baseline | Same runtime-hosting question; use its required query/passage prefixes. |
| **Amazon Titan Text Embeddings V2** (Bedrock) | Managed, pay per request, no server; AWS lists Modern Greek as supported | AWS says it is optimised for English; Greek quality is an empirical question. |

- Compare on the same corpus snapshot, the same chunking and the same eval questions.
- Judge on **quality AND production cost/latency**, not quality alone.
- Changing model means re-embedding the whole corpus; never mix vectors from different models in one index.

### 7.2 Vector store

**Decided 2026-10-17: Amazon S3 Vectors** (`docs/decisions/005-vector-store-s3-vectors.md`). The table below is the original option list; "leading choice" for Postgres predates decision 004, which dropped BM25 at runtime.

| Option | For | Watch out |
|---|---|---|
| **PostgreSQL + pgvector** (leading choice) | Procedures, facts, chunks, vectors and full-text search in one place; easy local Docker; whole pipeline in one place to evaluate | Managed Postgres has a monthly baseline cost even with zero users; Lambda can open too many connections (needs pooling). |
| **Amazon S3 Vectors** | Serverless, AWS-native, potentially cheap | Keyword search and RRF must live elsewhere. |
| **Qdrant** | Dedicated vector DB, good local dev | One more service to run; keyword search and RRF still to design. |

### 7.3 LLM, compute, frontend

**Decided:** Claude Sonnet 4.6 answers with a Claude Haiku 4.5 reranker, in eu-central-1 (009); Lambda with a Function URL behind CloudFront, not API Gateway or Fargate (013); a static Greek UI on S3 + CloudFront (`web/`).

- **LLM:** a Greek-capable model (e.g. a Claude model on Bedrock in an EU region), chosen by answer quality, latency and cost on the eval set.
- **API:** FastAPI locally. In AWS, Lambda + API Gateway if measured latency and DB connections allow; otherwise ECS Fargate.
- **Frontend:** static site on S3 + CloudFront (mobile-first, Greek). A mockup exists in the "Mitos Assistant Mockup" design artifact.

**Check current AWS pricing before every infrastructure choice.** The < 20 USD/month target (section 9) is a goal to validate, not an assumption.

## 8. Evaluation (exists before any tuning)

### 8.1 Dataset

- `eval/dev.jsonl`: for building and tuning.
- `eval/test.jsonl`: **held out**, used only to report results, never to tune. Prevents overfitting to our own questions.
- Start with ~50 questions total, grow to 150 to 300 over time.
- Each item: question, category, expected `procedure_id`(s), expected key facts (e.g. cost 80), expected `status` (answered / ambiguous / not_found).
- Categories (cover all of them):
  1. Exact lookup: official names, abbreviations, alternative titles
  2. Natural Greek: everyday wording, no accents, typos, colloquial
  3. Fact extraction: documents, costs, deadlines, steps, authorities
  4. Multi-procedure: genuinely needs more than one procedure
  5. Ambiguous: not enough detail to pick one procedure
  6. Out of scope: not covered by Mitos

### 8.2 Metrics

| Metric | Tells us |
|---|---|
| Procedure hit@5 | right procedure in top 5 (after dedup by procedure) |
| MRR | how high the first right procedure ranks |
| Recall@k | all needed procedures found (multi-procedure questions) |
| Fact correctness | each fact in the answer is supported by the source |
| Citation correctness | the cited page really supports the answer (not just "a link is present") |
| Status accuracy | correct answered / ambiguous / not_found |
| Latency (p50, p95) and cost per question | operational targets |

### 8.3 Order and targets

1. Record a **keyword-only (BM25) baseline** first.
2. Then add embeddings and compare.
3. MVP targets: procedure hit@5 ≥ 0.85; citation correctness 100% on displayed answers; correct `not_found` on all out-of-scope questions; no wrong cost or deadline on the test set.
4. Run unit tests in CI on every change (`uv run pytest`); run the eval (`uv run mitos-eval`) on every change to retrieval, prompts or models.

## 9. Non-functional requirements

- **Separation:** ingestion is its own scheduled job. The public API never downloads from Mitos or builds embeddings.
- **Atomic refresh:** each refresh builds and validates a new dataset version; it is activated only if the full run succeeds. Keep the last known-good version for rollback. A failed or partial run never replaces live data.
- **Cost:** target < 20 USD/month at low traffic, validated with real prices. An AWS Budget alarm is an alert, **not a spending cap**, so also: per-IP rate limits, input size limits, max tokens per answer, concurrency limits.
- **Latency:** first answer token < 3 s at p95, measured end to end (request in to first token), warm and cold tracked separately.
- **Reliability:** request timeouts; a clear Greek error message when the model provider is down.
- **Privacy:** do not store raw questions by default. If diagnostic logging is needed: redact before logging, short retention, and say so in the UI. Removing obvious identifiers does not make a query anonymous.
- **Security:** least-privilege IAM, Secrets Manager for any secrets, AWS WAF on the public endpoint.
- **Observability:** structured logs, latency/error/cost metrics, a dashboard, alarms on error rate and failed refreshes.

## 10. Engineering conventions

- Python 3.12+, `uv` for everything (never pip directly).
- Type hints everywhere, explicit code over clever code.
- `pytest`; mock HTTP with `httpx.MockTransport`; no real network calls in unit tests.
- Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- Terraform for all AWS resources; remote state; GitHub Actions with OIDC (no long-lived AWS keys).
- Small pull requests, each with tests.
- Architecture decisions recorded in `docs/decisions/` with the eval numbers behind them.

## 11. Phased plan

**Phase 0: Data (done).** Client, downloader, renderer, tests.

**Phase 1: Validate the real data** (done: decision 001)
1. Run the full download; read 5 to 10 random documents; fix the renderer.
2. Build the structured facts table (6.1) and the provenance fields (5.3), with tests.

**Phase 2: Evaluation set** (done: 30 questions, decisions 002 and 003)
3. Write ~50 questions across all six categories, split dev/test (8.1).
4. Build `mitos-eval` and record the BM25-only baseline.

**Phase 3: Retrieval** (done: decisions 004 and 005)
5. Greek normaliser + chunker with metadata (6.2, 6.3).
6. ~~Postgres + pgvector in Docker~~ Dropped: S3 Vectors chosen (005). Procedure-level retrieval with dedup (6.4); hybrid RRF was measured and not adopted (004).
7. Compare embedding models (7.1) on quality AND runtime cost. Record the decision.

**Phase 4: Grounded answers** (done: decisions 006 to 011; test split run once on 2026-10-22)
8. Structured LLM output, code validation, answered / ambiguous / not_found (6.5, 6.6).
9. CLI: `uv run mitos-ask "ερώτηση"`. Run the eval on the test set.

**Phase 5: API + UI** (done: `/ask`, streaming `/ask/stream` and the Greek UI, decisions 012 and 014)
10. FastAPI `/ask`; streaming only once the basic endpoint is reliable.
11. Mobile-first Greek UI based on the mockup; sources prominent.

**Phase 6: AWS production** (deployed as a demo, decision 013: Lambda, CloudFront, S3, DynamoDB, Budgets, IAM and the custom domain in Terraform. Not done: WAF, GitHub Actions, scheduled refresh, dashboard and alarms)
12. Price the options first (database, model, logging, networking).
13. Terraform: compute, database, S3 snapshots, CloudFront + WAF, Budgets, IAM.
14. GitHub Actions: test, eval, plan, apply (OIDC).
15. Scheduled atomic refresh (EventBridge Scheduler) with failure alerts.
16. Dashboard and alarms.

**Phase 7: Showcase** (README done 2026-10-25; post and case study next)
17. README: architecture diagram, eval results, measured latency, real monthly cost, failure cases, what changed because of the eval.
18. LinkedIn post: the problem, a short demo video, the architecture, one real lesson.

## 12. Risks

| Risk | Mitigation |
|---|---|
| API changes or blocks heavy use (no published terms or limits found) | Polite rate, incremental refresh, keep last good snapshot, contact GRNET before going public |
| Wrong cost or deadline shown to a citizen | Structured facts from source fields, code validation, official link always shown |
| Wrong procedure chosen | Procedure-level retrieval, ambiguous status with clarifying question, eval categories 4 and 5 |
| Mitos itself outdated for some procedures | Show `last_updated`; tell users to confirm on the official page |
| Poor Greek retrieval | Hybrid search, normalisation measured on eval, compare embedding models |
| Embedding model too expensive to host | Judge models on runtime cost too; managed option as fallback |
| Lambda exhausts DB connections | Connection pooling (e.g. RDS Proxy) or move compute to Fargate, decided by measurement |
| Costs grow | Rate and token limits, budget alarms, caching frequent questions |
| Over-engineering for one person | Section 0: simple first, stages added only on eval evidence |

## 13. Open questions

1. Public launch or portfolio demo only? (Affects WAF, rate limits, and contacting GRNET.) **Decided 2026-10-22: a demo, 15 questions a day (013).**
2. Domain: subdomain of pantelis-tsagkas.com? **Decided: `rotato.pantelis-tsagkas.com` (013).**
3. App name? ("Απλά Χαρτιά" in the mockup is a placeholder.) **Decided: "Ρώτα το", tagline "Οι δημόσιες υπηρεσίες, απλά." (013).**
4. Application code licence?
5. Diagnostic logging: none, or redacted with short retention?
