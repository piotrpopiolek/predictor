# Production image tag (vX.Y.Z)

Build and run the same immutable tag locally (first month) and on the VPS. Compose production does not bind-mount application source.

```
docker build -f Dockerfile.prod -t predictor:v0.1.0 .
# Git Bash / Makefile
PREDICTOR_IMAGE_TAG=v0.1.0 make build-prod up-prod
```

Checkout a clean git tag on the VPS. Observability: `docker compose -f docker-compose.prod.yml -f docker-compose.observability.yml --profile observability up -d`.

Access Grafana via `ssh -L 3000:127.0.0.1:3000` (ports bind loopback only).
