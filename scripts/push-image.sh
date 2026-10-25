#!/usr/bin/env bash
# Build the API image (Dockerfile) and push it to the ECR repository from terraform/app.
# Prints the tag to pass as `terraform apply -var image_tag=<tag>`. The tag is the git commit,
# plus "-dirty" and a timestamp when the tree has uncommitted changes (tags are immutable).
set -euo pipefail

cd "$(dirname "$0")/.."
region="${AWS_REGION:-eu-central-1}"
repo="$(terraform -chdir=terraform/app output -raw ecr_repository_url)"
tag="$(git rev-parse --short HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
  tag="${tag}-dirty-$(date +%Y%m%d%H%M%S)"
fi

aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "${repo%%/*}" >&2
docker build --platform linux/arm64 --provenance=false -t "${repo}:${tag}" . >&2
docker push "${repo}:${tag}" >&2
echo "$tag"
