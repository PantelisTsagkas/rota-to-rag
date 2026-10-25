# The API: one container image on Lambda (arm64), reached only through CloudFront.

resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true # images are rebuilt from the repo; destroy must not get stuck

  image_scanning_configuration {
    scan_on_push = true
  }
}

# ECR storage is the only idle cost ($0.10 per GB-month): keep the last three images.
resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 3 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 3 }
      action       = { type = "expire" }
    }]
  })
}

data "aws_ecr_image" "api" {
  count           = var.image_tag == "" ? 0 : 1
  repository_name = aws_ecr_repository.api.name
  image_tag       = var.image_tag
}

# Daily count, per-IP minute counts and the busy lease (src/mitos_rag/dynamo.py).
resource "aws_dynamodb_table" "limits" {
  name         = "${local.name}-limits"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "assume_lambda" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.assume_lambda.json
}

locals {
  vector_bucket = split("/", var.vector_index)[0]
  vector_index  = split("/", var.vector_index)[1]
  # The eu. inference profiles route to these regions (GetInferenceProfile, 2026-10-22).
  eu_model_regions = ["eu-central-1", "eu-north-1", "eu-south-1", "eu-south-2", "eu-west-1", "eu-west-3"]
  claude_models    = ["anthropic.claude-sonnet-4-6", "anthropic.claude-haiku-4-5-20251001-v1:0"]
}

data "aws_iam_policy_document" "api" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }

  statement {
    sid     = "ClaudeThroughEuProfiles"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(
      [for model in local.claude_models : "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/eu.${model}"],
      flatten([for region in local.eu_model_regions : [for model in local.claude_models : "arn:aws:bedrock:${region}::foundation-model/${model}"]]),
    )
  }

  statement {
    sid       = "QueryEmbeddings"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.region}::foundation-model/cohere.embed-multilingual-v3"]
  }

  statement {
    sid       = "WarmUp"
    actions   = ["bedrock:CountTokens"]
    resources = [for model in local.claude_models : "arn:aws:bedrock:${var.region}::foundation-model/${model}"]
  }

  # QueryVectors with returnMetadata also needs GetVectors.
  statement {
    sid       = "Vectors"
    actions   = ["s3vectors:QueryVectors", "s3vectors:GetVectors"]
    resources = ["arn:aws:s3vectors:${var.region}:${local.account_id}:bucket/${local.vector_bucket}/index/${local.vector_index}"]
  }

  statement {
    sid       = "Limits"
    actions   = ["dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.limits.arn]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "${local.name}-api"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_lambda_function" "api" {
  count         = var.image_tag == "" ? 0 : 1
  function_name = "${local.name}-api"
  role          = aws_iam_role.api.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.api.repository_url}@${data.aws_ecr_image.api[0].image_digest}"
  architectures = ["arm64"]
  memory_size   = 1024 # the app holds about 275 MB (measured locally); memory also buys CPU for the cold start
  timeout       = var.lambda_timeout_s

  environment {
    variables = {
      MITOS_LIMITS_TABLE      = aws_dynamodb_table.limits.name
      MITOS_CLIENT_IP_HEADER  = "cloudfront-viewer-address"
      MITOS_DAILY_QUESTIONS   = tostring(var.daily_questions)
      MITOS_PER_IP_PER_MINUTE = tostring(var.per_ip_per_minute)
      MITOS_LEASE_S           = tostring(var.lambda_timeout_s)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.api.name
  }

  depends_on = [aws_iam_role_policy.api]
}

# IAM auth: only CloudFront, signing with origin access control, can call it.
resource "aws_lambda_function_url" "api" {
  count              = var.image_tag == "" ? 0 : 1
  function_name      = aws_lambda_function.api[0].function_name
  authorization_type = "AWS_IAM"
  invoke_mode        = "RESPONSE_STREAM"
}

# Both permissions, as the CloudFront developer guide shows for function URL origins.
resource "aws_lambda_permission" "cloudfront_url" {
  count         = var.image_tag == "" ? 0 : 1
  statement_id  = "AllowCloudFrontServicePrincipal"
  action        = "lambda:InvokeFunctionUrl"
  function_name = aws_lambda_function.api[0].function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.site[0].arn
}

resource "aws_lambda_permission" "cloudfront_invoke" {
  count         = var.image_tag == "" ? 0 : 1
  statement_id  = "AllowCloudFrontServicePrincipalInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api[0].function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.site[0].arn
}
