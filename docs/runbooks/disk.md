# Disk usage

## Symptoms
Warning at 50/60/70% root used; critical at 80/90%. Recording rule `instance:node_disk_root_used_ratio` excludes tmpfs/overlay.

## Diagnose
`df -h` on the host (VPS) or Docker Desktop VM. Check Prometheus/Loki/Tempo volume growth vs `predictor_pgdata`.

## Action
Telemetry retention: metrics 30d, logs 14d, traces 7d. Never delete `predictor_pgdata`. Shrink observability volumes first.

## Resolved
Used ratio stays below the fired threshold for 30 minutes.
