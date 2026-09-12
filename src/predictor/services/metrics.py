"""Prometheus text for the private status process. Finite labels only."""

from __future__ import annotations

import secrets

from predictor.constants import ETL_STATUSES


def metrics_token_ok(authorization: str | None, token: str) -> bool:
    if authorization is None or not authorization.startswith("Bearer "):
        return False
    got = authorization.removeprefix("Bearer ")
    return secrets.compare_digest(got, token)


def render_metrics(
    *,
    environment: str,
    lock_held: bool,
    task_counts: dict[str, int],
) -> str:
    labels = f'service="status",environment="{environment}"'
    lines = [
        "# HELP predictor_writer_lock Writer advisory lock held (1) or not (0).",
        "# TYPE predictor_writer_lock gauge",
        f"predictor_writer_lock{{{labels}}} {1 if lock_held else 0}",
        "# HELP predictor_etl_tasks Number of etl_tasks by status.",
        "# TYPE predictor_etl_tasks gauge",
    ]
    for status in sorted(ETL_STATUSES):
        count = task_counts.get(status, 0)
        lines.append(f'predictor_etl_tasks{{{labels},status="{status}"}} {count}')
    return "\n".join(lines) + "\n"
