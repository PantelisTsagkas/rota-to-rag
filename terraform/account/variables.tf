variable "region" {
  description = "Region for the provider. Budgets is global, so this only affects the API endpoint."
  type        = string
  default     = "eu-north-1"
}

variable "alert_email" {
  description = "Where budget alerts go."
  type        = string
}

variable "monthly_limit_usd" {
  description = "Monthly account spend cap in USD (Budgets does not accept GBP)."
  type        = string
  default     = "40"
}

variable "actual_thresholds_percent" {
  description = "Actual-spend alert thresholds as a percentage of the limit. 50 and 80 are roughly 15 and 24 GBP."
  type        = list(number)
  default     = [50, 80]
}
