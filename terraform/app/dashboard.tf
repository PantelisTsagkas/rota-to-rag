# One CloudWatch dashboard for the live demo: questions against the daily cap, statuses,
# estimated cost, feedback and failures. Everything comes from the API's log lines (status,
# cost and timings, never the question) and from Lambda and CloudFront metrics. Opened through
# the console with the owner's login; never shared publicly.
#
# Cost: dashboards are $3 a month beyond the account's three free ones. Each view runs the
# Logs Insights queries over the 7-day log group ($0.0063 per GB scanned in eu-central-1),
# a fraction of a cent.

locals {
  # Admitted questions: each one counts towards the daily cap, even if it fails or is cancelled.
  questions_filter = "filter @message like /ask (status=|cancelled|failed)/"
  log_source       = "SOURCE '${aws_cloudwatch_log_group.api.name}'"
}

resource "aws_cloudwatch_dashboard" "demo" {
  count          = var.image_tag == "" ? 0 : 1
  dashboard_name = "${local.name}-demo"

  dashboard_body = jsonencode({
    start          = "-P7D"
    periodOverride = "inherit"
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 3
        properties = {
          markdown = join("\n", [
            "## Ρώτα το: live demo",
            "Daily cap: **${var.daily_questions} questions per UTC day** for the whole site (resets at 02:00 Greek time in winter, 03:00 in summer). Costs below are estimates from the log lines; Cost Explorer has the real figure. No question text is ever logged.",
            "[Site](https://${var.domain_name})",
          ])
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 3
        width  = 12
        height = 6
        properties = {
          title  = "Questions per UTC day (compare with the cap)"
          region = var.region
          view   = "bar"
          query  = "${local.log_source} | ${local.questions_filter} | stats count(*) as questions by bin(1d)"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 3
        width  = 12
        height = 6
        properties = {
          title  = "Answers by status: count, estimated cost, seconds to the full answer"
          region = var.region
          view   = "table"
          query  = "${local.log_source} | filter @message like /ask status=/ | parse @message /ask status=(?<status>\\S+) cost_usd=(?<cost>[\\d.]+) total_s=(?<total>[\\d.]+)/ | stats count(*) as questions, sum(cost) as est_cost_usd, pct(total, 50) as p50_s, pct(total, 95) as p95_s by status"
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 9
        width  = 12
        height = 6
        properties = {
          title  = "Questions per hour (UTC)"
          region = var.region
          view   = "bar"
          query  = "${local.log_source} | ${local.questions_filter} | stats count(*) as questions by bin(1h)"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 9
        width  = 12
        height = 6
        properties = {
          title  = "Feedback votes (helpful, reason)"
          region = var.region
          view   = "table"
          query  = "${local.log_source} | filter @message like /feedback helpful=/ | parse @message /feedback helpful=(?<helpful>\\S+) reason=(?<reason>\\S+)/ | stats count(*) as votes by helpful, reason"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 15
        width  = 12
        height = 6
        properties = {
          title  = "API responses per hour"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 3600
          metrics = [
            ["AWS/Lambda", "UrlRequestCount", "FunctionName", aws_lambda_function.api[0].function_name, { label = "All API requests (questions and votes)" }],
            [".", "Url4xxCount", ".", ".", { label = "4xx: limit refusals, cancellations, bad requests" }],
            [".", "Url5xxCount", ".", ".", { label = "5xx: busy or provider errors" }],
            [".", "Errors", ".", ".", { label = "Lambda errors" }],
            [".", "Throttles", ".", ".", { label = "Lambda throttles" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 15
        width  = 12
        height = 6
        properties = {
          title  = "CloudFront requests per hour (every file, not visitors)"
          region = "us-east-1"
          view   = "timeSeries"
          stat   = "Sum"
          period = 3600
          metrics = [
            ["AWS/CloudFront", "Requests", "Region", "Global", "DistributionId", aws_cloudfront_distribution.site[0].id, { label = "Requests" }],
          ]
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 21
        width  = 24
        height = 6
        properties = {
          title  = "Recent failures and cancellations (error type only)"
          region = var.region
          view   = "table"
          query  = "${local.log_source} | filter @message like /ask (failed|cancelled)|limits failed|feedback record failed|feedback failed|warm-up skipped/ | fields @timestamp, @message | sort @timestamp desc | limit 20"
        }
      },
    ]
  })
}
