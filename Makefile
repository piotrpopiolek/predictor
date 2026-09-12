# GNU Make (Git Bash / WSL / Linux).
# Windows PowerShell equivalents: .\scripts\test-ci.ps1
#   up:    docker compose -f docker-compose.yml up -d
#   down:  docker compose -f docker-compose.yml down
#   logs:  docker compose -f docker-compose.yml logs -f worker status
#   postgres host port: 127.0.0.1:55432 (avoids Windows 5432 exclusions)

COMPOSE := docker compose -f docker-compose.yml
COMPOSE_PROD := docker compose -f docker-compose.prod.yml
WORKER := $(COMPOSE) run --rm --no-deps worker

.PHONY: up down logs migrate pytest test-ci up-prod down-prod build-prod

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f worker status

migrate:
	$(COMPOSE) up -d postgres --wait
	$(WORKER) alembic upgrade head

pytest: migrate
	$(WORKER) python -m pytest

test-ci:
	$(COMPOSE) build
	$(COMPOSE) up -d postgres --wait
	$(WORKER) python -m ruff check src tests
	$(WORKER) python -m black --check src tests
	$(WORKER) python -m mypy
	$(WORKER) alembic upgrade head
	$(WORKER) python -m pytest
	docker build -f Dockerfile.prod -t predictor:test-prod .

up-prod:
	$(COMPOSE_PROD) up -d --build

down-prod:
	$(COMPOSE_PROD) down
