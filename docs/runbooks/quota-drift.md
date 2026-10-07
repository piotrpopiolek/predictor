# Quota burn drift

## Symptoms
`PredictorQuotaDrift`: remaining dropped by more than 10% of the daily plan in 5 minutes.

## Diagnose
Scheduler logs and `predictor_quota_remaining` vs `predictor_quota_plan`. Unexpected paging or a stuck retry loop. Daily quota resets at **00:00 UTC** (API-Football dashboard: "Quota reset in (00h00 UTC time)"), not midnight Europe/Warsaw.

## Action
Confirm remaining from `/status` or Grafana **Quota usage**. Do not raise the plan in code. Let live keep priority; backfill waits.

## Resolved
Burn rate returns under 10% of plan per 5 minutes.
