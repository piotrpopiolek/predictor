# GNU Make (Git Bash / WSL / Linux).
# Windows PowerShell equivalents: .\scripts\test-ci.ps1
#   up:    docker compose -f docker-compose.yml up -d
#   down:  docker compose -f docker-compose.yml down
#   logs:  docker compose -f docker-compose.yml logs -f worker status
#   postgres host port: 127.0.0.1:15432 (avoids Windows Hyper-V excluded ranges)
#   obs:   docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability up -d

PREDICTOR_IMAGE_TAG ?= v0.1.0
PROMTOOL_IMAGE := prom/prometheus:v3.4.1
COMPOSE := docker compose -f docker-compose.yml
COMPOSE_OBS := docker compose -f docker-compose.yml -f docker-compose.observability.yml
COMPOSE_PROD := docker compose -f docker-compose.prod.yml -f docker-compose.observability.yml
WORKER := $(COMPOSE) run --rm --no-deps worker

.PHONY: up down logs migrate pytest pytest-unit test-ci mutmut stability up-obs up-prod down-prod build-prod promtool backup-restore-test

up:
	$(COMPOSE) up -d --build

up-obs:
	$(COMPOSE_OBS) --profile observability up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f worker status

migrate:
	$(COMPOSE) up -d postgres --wait
	$(WORKER) alembic upgrade head

pytest: migrate
	$(WORKER) python -m pytest

pytest-unit:
	$(WORKER) python -m pytest -m unit -n auto --no-cov

promtool:
	docker run --rm -w /work --volume "$(CURDIR)/deploy/prometheus:/work:ro" --entrypoint /bin/promtool $(PROMTOOL_IMAGE) check rules /work/recording_rules.yml /work/alerts.yml
	docker run --rm -w /work --volume "$(CURDIR)/deploy/prometheus:/work:ro" --entrypoint /bin/promtool $(PROMTOOL_IMAGE) test rules /work/tests/alerts.test.yml

backup-restore-test:
	$(COMPOSE) up -d postgres --wait
	$(COMPOSE) cp scripts/backup/test-restore.sh postgres:/tmp/test-restore.sh
	$(COMPOSE) exec -T postgres bash /tmp/test-restore.sh

# Local only. Two model passes on a frozen set. Not part of test-ci.
stability:
	$(COMPOSE) --profile advisor run --rm --no-deps advisor python -m predictor.advisor.stability

# Local only. Hunts surviving mutants in coupon logic. Not part of test-ci.
mutmut:
	mkdir -p .mutmut-out
	$(COMPOSE) run --rm --no-deps -T --user root --entrypoint bash \
	  -v "$(CURDIR)/.mutmut-diagnostic.sh:/tmp/run.sh:ro" \
	  -v "$(CURDIR)/.mutmut-out:/mutmut-out" \
	  worker /tmp/run.sh

test-ci:
	$(COMPOSE) build
	$(COMPOSE) up -d postgres --wait
	$(WORKER) python -m ruff check src tests
	$(WORKER) python -m black --check src tests
	$(WORKER) python -m mypy
	$(WORKER) alembic upgrade head
	$(WORKER) python -m pytest
	$(MAKE) promtool
	$(MAKE) backup-restore-test
	docker build -f Dockerfile.prod -t predictor:$(PREDICTOR_IMAGE_TAG) -t predictor:test-prod .

up-prod:
	$(COMPOSE_PROD) --profile observability up -d --build

down-prod:
	$(COMPOSE_PROD) --profile observability down

build-prod:
	docker build -f Dockerfile.prod -t predictor:$(PREDICTOR_IMAGE_TAG) .
