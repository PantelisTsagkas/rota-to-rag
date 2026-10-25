# Account guardrails

Account-wide AWS Budgets alarm: 40 USD per month (about 30 GBP), with email alerts at 50% and 80% of actual spend and at 100% of forecast spend. It covers every service in the account, not only this project.

**Never `terraform destroy` this root.** It has its own state, separate from the project's infrastructure, so tearing the project down keeps the alarm. This is the one budget meant to outlive every project; don't add a second long-lived one. A project may carry its own short-lived budget (sagemaker-incident-lab has `incident-lab-monthly`), which goes away with that project's `terraform destroy`. Budgets without actions are free (checked 2026-10-14 on the AWS Budgets pricing page).

```bash
cd terraform/account
cp terraform.tfvars.example terraform.tfvars   # set alert_email
terraform init
terraform plan
terraform apply
```

`terraform.tfvars` and the state file are git-ignored.

History: written 2026-09-24 in flight-delay-mlops (now on hold) and moved here 2026-10-14, before it was ever applied.
