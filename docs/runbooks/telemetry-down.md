# Observability stack down

## Symptoms
Collector, Prometheus, Loki, or Tempo scrape `up == 0`.

## Diagnose
`docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability logs otel-collector loki tempo`

## Action
Restart the failed observability service. **Do not** restart a healthy worker to fix telemetry. Domain ingest must continue.

## Resolved
`up` is 1 for the component and new traces/logs appear after recovery.
