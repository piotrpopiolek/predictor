from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONCE = ROOT / "scripts" / "backup" / "backup-once.sh"
LOOP = ROOT / "scripts" / "backup" / "backup-loop.sh"
RESTORE = ROOT / "scripts" / "backup" / "test-restore.sh"


def test_backup_dump_does_not_wait_for_advisory_lock() -> None:
    once = ONCE.read_text(encoding="utf-8")
    loop = LOOP.read_text(encoding="utf-8")
    restore = RESTORE.read_text(encoding="utf-8")
    for text in (once, loop, restore):
        assert "pg_try_advisory_lock" not in text
        assert "pg_advisory_lock" not in text
        assert "steal" not in text
    assert "--lock-wait-timeout=0" in once
    assert "--lock-wait-timeout=0" in restore
    assert "predictor_prod" in once
    assert "/backups" in once
    assert "predictor_pgdata" not in once


def test_restore_targets_clone_not_prod() -> None:
    restore = RESTORE.read_text(encoding="utf-8")
    assert "predictor_restore_test" in restore
    assert "CREATE DATABASE predictor_restore_test" in restore
    assert "DROP DATABASE IF EXISTS predictor_restore_test" in restore
    assert "CREATE DATABASE predictor_prod" not in restore
    assert "DROP DATABASE IF EXISTS predictor_prod" not in restore
    assert "predictor_dev" not in restore
