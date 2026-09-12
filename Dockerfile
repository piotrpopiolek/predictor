FROM python:3.13-slim-bookworm

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY tests ./tests
COPY alembic ./alembic

RUN pip install --upgrade pip \
    && pip install -e ".[dev]" \
    && chown -R app:app /app

USER app

CMD ["python", "-m", "predictor.worker"]
