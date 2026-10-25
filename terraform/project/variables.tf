variable "region" {
  description = "Bedrock runs in eu-central-1 for this account (CLAUDE.md), so the vectors sit next to it."
  type        = string
  default     = "eu-central-1"
}

variable "vector_bucket_name" {
  type    = string
  default = "mitos-rag-vectors"
}

variable "index_names" {
  description = "Live index versions, named chunks-<model>-<snapshot date>. Add a new name before removing an old one."
  type        = list(string)
  default     = ["chunks-cohere-v3-20261014"]
}

variable "dimension" {
  description = "Cohere Embed Multilingual v3 returns 1,024 dimensions."
  type        = number
  default     = 1024
}
