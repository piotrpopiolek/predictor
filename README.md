# Predictor

A private operator console for live football. One process mirrors an upstream data feed into PostgreSQL. A separate process reads that database and asks Gemini for a next-goal decision. The operator places the bet. There is no bookmaker integration.

**Version:** `0.1.0`

## Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.14 |
| HTTP API | FastAPI 0.141.1, uvicorn 0.52.4, Pydantic 2.13.5 |
| Database | PostgreSQL 17, SQLAlchemy 2.0.52, Alembic 1.20.0, psycopg 3.3.5 |
| External HTTP | httpx 0.28.1, tenacity 9.1.4 |
| LLM | `google-genai` 2.31.0, model `gemini-2.5-flash` |
| Telemetry | OpenTelemetry 1.45.1 (API, SDK, OTLP exporter; FastAPI, httpx, logging, SQLAlchemy) |
| Dashboards | Prometheus 3.4, Alertmanager 0.28, Loki 3.5, Tempo 2.8, Grafana 11.6 |
| Quality | ruff 0.16, black 26.10, **mypy strict**, pytest 9.1 (80% branch coverage gate) |
| Packaging | Docker (`python:3.14-slim-bookworm`), Compose, GitHub Actions |

There is no Redis and no Celery. The ETL queue is a Postgres table. A single worker holds an advisory lock, so only one process writes to the vendor API.

The UI is server-rendered HTML. There is no separate frontend.

## Three processes

| Process | Command | What it does |
|---------|---------|--------------|
| Worker | `python -m predictor.worker` | Pulls the upstream feed on a priority schedule. Owns that quota. |
| Status | FastAPI via uvicorn | Reads Postgres only. Live board, bets, advisor report. Does not call upstream. |
| Advisor | `python -m predictor.advisor` | Reads candidates, calls Gemini, writes a decision. Does not spend the upstream quota. |

`make up` starts the worker and the status API. The advisor is a Compose profile (`advisor`) and stays off until it is started explicitly. It also refuses to boot without an API key and token prices.

## LLM integration

One provider. Settings accept only `google`. The default model is `gemini-2.5-flash`.

Each cycle is a single `generate_content` call, not a chat session:

- `temperature=0`
- response forced to `application/json`
- a JSON Schema sent as `response_json_schema`
- the application still parses the body and checks it with Pydantic (`extra="forbid"`), then applies coupon rules
- a bad body or a broken rule rejects the whole cycle and does not place a bet

The vendor SDK retries HTTP 408, 429 and 5xx inside that one request, up to 5 attempts. The application does not retry the cycle. There is no second model and no fallback provider.

The cycle prompt is versioned in code (`p4`). Match context is appended from Postgres (score, cards, stats, odds). That is not retrieval: there is no vector index and no embeddings.

Cost is a daily budget in PLN (default 10). The sandbox may spend 70% of it. Prices per million tokens are required configuration, not hardcoded. A Postgres advisory lock covers the spend check and the call, so two cycles cannot both pass the budget.

Traces go to Langfuse through a small OTLP exporter and an outbox (up to 5 attempts). Telegram uses a separate outbox. A missing key skips the export and does not undo the decision. Langfuse stores the trace. It does not score the answer.

## Upstream ingestion

The worker is the only caller of the upstream HTTP API. Retries honour `Retry-After`, otherwise exponential jitter, up to 5 attempts. HTTP 401 and 403 stop the client.

The configured quota defaults to 75 000 requests a day and 450 a minute, with a safety buffer.

## Quality gate

On every push and pull request, GitHub Actions runs `make test-ci`:

1. `ruff check`
2. `black --check`
3. `mypy` (strict)
4. `alembic upgrade head` against PostgreSQL 17
5. `pytest` with `--cov-fail-under=80` and branch coverage
6. `promtool` on the alert rules
7. a backup restore test
8. a production image build (`Dockerfile.prod`)

A second job runs `pip-audit` on the production dependencies. Tests that touch the model use a fake client and do not call Gemini.

## Run locally

Requires Docker, and GNU Make (on Windows, Git Bash or the PowerShell script `scripts/test-ci.ps1`).

```bash
cp .env.example .env
make up
```

Postgres is published on `127.0.0.1:15432`. The status API listens on `127.0.0.1:8000`.

Observability (Prometheus, Grafana, Loki, Tempo, Alertmanager), bound to localhost:

```bash
make up-obs
```

The advisor profile needs `LLM_API_KEY`, `LLM_PRICE_INPUT_PLN_PER_MTOK` and `LLM_PRICE_OUTPUT_PLN_PER_MTOK` in `.env`.
