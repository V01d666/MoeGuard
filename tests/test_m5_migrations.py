"""M5-A：旧本地配置与 SQLite 数据升级回归。"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

import pytest

from moeguard.config import AppConfig, PetConfig
from moeguard.storage.db import Database


def test_distribution_metadata_matches_public_windows_resource_contract() -> None:
    """公开 wheel 必须携带资源，且不得宣称未实现的平台/云端 extra。"""
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert "Operating System :: MacOS" not in project["project"]["classifiers"]
    assert "cloud" not in project["project"]["optional-dependencies"]
    assert "resources" in project["tool"]["setuptools"]["packages"]["find"]["include"]
    assert project["tool"]["setuptools"]["package-data"]["resources"] == ["**/*"]

    yunet_license = root / "resources" / "models" / "YuNet-LICENSE.txt"
    license_text = yunet_license.read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Copyright (c) 2020 Shiqi Yu" in license_text


def test_config_save_is_atomic_when_writer_fails(tmp_path: Path, monkeypatch) -> None:
    from moeguard import config as config_module

    path = tmp_path / "config.toml"
    path.write_text("[presence]\nauto_patrol_enabled = true\n", encoding="utf-8")

    def fail_dump(_data, _file) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(config_module._toml_writer, "dump", fail_dump)
    assert not AppConfig.save(AppConfig(), path)
    assert path.read_text(encoding="utf-8") == (
        "[presence]\nauto_patrol_enabled = true\n"
    )
    assert not list(tmp_path.glob(".config.toml.*"))


def test_legacy_config_loads_new_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[camera]\ndevice_index = 1\n", encoding="utf-8")

    loaded = AppConfig.load(path)
    assert loaded.camera.device_index == 1
    assert not loaded.presence.auto_patrol_enabled
    assert not loaded.consent.patrol_consent_granted


def test_legacy_dialogue_credentials_are_not_loaded_or_resaved(tmp_path: Path) -> None:
    """MVP 升级必须清空旧云凭据，且之后不再写回 config.toml。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "[dialogue]\napi_key = 'secret'\nbase_url = 'https://example.test'\n"
        "model = 'legacy-model'\n",
        encoding="utf-8",
    )

    loaded = AppConfig.load(path)
    assert not loaded.dialogue.enabled
    assert loaded.dialogue.api_key == ""
    assert loaded.dialogue.base_url == ""
    assert loaded.dialogue.model == ""
    assert AppConfig.save(loaded, path)
    assert "dialogue" not in path.read_text(encoding="utf-8")


def test_config_save_returns_success_and_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert AppConfig.save(AppConfig(), path)
    assert AppConfig.load(path) == AppConfig()


def test_managed_role_package_key_roundtrips_without_assets_path(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = AppConfig(
        pet=PetConfig(
            role_id="custom-whale",
            role_package_version=3,
            assets_dir="",
        )
    )

    assert AppConfig.save(config, path)
    loaded = AppConfig.load(path)

    assert loaded.pet.role_id == "custom-whale"
    assert loaded.pet.role_package_version == 3
    assert loaded.pet.assets_dir == ""


@pytest.mark.parametrize("invalid", [True, -1, "3"])
def test_invalid_managed_role_version_falls_back_to_bundled_mode(
    tmp_path: Path, invalid
) -> None:
    path = tmp_path / "config.toml"
    if invalid is True:
        rendered = "true"
    elif isinstance(invalid, str):
        rendered = f'"{invalid}"'
    else:
        rendered = str(invalid)
    path.write_text(
        f"[pet]\nrole_id = \"custom-whale\"\nrole_package_version = {rendered}\n",
        encoding="utf-8",
    )

    assert AppConfig.load(path).pet.role_package_version == 0


def test_legacy_database_migrates_idempotently_and_preserves_events(tmp_path: Path) -> None:
    path = tmp_path / "moeguard.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            evidence TEXT
        );
        CREATE TABLE patrol_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts REAL NOT NULL,
            end_ts REAL NOT NULL,
            incident_count INTEGER NOT NULL DEFAULT 0,
            trigger TEXT NOT NULL
        );
        INSERT INTO incidents (ts, kind, evidence) VALUES (123.0, 'stranger', 'old');
        PRAGMA user_version = 0;
        """
    )
    conn.close()

    db = Database(path)
    db.connect()
    assert db.schema_version == 4
    rows = db.incidents_since(0)
    assert len(rows) == 1 and rows[0]["evidence"] == "old"
    assert rows[0]["summary"] is None
    assert rows[0]["event_uuid"] is None
    db.bump_usage("2026-08-09", clicks=1)
    db.close()

    reopened = Database(path)
    reopened.connect()
    assert reopened.schema_version == 4
    assert len(reopened.daily_usage_since(0)) == 1
    reopened.close()


def test_missing_evidence_indices_are_recovered_on_later_maintenance(
    tmp_path: Path,
) -> None:
    """文件删除后索引写入失败，下一轮维护仍能收敛到无孤立索引。"""
    db = Database(tmp_path / "moeguard.db")
    db.connect()
    existing = tmp_path / "evidence" / "existing"
    existing.mkdir(parents=True)
    missing = tmp_path / "evidence" / "missing"
    db.log_incident(1.0, "stranger", str(existing))
    db.log_incident(2.0, "stranger", str(missing))

    assert db.delete_incidents_with_missing_evidence() == 1
    rows = db.incidents_since(0)
    assert len(rows) == 1
    assert rows[0]["evidence"] == str(existing)
    db.close()


def test_evidence_event_commit_is_atomic_and_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    """同一录制只产生一次索引和一次日统计，内部失败则两者都不保留。"""
    db = Database(tmp_path / "moeguard.db")
    db.connect()
    evidence = tmp_path / "evidence" / "event"
    evidence.mkdir(parents=True)

    incident_id, inserted = db.commit_evidence_event(
        event_uuid="a" * 32,
        ts=1.0,
        kind="stranger",
        evidence=str(evidence),
        summary="test",
        usage_date="2026-08-10",
    )
    assert inserted
    duplicate_id, duplicate_inserted = db.commit_evidence_event(
        event_uuid="a" * 32,
        ts=1.0,
        kind="stranger",
        evidence=str(evidence),
        summary="test",
        usage_date="2026-08-10",
    )
    assert (duplicate_id, duplicate_inserted) == (incident_id, False)
    assert len(db.incidents_since(0)) == 1
    assert db.daily_usage_since(0)[0]["incident_count"] == 1

    def fail_usage(*_args, **_kwargs) -> None:
        raise OSError("simulated usage failure")

    monkeypatch.setattr(db, "_bump_usage", fail_usage)
    with pytest.raises(OSError, match="simulated usage failure"):
        db.commit_evidence_event(
            event_uuid="b" * 32,
            ts=2.0,
            kind="motion",
            evidence=str(evidence),
            summary="test",
            usage_date="2026-08-10",
        )
    assert len(db.incidents_since(0)) == 1
    assert db.daily_usage_since(0)[0]["incident_count"] == 1
    db.close()
