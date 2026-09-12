from __future__ import annotations

from pathlib import Path

from predictor.constants import WRITER_LOCK_KEY


def test_writer_lock_key_is_documented_constant() -> None:
    assert WRITER_LOCK_KEY == 739201


def test_only_lock_module_calls_try_advisory_lock() -> None:
    root = Path("src/predictor")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "pg_try_advisory_xact_lock" in text:
            offenders.append(str(path))
        if "pg_try_advisory_lock" in text and path.name != "lock.py":
            offenders.append(str(path))
    assert offenders == []


def test_api_does_not_import_football_client() -> None:
    source = Path("src/predictor/api/main.py").read_text(encoding="utf-8")
    assert "FootballClient" not in source
    assert "httpx" not in source
    assert "pg_try_advisory_lock" not in source


def test_no_vendor_api_host_in_application() -> None:
    root = Path("src")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "api-sports.io" not in text
        assert "api-football.com" not in text
