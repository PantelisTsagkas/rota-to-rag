# mitos-rag

A Greek-language assistant that answers questions about Greek public administration procedures (documents, cost, steps, where to apply), using the official registry **Μίτος** (mitos.gov.gr).

**Status: ACTIVE.** Phases 0 to 5 are built and measured: it runs end to end locally (CLI, HTTP API and a mobile Greek UI with streamed answers). It is deployed on AWS as a demo (Lambda, CloudFront, S3, DynamoDB: [`013`](docs/decisions/013-hosting.md)) and is being renamed "Ρώτα το"; the writeup (Phase 7) is next. See [`docs/PRD.md`](docs/PRD.md) for the plan.

How a question is answered:

1. The question is embedded with Cohere Embed Multilingual v3 on Bedrock and searched in Amazon S3 Vectors (65,312 chunks from all 4,406 procedures). Dense retrieval beat BM25 by a wide margin on Greek everyday wording ([`002`](docs/decisions/002-bm25-baseline.md), [`003`](docs/decisions/003-greek-stemming.md), [`004`](docs/decisions/004-dense-retrieval-cohere.md), [`005`](docs/decisions/005-vector-store-s3-vectors.md)).
2. Claude Haiku 4.5 reranks the top 30 procedures by title ([`008`](docs/decisions/008-rerank.md), [`009`](docs/decisions/009-rerank-in-the-pipeline.md)).
3. Claude Sonnet 4.6 answers in Greek with structured output: answered, ambiguous (with a clarifying question) or not found ([`006`](docs/decisions/006-grounded-answers-haiku.md), [`007`](docs/decisions/007-answer-model-sonnet-4-6.md)).
4. Code checks every cited procedure, fee and duration against the structured facts table and the procedure text, and warns when a number cannot be grounded ([`010`](docs/decisions/010-text-grounded-checker.md), [`011`](docs/decisions/011-facts-row-cap.md)).
5. Every answer links the official Mitos page and credits Mitos ([`012`](docs/decisions/012-streaming-and-procedure-links.md)).

Results on the held-out test split, run once: retrieval hit@5 0.89, status accuracy 0.80, 0 invalid citations, 0 unsupported claims. On dev, BM25 scored hit@5 0.50, Cohere dense retrieval 0.89, and the reranker took it to 1.00. The weak spot is everyday questions that need more than one procedure. Each question costs about $0.045, and the whole project has cost about $5.30 in AWS so far. AWS practice is tracked in [`docs/well-architected.md`](docs/well-architected.md).

## Quick start

```bash
uv sync
uv run pytest

# Download 20 procedures to try it out
uv run mitos-download --out data/raw --max-services 20

# Turn them into clean Markdown documents
uv run mitos-render --raw data/raw --out data/docs

# Extract structured facts (costs, deadlines, online or not)
uv run mitos-facts --raw data/raw --out data/facts/facts.jsonl

# Retrieval eval on the dev questions (needs the full download and render)
uv run mitos-eval --split dev
```

Dense retrieval and the S3 Vectors index need AWS credentials and cost money; prices are in the decision records:

```bash
uv run mitos-embed --model cohere                       # Bedrock embeddings, about $1.70 for the corpus
(cd terraform/project && terraform init && terraform apply)   # vector bucket and index
uv run mitos-index --bucket mitos-rag-vectors --index chunks-cohere-v3-20261014 --resume
uv run mitos-eval --split dev --dense cohere --vector-index mitos-rag-vectors/chunks-cohere-v3-20261014

# Ask a question: Haiku 4.5 reranks, Sonnet 4.6 answers (about $0.05 per new question)
uv run mitos-ask "ποσο κοστιζει διαβατηριο για παιδι 10 χρονων"
uv run mitos-ask --model haiku-4.5 "..."     # Haiku answers, about $0.018
uv run mitos-ask --rerank none "..."         # the dense top 5, as in 006 and 007

# HTTP API and Greek UI: one worker, no question storage, per-IP and daily limits
uv run mitos-api                             # open http://127.0.0.1:8000 (each question about $0.05)
curl -s localhost:8000/ask -H 'content-type: application/json' -d '{"question": "διαβατηριο"}'

# Answer eval: quote the cost first (free), then run it
uv run mitos-answer-eval --split dev --dry-run
uv run mitos-answer-eval --split dev
```

Then open any file in `data/docs/`.

### Deploy the demo (decision 013)

The demo runs the same API on AWS Lambda behind CloudFront, with the UI on S3 and the limits in DynamoDB. It needs Docker and the full download and render (the image includes `data/docs` and the facts table):

```bash
cd terraform/app
terraform init
terraform apply -target=aws_ecr_repository.api -target=aws_ecr_lifecycle_policy.api
../../scripts/push-image.sh                    # builds the arm64 image, pushes it, prints the tag
terraform apply -var image_tag=<tag>           # prints site_url
```

Idle, it costs about $0.03 a month (image storage). Every question costs about $0.05, with at most 15 a day for the whole site. `terraform destroy` removes all of it.

Downloading all ~4,400 procedures takes roughly 30 to 45 minutes with the default polite delay (0.25 s between requests). Re-runs only fetch procedures whose `last_updated` changed.

## Layout

```
src/mitos_rag/
  client.py     typed client for the Mitos API (pagination, retries, backoff)
  download.py   saves raw JSON, incremental
  render.py     raw JSON -> one Greek Markdown document per procedure
  facts.py      raw JSON -> structured facts (costs, deadlines, online or not) with provenance
  text.py       Greek tokeniser (casefold, accent folding, optional Snowball stemming)
  bm25.py       Okapi BM25
  corpus.py     rendered docs -> retrieval units (whole doc or section)
  chunk.py      sections -> chunks of at most 1,500 characters, with title prefix
  embed.py      mitos-embed: Bedrock embeddings with a resumable cache
  dense.py      exact cosine search and reciprocal rank fusion
  vectorstore.py  mitos-index: upload to S3 Vectors, and approximate search over it
  rerank.py     mitos-rerank-eval: Haiku reranks the dense candidates by title
  pacing.py     shared pacer for the Bedrock request quota
  answer.py     mitos-ask: evidence, structured answer from Claude, sources and attribution
  api.py        mitos-api: FastAPI /ask and /ask/stream with limits and Greek errors; serves web/
  dynamo.py     the limits and the one-at-a-time lease in DynamoDB, for Lambda
  validate.py   code checks: cited ids retrieved, typed facts match the facts table, amounts and durations in the text match the facts table or the cited procedure's text
  answer_eval.py  mitos-answer-eval: status, citations, facts, cost and latency
  evaluate.py   mitos-eval: hit@k, MRR, recall@k on eval/
web/            mobile-first Greek UI: static HTML, CSS and JS over /ask/stream, no build step
eval/           dev and held-out test questions
tests/          pytest, no real network calls
docs/PRD.md     product requirements and plan
docs/decisions/ decisions with the numbers behind them
docs/well-architected.md  AWS Well-Architected checklist, done and planned
terraform/account  account budget alarm (never destroy)
terraform/project  project resources: S3 Vectors bucket and index
terraform/app      the demo: Lambda, Function URL, DynamoDB, S3, CloudFront, certificate
Dockerfile         the API image for Lambda, with the Lambda Web Adapter
scripts/push-image.sh  build and push the image, print its tag
```

## Data licence

Procedure data comes from the Εθνικό Μητρώο Διοικητικών Διαδικασιών «Μίτος» (mitos.gov.gr) and is licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Any published copy of the processed data must keep the attribution and the same licence.
