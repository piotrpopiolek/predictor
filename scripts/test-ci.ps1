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
docker build -f Dockerfile.prod -t predictor:test-prod .
if ($LASTEXITCODE -ne 0) {
    throw "Dockerfile.prod build failed"
}

Write-Host "test-ci passed"
