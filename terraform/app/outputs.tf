output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "cloudfront_domain" {
  description = "The site before the custom domain, and the target of the `mitos` CNAME in Vercel."
  value       = var.image_tag == "" ? null : aws_cloudfront_distribution.site[0].domain_name
}

output "acm_validation_records" {
  description = "Add each as a CNAME in Vercel's DNS for pantelis-tsagkas.com."
  value = [for option in aws_acm_certificate.site.domain_validation_options : {
    name  = option.resource_record_name
    type  = option.resource_record_type
    value = option.resource_record_value
  }]
}

output "site_url" {
  value = var.image_tag == "" ? null : (var.custom_domain_ready ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.site[0].domain_name}")
}

output "dashboard_url" {
  description = "Questions, statuses, estimated cost, feedback and failures; opens with the console login."
  value       = var.image_tag == "" ? null : "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.demo[0].dashboard_name}"
}

output "function_url" {
  description = "IAM auth: a direct request without CloudFront's signature gets 403."
  value       = var.image_tag == "" ? null : aws_lambda_function_url.api[0].function_url
}
