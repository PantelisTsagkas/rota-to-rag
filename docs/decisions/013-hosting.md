# 013: Hosting the demo on AWS (Phase 6)

Status: adopted, 2026-10-22. **Chosen: a demo, not a public launch, with a cap of 15 questions a day for the whole site, hosting option A (Lambda) and lock option (a), the DynamoDB lease.** Built in `terraform/app`, `Dockerfile`, `src/mitos_rag/dynamo.py`; not yet applied. Paid calls for this record: none so far (Price List API and account settings reads are free). Project spend is about $5.28.

## What "demo" means here

A website anyone with the link can open, at `https://mitos.pantelis-tsagkas.com`, with no sign-up. The link goes on the CV, the site's case study and LinkedIn; it is not promoted to citizens. All visitors share 15 questions a day. After that, a Greek message says to come back tomorrow. A public launch would need WAF, a much larger budget and a message to GRNET first (PRD 12, 13.1), so it is out of scope.

## The budget

The account cap is $40 a month (`terraform/account`), and it is an alarm, not a hard stop. Bedrock is almost all of the cost:

| | per month, worst case |
|---|---|
| 25 questions a day (the old default) × 31 × $0.056 measured in the browser (012) | about $43, over the cap on its own |
| **15 a day × 31 × $0.056** | **about $26** |

So hosting has to cost close to nothing when idle.

## Options (prices: AWS Price List API, eu-central-1, 2026-10-22)

| option | idle cost a month | notes |
|---|---|---|
| **A. Lambda (arm64) + Function URL + CloudFront, UI on S3** | **about $0.05** (container image storage) | cold start of a few seconds after a quiet period |
| B. ECS Fargate + Application Load Balancer | about $37 to $43 | always on: Fargate ARM 0.25 vCPU and 1 GB $9.78, ALB hours $19.71, ALB capacity units up to $5.84, two public IPv4 addresses $7.30. Over the cap before any question |
| C. AWS App Runner | n/a | closed to new customers since 30 April 2026 (AWS service availability update, March 2026) |
| D. Lambda Web Functions (launched October 2026) | n/a | Node.js only for now; this app is Python |

**Recommendation: A.** Per question it is Lambda for about 20 s at 1 GB: 465 questions a month is about 9,300 GB-seconds, $0.12 at $0.0000133334 per GB-second, and inside Lambda's free 400,000 GB-seconds. DynamoDB on-demand is about four writes a question, $0.0014 a month at $0.7625 per million. ECR storage is $0.10 per GB-month, and the image should be well under 1 GB. CloudFront and S3 for a static UI this small are within the free tiers. The cold start is the price: it is accepted because idle cost is the constraint, and it is measured after deploy (below).

## Design for option A

- **One container image**, arm64, Python 3.12, built with `uv sync --frozen`, with the AWS Lambda Web Adapter as an extension so the FastAPI app runs unchanged. Lambda streams responses natively only for Node.js; the adapter streams for Python. Region eu-central-1, next to Bedrock and the S3 Vectors index.
- **Data in the image**: `data/docs` (82 MB) and `data/facts/facts.jsonl` (5 MB). The 272 MB of document vectors are not needed: retrieval runs in S3 Vectors, and the API only reads the query-vector cache, which may be absent. `data/` is git-ignored, so the image is built locally for now. A CI build (PRD step 14) needs the data from an S3 snapshot first, which the scheduled refresh (step 15) would produce.
- **The Function URL is not public.** Auth type `AWS_IAM`, invoke mode `RESPONSE_STREAM`, and CloudFront signs every request with Origin Access Control. Because Lambda does not accept unsigned payloads, the browser must send the SHA-256 of the request body in `x-amz-content-sha256` for every POST (CloudFront developer guide, "Restrict access to an AWS Lambda function URL origin"). `web/app.js` gets one `crypto.subtle.digest` call.
- **CloudFront**: the UI from a private S3 bucket (OAC), `/ask*` to the Lambda URL with caching off. Certificate from ACM in us-east-1 (required for CloudFront), so Terraform gets a second provider for that region.
- **Domain**: DNS for pantelis-tsagkas.com is on Vercel, so two CNAME records are added there by hand: one for ACM validation, then `mitos` pointing at the CloudFront domain.
- **Limits move to DynamoDB**, because Lambda instances share no memory:
  - the daily count: one atomic update, "add 1 only if below 15", so two requests cannot both slip past the cap;
  - per-IP rows with a TTL, so they clean themselves up;
  - the client IP comes from the `CloudFront-Viewer-Address` header (forwarded by an origin request policy, port stripped). The socket address would be CloudFront's, and `X-Forwarded-For` can be set by the visitor.
- **Privacy stays as in Phase 5**: questions are never stored. Before going live, check that the Web Adapter, Lambda and CloudFront log no request bodies, keep CloudFront access logs off, and set log retention to 7 days.
- **Re-verify after deploy**: whether the "skip paid calls when the visitor leaves" check (012) still sees the disconnect through the Web Adapter and the Function URL. Lambda bills until the handler returns either way.
- **Re-measure after deploy**: a few cold and warm questions, logged with the per-stage timings, before Phase 6 is called done. The cold start counts against the proposed first-text target (012).

