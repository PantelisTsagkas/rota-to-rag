# The public demo (decision 013): the API on Lambda behind CloudFront, the UI on S3.
# Safe to `terraform destroy`: the vectors live in terraform/project and the account budget
# in terraform/account, each with its own state.
#
# Order on a fresh account (the Lambda needs an image that exists):
#   terraform apply -target=aws_ecr_repository.api
#   ../../scripts/push-image.sh                 # prints the tag
#   terraform apply -var image_tag=<tag>
# The custom domain is a second step: see var.custom_domain_ready.

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

# CloudFront only takes certificates from us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      ManagedBy = "terraform"
      Project   = "mitos-rag"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  name       = "mitos-rag"
}
