# The API on Lambda (decision 013): FastAPI unchanged, run by the AWS Lambda Web Adapter, which
# streams the response through a Function URL in RESPONSE_STREAM mode. arm64 to match Lambda.
#
# Build locally (data/ is git-ignored, so CI cannot build this yet):
#   docker build --platform linux/arm64 --provenance=false -t mitos-rag-api .
FROM ghcr.io/astral-sh/uv:0.11.7 AS uv

FROM public.ecr.aws/docker/library/python:3.12-slim
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter
COPY --from=uv /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Only what the API reads: the rendered procedures and the facts table. Retrieval runs in
# S3 Vectors, so the 272 MB of document vectors stay out.
COPY data/docs ./data/docs
COPY data/facts/facts.jsonl ./data/facts/facts.jsonl

# Async init: Lambda allows 10 s of init; the first start after a deploy took about 20 s (image
# blocks are fetched lazily), and a timed-out init starts again from scratch (34 s in all).
ENV AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    AWS_LWA_ASYNC_INIT=true \
    AWS_LWA_INVOKE_MODE=response_stream \
    PATH="/app/.venv/bin:$PATH"
CMD ["mitos-api", "--host", "0.0.0.0", "--port", "8080", "--no-web", "--no-access-log", "--embeddings", "/tmp/embeddings"]