## One question at a time: the choice

Locally, the API runs one worker and takes one question at a time, which also keeps the Bedrock calls under their 10 requests a minute per model (`Pacer`). On Lambda each instance has its own memory, so that guarantee needs a new mechanism. The obvious one, reserved concurrency of 1, **is not possible on this account**: its Lambda concurrency limit is 10 (`GetAccountSettings`, 2026-10-22), and Lambda only allows reserving down to 100 unreserved.

| option | how | tradeoff |
|---|---|---|
| **a. DynamoDB lease (recommended)** | before a question, a conditional write takes a "busy" row unless it exists and has not expired; the row carries an expiry of now plus the Lambda timeout; the answer deletes it | works today, about $0; a crashed or timed-out Lambda frees the row by expiry, not by DynamoDB TTL, which can lag up to 48 h |
| b. Quota increase, then reserved concurrency 1 | a Service Quotas request (free) | the simplest code, but the Bedrock request came back at 10 instead of 10,000, so it may not be granted |
| c. No lock | rely on the daily cap and Bedrock throttling with one retry | rare collisions at 15 a day, but a collision becomes an error, not the friendly busy message |

With (a), a second visitor gets the existing Greek busy message ("Η υπηρεσία απαντά σε άλλη ερώτηση. Δοκιμάστε ξανά σε λίγα δευτερόλεπτα.") straight away instead of an error. Today the API waits up to 20 s for the lock before saying busy; on Lambda it answers busy at once, so a waiting visitor does not hold an instance open. (Changed: the lease now waits too, see "The lease waits" below.) The in-process `Pacer` no longer covers anything across instances; that is acceptable because one question at a time makes two model calls, far below 10 a minute each.

## Deployed and measured (2026-10-22)

Applied from `terraform/app` (image `09c1023`). The site answered at `https://d2t85d4wxmlea2.cloudfront.net` on a laptop and a phone.

| check | result |
|---|---|
| UI from S3 through CloudFront | 200 |
| POST straight to the Function URL | 403: IAM auth holds, only CloudFront can call it |
| 1-character question through CloudFront, body hash sent | the Greek 400, before any paid call |
| same without `x-amz-content-sha256` | 403 signature mismatch, as the CloudFront guide says |
| a planted `busy` row | the Greek busy message, 503 |
| `day#<today>` set to 15 | the Greek daily-limit message, 429, and no busy row left behind |
| two real questions (one by a test-script mistake: the region flag did not expand, so the planted row was never written) | answered, streamed piece by piece through CloudFront, ending in the full card; $0.0483 and $0.0484; 16.1 s and 26.5 s total, first token 0.9 s and 4.9 s |
| logs | no question text; the DynamoDB rows hold only a hash of the IP |

Found and fixed (not yet deployed):

- CloudFront refuses `x-amz-content-sha256` in an origin request policy; it is not needed there, because OAC reads it when it signs.
- **Cold start.** The first start after a deploy took 20 s, over Lambda's 10 s init limit, so init ran twice: 34 s for the first visitor. The same startup takes about 3 s in the image at 0.58 CPU locally, so the time is Lambda fetching image blocks on first read. After 17 minutes idle, a normal cold start took 6.0 s end to end (about 5 s of startup), under the limit. `AWS_LWA_ASYNC_INIT=true` stops the double init after a deploy.
- **The uvicorn access log printed the visitor's IP** (it trusts `X-Forwarded-For` from the adapter on 127.0.0.1). Off on Lambda (`--no-access-log`).

Spend for this record: $0.097 (the two questions). Project spend is about $5.38.

The custom domain is not set up: the certificate requested for `mitos.pantelis-tsagkas.com` is pending and will be replaced, because the app was renamed (below). The domain's CAA records allow only sectigo.com, pki.goog and letsencrypt.org, so Vercel DNS needs `0 issue "amazon.com"` before ACM can issue any certificate for it.

## Name (2026-10-22)

"Mitos" in the address would look like the official service. **Chosen name: "Ρώτα το"**, tagline "Οι δημόσιες υπηρεσίες, απλά.", at `rotato.pantelis-tsagkas.com`. The Latin "Rotato" is only the address: an existing Mac app has that name. Brand images in `docs/brand/` (design references; the mark is to be redrawn as SVG for the page and favicon).

## The lease waits (2026-10-22, changed after a real collision)

A question was sent from a phone while a laptop's question was being answered. The phone's request started a second Lambda (6.2 s cold start), found the lease taken and replied "busy" at once, although the laptop's answer ended about 5 s later. **The choice was to wait instead:** `DynamoLease.acquire` now tries the conditional put once a second for up to `BUSY_TIMEOUT_S` (20 s), the same as the local lock, and only then says busy. Cost of a wait: up to 20 s of a 1 GB Lambda (under $0.001) and about 20 small conditional writes. CloudFront's origin read timeout is 60 s, and nothing is streamed until the lease is held. The busy and rate-limit messages in the UI also gained a "Δοκιμάστε ξανά" button that resends the same question.
