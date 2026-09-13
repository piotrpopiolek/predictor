# node_exporter down

## Symptoms
`PredictorNodeExporterDown`. Disk alerts will not fire.

## Diagnose
`docker compose logs node-exporter`. On Docker Desktop, host metrics are the Linux VM, not Windows C:.

## Action
Restart `node-exporter`. Keep `/` bind for `--path.rootfs=/host`. Do not add `rslave` on Docker Desktop (`path / is not a shared or slave mount`).

## Resolved
`up{job="node"} == 1` and `instance:node_disk_root_used_ratio` has samples.
