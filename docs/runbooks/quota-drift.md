# Quota burn drift

## Symptoms
`PredictorQuotaDrift`: remaining dropped by more than 10% of the daily plan in 5 minutes.

## Diagnose
Scheduler logs and `predictor_quota_remaining` vs `predictor_quota_plan`. Unexpected paging or a stuck retry loop.

## Action
Confirm remaining from `/status`. Do not raise the plan in code. Let live keep priority; backfill waits.

## Resolved
Burn rate returns under 10% of plan per 5 minutes.
