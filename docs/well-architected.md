# AWS Well-Architected checklist

How mitos-rag lines up with the six pillars of the AWS Well-Architected Framework. Update this file whenever infrastructure changes. Each row says what is in place, or which phase adds it (PRD section 11).

Last reviewed: 2026-10-22. Deployed: the account budget (`terraform/account`), the S3 Vectors bucket and index (`terraform/project`), and the demo (`terraform/app`, decision 013): the API on Lambda behind CloudFront, the UI on S3, limits in DynamoDB.

## Operational excellence

| Practice | State |
|---|---|
| All infrastructure as code (Terraform) | Done: `terraform/account`, `terraform/project`, `terraform/app`. Nothing is created by hand; the image is built and pushed by `scripts/push-image.sh`. |
| Decisions recorded with their evidence | Done: `docs/decisions/` keeps the eval numbers and the prices behind each choice. |
| Tests on every change, no real network in unit tests | Done: pytest with `httpx.MockTransport` and botocore `Stubber`. |
| Remote Terraform state with locking | **Phase 6.** Both roots use local state for now. |
| CI/CD: test, eval, plan, apply with GitHub OIDC | **Phase 6** (PRD step 14). There is no GitHub remote yet. |
| Structured logs, dashboard, alarms on errors and failed refreshes | Partly: one log line per question with status, cost, per-stage seconds and tokens, in a 7-day log group. Dashboard and alarms are PRD step 16. |

## Security

| Practice | State |
|---|---|
| Encryption at rest | Done: the vector index uses SSE-S3 (AES256); the UI bucket, the DynamoDB table and ECR use their AWS-owned default encryption. |
| No long-lived keys | Done locally: AWS SSO sessions. Phase 6 uses OIDC for CI. |
| Least-privilege IAM for the app | Done (013): the Lambda role can invoke only Sonnet 4.6 and Haiku 4.5 through their eu. profiles and Cohere embeddings, query this one vector index (`QueryVectors`, `GetVectors` for metadata), write its own log group and three item actions on the limits table. |
| Secrets in Secrets Manager | Nothing secret yet: Mitos needs no key, and Bedrock uses IAM. |
| Edge protection: WAF, per-IP rate limits, input size limits | Done for a demo (013): the Lambda function URL uses IAM auth, so only CloudFront (origin access control) can call it (a direct POST gets 403, checked); the UI bucket is private behind OAC. Limits in DynamoDB: 3 to 300 characters, 3 per IP per minute (from `CloudFront-Viewer-Address`), 15 a day for the whole site, one question at a time (a lease). WAF is left out for a demo; a public launch needs it. |
| Privacy: raw questions not stored by default | Done in `api.py` (Phase 5): no response, rerank or query-embedding cache writes, and logs carry status, cost and timings, never question text (checked in the Lambda logs). On Lambda the uvicorn access log is off, because it printed the visitor's IP; DynamoDB keeps only a SHA-256 prefix of the IP, for about two minutes. CloudFront access logs are off. Answer feedback (014) stores status, cited procedure ids, cost, date and the vote for 90 days, never the question or IP, and takes fixed reasons only, no free text. |

## Reliability

| Practice | State |
|---|---|
| Atomic refresh with rollback | In Terraform (decision 005): `var.index_names` keeps versioned indexes side by side, and the old one stays live until the new one passes the eval. The scheduled job is **Phase 6**. |
| Idempotent, resumable data loads | Done: `mitos-download` is incremental, the `mitos-embed` cache resumes, and `mitos-index --resume` skips keys already put. |
| Timeouts and retries on calls out | Done for Mitos and Bedrock. The S3 Vectors upload uses small batches after a request timeout (005). |
| A clear Greek error when the model provider is down | Done (Phase 5), and a Greek error when the limits table cannot be reached (the question is refused, not answered uncounted). |
| Known throughput ceiling | One question at a time (the DynamoDB lease), and Lambda concurrency is 10 for the account. Bedrock allows Sonnet 4.6 and Haiku 4.5 10 requests per minute each and Cohere 20. |

## Performance efficiency

| Practice | State |
|---|---|
| Choices made on measured numbers | Done: retrieval quality from the eval (002 to 005), and every price from the Price List API. |
| Serverless and managed services | Done: Bedrock, S3 Vectors, Lambda (arm64 container), DynamoDB on demand, CloudFront and S3. Nothing runs all the time. |
| Latency target: first token p95 under 3 s, cold and warm measured separately | Measuring (013): warm requests through CloudFront are fast; the first cold start after deploy was 34 s and is being fixed. S3 Vectors query latency is recorded in 005. |

## Cost optimization

| Practice | State |
|---|---|
| Account budget alarm | Done: 40 USD per month, alerts at 50% and 80% of actual spend and at 100% of forecast. |
| Price checked before every paid call or new service | Done, and recorded in `docs/decisions/` and CLAUDE.md. |
| Idle cost near zero | Done: the vector index costs about $0.02 a month and the demo's container image about $0.03 (ECR); Lambda, DynamoDB on demand, CloudFront and S3 cost nothing idle. Fargate with a load balancer would have been about $37 to $43 a month (013). |
| Token and request caps per answer | Partly done (006): `max_tokens` 2,000, an evidence budget of 12,000 characters, Haiku calls paced to 10 per minute, and a dry-run cost quote before every eval. Facts rows capped at 6,000 characters in the prompt (011). In the API (Phase 5): a daily question cap (15, about $0.84 a day and $26 a month at most, 013), per-IP and input-size limits, a 45 s model timeout and one retry. |
| `terraform destroy` when the project is paused | Possible: `terraform/app` (the demo) and `terraform/project` (the vectors) each destroy on their own, and the budget lives in its own root. |

## Sustainability

| Practice | State |
|---|---|
| No idle compute | Done: nothing is provisioned to run continuously. |
| Compute and data in one region | Done: Lambda, Bedrock, S3 Vectors and DynamoDB are all in eu-central-1, so there are no cross-region calls for retrieval. |
| Re-embed only changed chunks | Done: the embedding cache is keyed by content hash. |
