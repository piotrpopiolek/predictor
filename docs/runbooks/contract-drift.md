# Permanent error and contract drift

## Symptoms
`PredictorPermanentError` or `PredictorContractDrift`. Log event `contract_drift` lists unknown field names (not values).

## Diagnose
`etl_tasks` with `permanent_error`. JSON logs `fields` on `contract_drift`. Do not expect secrets in those logs.

## Action
Inspect the endpoint template. Keep extra fields on the model (`extra=allow`). Patch mapping if the vendor added a required column.

## Resolved
No new `permanent_error` and no `contract_drift` increase for 15 minutes.
