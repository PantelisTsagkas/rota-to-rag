# 005: Vector store: Amazon S3 Vectors

Status: **chosen on 2026-10-17**, over the no-database option recommended in this document. The index was built and checked on dev the same day.

## Context

Decision 004 adopted dense-only retrieval with Cohere Embed Multilingual v3: 65,312 chunks, 1,024 dimensions. BM25 is not used at runtime, because equal-weight RRF made hit@5 worse on dev and on test. That changes PRD 7.2. Postgres was the "leading choice" because it kept full-text search and vectors in one place, and that advantage went away with 004.

The vector store must:
- give the eval's results in production,
- fit the PRD target of under 20 USD per month at low traffic,
- meet a cold first-token p95 under 3 s,
- support the atomic refresh from PRD 9.

## Options and prices

Prices are from the AWS Price List API for eu-central-1, checked on 2026-10-17. Months are 730 hours. Bedrock, API Gateway, CloudFront and WAF cost the same under every option and are left out.

| Option | Idle cost per month | Cold start | Notes |
|---|---|---|---|
| A. RDS PostgreSQL + pgvector, db.t4g.micro Single-AZ ($0.019/h) with 20 GB gp3 ($0.137/GB-month) | $16.61 for the database. **$25.37** once the Lambda sits in a VPC with one Bedrock interface endpoint ($0.012/h). $54.57 with a NAT gateway ($0.052/h) instead | fine | The micro has 1 GB of RAM for an HNSW index. Its one-place advantage went with 004. |
| B. Aurora PostgreSQL Serverless v2, minimum 0 ACU, through the Data API | About $0.15 for storage ($0.119/GB-month). Each wake runs at least 5 minutes at $0.14 per ACU-hour | **about 15 s resume, 30 s or more after 24 h paused** (AWS docs) | No VPC endpoint is needed, but the resume time breaks the 3 s target. RDS Proxy would stop it from pausing at all. |
| C. Amazon S3 Vectors | About $0.02 for storage ($0.064/GB-month for 0.27 GB) | sub-second per AWS (measured below) | Approximate search. Each full load costs about $0.06 ($0.214 per GB put). A query is $0.0000027 plus $0.0000042 per GB processed: about $0.004 per 1,000 questions. Section text and facts still need S3. There is no local emulator. |
| D. No database: a float16 matrix plus SQLite in S3, exact numpy search in Lambda | About $0.01 for S3 storage. About $0.10 per 1,000 questions for the extra Lambda memory | load about 230 MB (not measured) | Exact search, the same code as the eval. Over the 250 MB zip limit, so it needs a container image or a /tmp download. |

Option D was recommended: it costs the least and makes production identical to the eval by construction. C, a managed AWS vector store, was chosen instead. C keeps the stack serverless and the Lambda out of a VPC, as D does, and still costs pennies.

## Free checks before building (dev, exact search simulated)

- **Top-K cap.** S3 Vectors returns the nearest chunks, not all 65,312. A procedure scores by its best chunk, so a short list could drop a procedure. Cutting the exact ranking to the top 30, 100 or 1,000 chunks gave the same dev numbers as the full ranking: hit@5 0.89, MRR 0.70, recall@10 0.88. `vectorstore.py` asks for 100, one result page.
- **float16 storage** (for option D) also changed no metric.
- **Search speed** (local, M4 Pro): exact float32 search over all chunks takes 1.3 ms per question.

## Build

