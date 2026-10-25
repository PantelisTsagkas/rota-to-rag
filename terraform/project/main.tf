# Project infrastructure for mitos-rag. Safe to `terraform destroy`: the account
# budget lives in terraform/account with its own state.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      ManagedBy = "terraform"
      Project   = "mitos-rag"
    }
  }
}

# Chunk embeddings (decision 005). Vectors are written by `uv run mitos-index`, not by Terraform.
resource "aws_s3vectors_vector_bucket" "chunks" {
  vector_bucket_name = var.vector_bucket_name
}

# One index per dataset version, side by side (PRD 9 atomic refresh): add the new version's
# name to var.index_names, upload and eval it, switch the app to it, and only then remove
# the old name. Renaming an index in place would destroy it first, so never do that.
resource "aws_s3vectors_index" "chunks" {
  for_each = toset(var.index_names)

  vector_bucket_name = aws_s3vectors_vector_bucket.chunks.vector_bucket_name
  index_name         = each.value
  data_type          = "float32"
  dimension          = var.dimension
  distance_metric    = "cosine"
}

moved {
  from = aws_s3vectors_index.chunks
  to   = aws_s3vectors_index.chunks["chunks-cohere-v3-20261014"]
}
