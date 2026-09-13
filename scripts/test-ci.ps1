$ErrorActionPreference = "Stop"

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    docker compose -f docker-compose.yml @Args
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $Args"
    }
}

Invoke-Compose build
Invoke-Compose up -d postgres --wait
Invoke-Compose run --rm --no-deps worker python -m ruff check src tests
Invoke-Compose run --rm --no-deps worker python -m black --check src tests
Invoke-Compose run --rm --no-deps worker python -m mypy
Invoke-Compose run --rm --no-deps worker alembic upgrade head
Invoke-Compose run --rm --no-deps worker python -m pytest

docker run --rm -w /work -v "${PWD}/deploy/prometheus:/work:ro" --entrypoint /bin/promtool prom/prometheus:v3.4.1 check rules /work/recording_rules.yml /work/alerts.yml
if ($LASTEXITCODE -ne 0) { throw "promtool check rules failed" }
docker run --rm -w /work -v "${PWD}/deploy/prometheus:/work:ro" --entrypoint /bin/promtool prom/prometheus:v3.4.1 test rules /work/tests/alerts.test.yml
if ($LASTEXITCODE -ne 0) { throw "promtool test rules failed" }

docker compose -f docker-compose.yml cp scripts/backup/test-restore.sh postgres:/tmp/test-restore.sh
if ($LASTEXITCODE -ne 0) { throw "copy restore script failed" }
docker compose -f docker-compose.yml exec -T postgres bash /tmp/test-restore.sh
if ($LASTEXITCODE -ne 0) { throw "backup restore test failed" }

docker build -f Dockerfile.prod -t predictor:v0.1.0 -t predictor:test-prod .
if ($LASTEXITCODE -ne 0) {
    throw "Dockerfile.prod build failed"
}

Write-Host "test-ci passed"
