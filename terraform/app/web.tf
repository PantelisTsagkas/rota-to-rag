# The UI on a private S3 bucket and one CloudFront distribution in front of the UI and the API.
# Same origin for both, so the page calls `ask/stream` with no CORS.

resource "aws_s3_bucket" "web" {
  bucket_prefix = "${local.name}-web-"
  force_destroy = true # the files come from web/ in the repo
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

locals {
  web_dir = "${path.module}/../../web"
  content_types = {
    html  = "text/html; charset=utf-8"
    js    = "text/javascript; charset=utf-8"
    css   = "text/css; charset=utf-8"
    svg   = "image/svg+xml"
    png   = "image/png"
    woff2 = "font/woff2"
    txt   = "text/plain; charset=utf-8" # fonts/OFL.txt, the font's licence, travels with it
  }
}

resource "aws_s3_object" "web" {
  for_each     = fileset(local.web_dir, "**")
  bucket       = aws_s3_bucket.web.id
  key          = each.value
  source       = "${local.web_dir}/${each.value}"
  etag         = filemd5("${local.web_dir}/${each.value}")
  content_type = local.content_types[reverse(split(".", each.value))[0]]
  # Five minutes at the edge: a UI change shows up soon without an invalidation.
  cache_control = "public, max-age=300"
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${local.name}-web"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_origin_access_control" "api" {
  name                              = "${local.name}-api"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}

# Only what the API needs: the body's type and the visitor's address for the per-IP limit.
# No cookies, no query strings. The browser's x-amz-content-sha256 is not listed (CloudFront
# refuses it here): OAC uses it when it signs the request.
resource "aws_cloudfront_origin_request_policy" "api" {
  name = "${local.name}-api"

  headers_config {
    header_behavior = "whitelist"
    headers {
      items = ["content-type", "CloudFront-Viewer-Address"]
    }
  }

  cookies_config {
    cookie_behavior = "none"
  }

  query_strings_config {
    query_string_behavior = "none"
  }
}

locals {
  api_origin_domain = var.image_tag == "" ? "" : trimsuffix(trimprefix(aws_lambda_function_url.api[0].function_url, "https://"), "/")
}

resource "aws_cloudfront_distribution" "site" {
  count               = var.image_tag == "" ? 0 : 1
  enabled             = true
  is_ipv6_enabled     = true
  http_version        = "http2and3"
  comment             = "mitos-rag demo (decision 013)"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  aliases             = var.custom_domain_ready ? [var.domain_name] : []

  origin {
    origin_id                = "web"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  origin {
    origin_id                = "api"
    domain_name              = local.api_origin_domain
    origin_access_control_id = aws_cloudfront_origin_access_control.api.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      # The longest gap between streamed bytes: a model call can take 45 s before its retry.
      origin_read_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id           = "web"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  ordered_cache_behavior {
    path_pattern               = "/ask*"
    target_origin_id           = "api"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = false # streamed NDJSON must not be buffered
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.api.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = !var.custom_domain_ready
    acm_certificate_arn            = var.custom_domain_ready ? aws_acm_certificate_validation.site[0].certificate_arn : null
    ssl_support_method             = var.custom_domain_ready ? "sni-only" : null
    minimum_protocol_version       = var.custom_domain_ready ? "TLSv1.2_2021" : null
  }
}

data "aws_iam_policy_document" "web" {
  count = var.image_tag == "" ? 0 : 1
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.web.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.site[0].arn]
    }
  }
}

resource "aws_s3_bucket_policy" "web" {
  count      = var.image_tag == "" ? 0 : 1
  bucket     = aws_s3_bucket.web.id
  policy     = data.aws_iam_policy_document.web[0].json
  depends_on = [aws_s3_bucket_public_access_block.web]
}

# The certificate is requested on the first full apply; its validation CNAME is added by hand in
# Vercel. Only with custom_domain_ready = true does Terraform wait for it to be issued.
resource "aws_acm_certificate" "site" {
  provider          = aws.us_east_1
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate_validation" "site" {
  count           = var.custom_domain_ready ? 1 : 0
  provider        = aws.us_east_1
  certificate_arn = aws_acm_certificate.site.arn
}
