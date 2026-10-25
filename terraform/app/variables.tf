variable "region" {
  description = "Next to Bedrock and the S3 Vectors index (terraform/project)."
  type        = string
  default     = "eu-central-1"
}

variable "image_tag" {
  description = "Tag pushed by scripts/push-image.sh. Empty only for the first, ECR-only apply."
  type        = string
  default     = ""
}

variable "vector_index" {
  description = "BUCKET/INDEX the API reads, from terraform/project's vector_indexes output."
  type        = string
  default     = "mitos-rag-vectors/chunks-cohere-v3-20261014"
}

variable "daily_questions" {
  description = "Questions per UTC day for the whole site: 15 × 31 × $0.056 is about $26 a month at most (013)."
  type        = number
  default     = 15
}

variable "per_ip_per_minute" {
  type    = number
  default = 3
}

variable "lambda_timeout_s" {
  description = "Also the busy lease: only a Lambda that died leaves it to expire."
  type        = number
  default     = 150
}

variable "domain_name" {
  description = "The app is \"Ρώτα το\" (013): \"mitos\" in the address would look like the official service."
  type        = string
  default     = "rotato.pantelis-tsagkas.com"
}

variable "custom_domain_ready" {
  description = <<-EOT
    False until the ACM validation CNAME (output acm_validation_records) is added at the DNS
    provider (Vercel) and the certificate shows ISSUED. Then true: CloudFront answers on
    var.domain_name, and the second CNAME (output cloudfront_domain) can be added.
    True by default since rotato's certificate was validated (2026-10-25), so a plan that
    forgets the flag cannot detach the domain. A new domain_name needs false first.
  EOT
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Short on purpose (PRD 9): the logs carry status, cost and timings, never questions."
  type        = number
  default     = 7
}
