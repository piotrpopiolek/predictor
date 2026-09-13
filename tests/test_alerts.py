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


def test_three_provisioned_grafana_dashboards() -> None:
    files = sorted(DASH_DIR.glob("*.json"))
    uids = set()
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        uids.add(data["uid"])
        assert data["title"]
        assert data["panels"]
    assert uids == {"predictor-infra", "predictor-api-live", "predictor-backfill"}


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