- **Terraform** (`terraform/project`): the vector bucket `mitos-rag-vectors` and the index `chunks-cohere-v3-20261014` (float32, 1,024 dimensions, cosine, SSE-S3). One index per dataset version, kept side by side: `var.index_names` is a list, and each name is its own `for_each` instance. A refresh adds the new name, uploads and evaluates it, switches the app, and only then removes the old name. That is the atomic refresh from PRD 9. Renaming an index in place would destroy the live one first, because `index_name` forces replacement. The first apply used a single index. A `moved` block converted it with 0 resources destroyed, so the uploaded vectors stayed.
- **Upload**: `uv run mitos-index --bucket mitos-rag-vectors --index chunks-cohere-v3-20261014`. Keys are `<procedure_id>:<chunk_index>`, so a query result names its procedure without a metadata read. `procedure_id` is also filterable metadata. The run puts about 0.269 GB.
- **Upload time**: boto3 sends vectors as JSON text, about 20 characters per float, so 65k vectors become about 1.3 GB on the wire. The first run used batches of 500, about 11 MB of JSON per call. After 21 minutes and 55,000 vectors it failed with `RequestTimeoutException` ("the server timed out waiting for the request").

The uploader was changed in three ways:
- batches of 100,
- floats sent with 9 significant digits, which rebuilds the same float32 exactly and makes the JSON 34% smaller,
- `--resume`, which lists the keys already in the index (1,000 per ListVectors call) and skips them.

The resumed run put the last 10,312 vectors at about 1,000 per minute.

Billing counts the float32 bytes, not the JSON. The scheduled refresh (Phase 6) should run inside AWS, not from a home connection.

## Results (dev, 18 scored items, 2026-10-17)

`uv run mitos-eval --split dev --dense cohere --vector-index mitos-rag-vectors/chunks-cohere-v3-20261014`

| config | hit@1 | hit@5 | hit@10 | MRR | recall@5 | recall@10 |
|---|---|---|---|---|---|---|
| dense/cohere (exact, 004) | 0.56 | 0.89 | 0.94 | 0.70 | 0.79 | 0.88 |
| **s3vectors/cohere/top100** | **0.56** | **0.89** | **0.94** | **0.69** | **0.79** | **0.88** |

- **Retrieval quality holds.** Only MRR moves, by 0.01.
- **The search is approximate, and it shows at the edges.** 5 of the 20 questions have a different top 5 or a different first relevant rank:
  - q06 moves from rank 4 to rank 5. It is still a hit@5, but only just.
  - q15 moves from 6 to 7, and q24 from 13 to 16. Both were already misses in 004.
  - q16 stays at rank 1. One neighbour, 302838 (exact cosine 0.657), is missing from the ANN top 5: the index skipped a close chunk.
  - q28 (out of scope) swaps one low-scoring neighbour.

  The changes sit inside score gaps of 0.001 to 0.005, where procedures are nearly tied.
- **Scores are cosine similarity.** 1 − distance matches the exact cosine of the same top procedure to within 6e-4 on all 20 questions. The small error suggests the index stores or compares vectors at reduced precision. A not-found threshold (Phase 4) will rest on these scores, so it should be set on S3 Vectors scores, not the exact ones.
- **Query latency, measured from a home connection in Greece to eu-central-1.** Over 20 sequential queries on one client: p50 177 ms, minimum 134 ms, maximum 1,305 ms. That includes the internet round trip. The maximum may include TLS connection setup on the first call. Latency from a Lambda in the same region is measured in Phase 6.
- **Cost of the build (estimate).** About 0.27 GB put ($0.06), 214 PutVectors calls (110 of 500, then 104 of 100), 55 ListVectors calls and 40 queries: under $0.10 in total. The index then costs about $0.02 per month to keep.
## Consequences

- PRD 7.2 is decided. Phase 3 step 6 (Postgres + pgvector in Docker) is dropped.
- With 18 dev items, a 0.01 change in MRR is a single rank. Rerun this comparison when the eval set grows, and before the test split is used again.
- Evals and `mitos-ask` now call AWS for retrieval. Each run costs under $0.001, but it needs credentials and a network.
- `DenseRetriever` stays as the exact reference. `mitos-eval --vector-index` runs both, so any ANN loss shows up in the table.
- Section text and structured facts need their own store for Phase 4. Phase 4 starts with local files, and the production store is decided in Phase 6.
