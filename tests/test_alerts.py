from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "deploy" / "prometheus" / "alerts.yml"
ALERT_TESTS = ROOT / "deploy" / "prometheus" / "tests" / "alerts.test.yml"
RECORDING = ROOT / "deploy" / "prometheus" / "recording_rules.yml"
OBS_COMPOSE = ROOT / "docker-compose.observability.yml"
PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
DEV_COMPOSE = ROOT / "docker-compose.yml"
DASH_DIR = ROOT / "deploy" / "grafana" / "provisioning" / "dashboards" / "json"


def _alerts() -> list[dict[str, object]]:
    payload = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    found: list[dict[str, object]] = []
    for group in payload["groups"]:
        for rule in group["rules"]:
            if "alert" in rule:
                found.append(rule)
    return found


def test_every_alert_has_runbook_and_operator_fields() -> None:
    for rule in _alerts():
        annotations = rule["annotations"]
        labels = rule["labels"]
        assert "runbook_url" in annotations, rule["alert"]
        assert str(annotations["runbook_url"]).startswith("docs/runbooks/")
        assert Path(ROOT / str(annotations["runbook_url"])).is_file()
        assert "summary" in annotations
        assert "description" in annotations
        assert labels["severity"] in {"warning", "critical"}


def test_live_stale_compares_age_to_two_poll_intervals() -> None:
    rule = next(r for r in _alerts() if r["alert"] == "PredictorLiveStale")
    expr = str(rule["expr"])
    assert "predictor_live_snapshot_age_seconds" in expr
    assert "2 * predictor_live_poll_interval_seconds" in expr
    assert "> 120" not in expr


def test_every_critical_alert_has_promtool_case() -> None:
    tested = set()
    text = ALERT_TESTS.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("alertname:"):
            tested.add(line.split(":", 1)[1].strip())
    missing = [
        str(rule["alert"])
        for rule in _alerts()
        if rule["labels"]["severity"] == "critical" and rule["alert"] not in tested
    ]
    assert missing == []


def test_recording_rule_excludes_tmpfs_overlay() -> None:
    text = RECORDING.read_text(encoding="utf-8")
    assert "instance:node_disk_root_used_ratio" in text
    assert "tmpfs" in text
    assert "overlay" in text


def test_provisioned_grafana_dashboards() -> None:
    files = sorted(DASH_DIR.glob("*.json"))
    uids = set()
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        uids.add(data["uid"])
        assert data["title"]
        assert data["panels"]
        if data["uid"] == "predictor-api-live":
            exprs = [
                str(target.get("expr", ""))
                for panel in data["panels"]
                for target in panel.get("targets", [])
            ]
            joined = " ".join(exprs)
            assert "predictor_live_snapshot_age_seconds" in joined
            assert "predictor_live_poll_interval_seconds" in joined
            assert "time() - predictor_live_last_snapshot_unixtime" not in joined
        if data["uid"] == "predictor-backfill":
            age = next(p for p in data["panels"] if p["id"] == 2)
            steps = age["fieldConfig"]["defaults"]["thresholds"]["steps"]
            assert steps[-1]["value"] == 86400
        if data["uid"] == "predictor-quota":
            exprs = [
                str(target.get("expr", ""))
                for panel in data["panels"]
                for target in panel.get("targets", [])
            ]
            joined = " ".join(exprs)
            assert "predictor_quota_used" in joined
            assert "deriv(predictor_quota_remaining" in joined
            assert "predictor_quota_seconds_until_reset" in joined
            assert "time() % 86400" not in joined
            percent = next(
                str(t.get("expr", ""))
                for p in data["panels"]
                if p["id"] == 3
                for t in p.get("targets", [])
            )
            assert percent.startswith("(predictor_quota_used / predictor_quota_plan)")
        if data["uid"] == "predictor-etl-queue":
            exprs = [
                str(target.get("expr", ""))
                for panel in data["panels"]
                for target in panel.get("targets", [])
            ]
            joined = " ".join(exprs)
            assert 'sum(predictor_etl_queue{status="pending"})' in joined
            assert 'topk(15, predictor_etl_queue{status="pending"})' in joined
            assert "predictor_oldest_pending_age_seconds" in joined
            assert "predictor_writer_lock" in joined
            assert (
                'sum(deriv(predictor_etl_queue{status="pending"}[15m])) * 60'
                in joined
            )
            assert "predictor_etl_tasks{" not in joined
    assert uids == {
        "predictor-infra",
        "predictor-api-live",
        "predictor-backfill",
        "predictor-quota",
        "predictor-etl-queue",
    }


def test_observability_host_ports_bind_loopback() -> None:
    text = OBS_COMPOSE.read_text(encoding="utf-8")
    assert "0.0.0.0:" not in text
    assert "OBSERVABILITY_BIND_ADDR:-127.0.0.1" in text
    assert "profiles:" in text
    assert "node-exporter" in text
    assert "cadvisor" in text
    assert "postgres-exporter" in text


def test_dev_compose_starts_without_observability_profile() -> None:
    text = DEV_COMPOSE.read_text(encoding="utf-8")
    assert "otel-collector" not in text
    assert "grafana" not in text
    assert "predictor_pgdata" not in text


def test_prod_compose_has_backup_healthcheck_and_image_tag() -> None:
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    assert "backup:" in text
    assert "predictor_pgdumps" in text
    assert "PREDICTOR_IMAGE_TAG" in text
    assert "./src:" not in text
    assert "restart: unless-stopped" in text
    assert "healthcheck:" in text
    assert "--lock-wait-timeout" not in text
