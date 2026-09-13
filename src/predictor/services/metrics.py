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
    in_play_fixtures: int = 0,
    live_last_snapshot_unixtime: float = 0,
    oldest_pending_age_seconds: float = 0,
    quota_plan: int = 0,
    quota_remaining: int = -1,
) -> str:
    labels = f'service="status",environment="{environment}"'
    lines = [
        "# HELP predictor_writer_lock Writer advisory lock held (1) or not (0).",
        "# TYPE predictor_writer_lock gauge",
        f"predictor_writer_lock{{{labels}}} {1 if lock_held else 0}",
        "# HELP predictor_heartbeat Status process scrape heartbeat.",
        "# TYPE predictor_heartbeat gauge",
        f"predictor_heartbeat{{{labels}}} 1",
        "# HELP predictor_in_play_fixtures Count of in-play fixtures.",
        "# TYPE predictor_in_play_fixtures gauge",
        f"predictor_in_play_fixtures{{{labels}}} {in_play_fixtures}",
        (
            "# HELP predictor_live_last_snapshot_unixtime "
            "Unix time of newest live odds snapshot."
        ),
        "# TYPE predictor_live_last_snapshot_unixtime gauge",
        (
            "predictor_live_last_snapshot_unixtime{"
            f"{labels}}} {live_last_snapshot_unixtime}"
        ),
        "# HELP predictor_oldest_pending_age_seconds Age of the oldest open ETL task.",
        "# TYPE predictor_oldest_pending_age_seconds gauge",
        (
            "predictor_oldest_pending_age_seconds{"
            f"{labels}}} {oldest_pending_age_seconds}"
        ),
        "# HELP predictor_quota_plan Configured daily request plan.",
        "# TYPE predictor_quota_plan gauge",
        f"predictor_quota_plan{{{labels}}} {quota_plan}",
        (
            "# HELP predictor_quota_remaining "
            "Last known remaining daily quota (-1 unknown)."
        ),
        "# TYPE predictor_quota_remaining gauge",
        f"predictor_quota_remaining{{{labels}}} {quota_remaining}",
        "# HELP predictor_etl_tasks Number of etl_tasks by status.",
        "# TYPE predictor_etl_tasks gauge",
    ]
    for status in sorted(ETL_STATUSES):
        count = task_counts.get(status, 0)
        lines.append(f'predictor_etl_tasks{{{labels},status="{status}"}} {count}')
    return "\n".join(lines) + "\n"
