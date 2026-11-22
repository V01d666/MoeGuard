"""Local settings operations for the headless patrol candidate."""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

import cv2
from PySide6.QtCore import QCoreApplication

from moeguard.app import MoeGuardApp
from moeguard.config import PATROL_CONSENT_VERSION, AppConfig
from moeguard.security.camera import CameraCapture
from moeguard.storage.db import Database
from moeguard.storage.evidence_store import EvidenceStore
from moeguard.storage.owner_profile import OwnerProfileStore
from moeguard.utils.crypto import CryptoBox
from moeguard.utils.paths import CRYPTO_KEY_PATH, DB_PATH, EVIDENCE_DIR, OWNER_EMBEDDINGS_PATH

_FIELDS = {
    "auto_patrol_enabled": ("presence", bool, 0, 1),
    "idle_threshold_sec": ("presence", int, 30, 600),
    "patrol_interval_sec": ("presence", float, 0.5, 5.0),
    "device_index": ("camera", int, 0, 32),
    "face_match_threshold": ("security", float, 0.1, 0.9),
    "stranger_cooldown_sec": ("security", int, 5, 300),
    "video_clip_sec": ("evidence", int, 5, 60),
    "retention_days": ("evidence", int, 1, 90),
    "blur_stranger_faces": ("evidence", bool, 0, 1),
    "motion_recording_enabled": ("evidence", bool, 0, 1),
}
_EVENT = re.compile(r"[0-9]{8}_[0-9]{6}(?:_[a-f0-9-]{1,40})?\Z")


def _consented(config: AppConfig) -> bool:
    consent = config.consent
    return (consent.patrol_consent_granted
            and consent.patrol_consent_version == PATROL_CONSENT_VERSION)


def _registered() -> bool:
    if not CRYPTO_KEY_PATH.is_file() or not OWNER_EMBEDDINGS_PATH.is_file():
        return False
    store = OwnerProfileStore(OWNER_EMBEDDINGS_PATH, CryptoBox(CRYPTO_KEY_PATH))
    return bool(store.load(quarantine=False))


def _view(config: AppConfig) -> dict:
    return {"consented": _consented(config), "registered": _registered(),
            "config": {name: getattr(getattr(config, group), name)
                       for name, (group, *_limits) in _FIELDS.items()}}


def _configure(config: AppConfig, values: object) -> AppConfig:
    if not isinstance(values, dict) or not values or values.keys() - _FIELDS.keys():
        raise ValueError("invalid_settings")
    for name, value in values.items():
        group, kind, low, high = _FIELDS[name]
        if name == "device_index":
            high = max(high, config.camera.device_index)
        kind_ok = type(value) is kind or (kind is float and type(value) is int)
        if not kind_ok or not low <= value <= high:
            raise ValueError("invalid_settings")
        config = replace(config, **{group: replace(getattr(config, group), **{name: value})})
    if config.presence.auto_patrol_enabled and not (
        _consented(config) and config.evidence.show_patrol_banner and _registered()
    ):
        raise ValueError("setup_required")
    return config


def _safe_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not path.is_junction()


def _evidence(op: str, request: dict, config: AppConfig) -> dict:
    if EVIDENCE_DIR.exists() and not _safe_directory(EVIDENCE_DIR):
        raise ValueError("evidence_unavailable")
    if op == "evidence" and not EVIDENCE_DIR.exists():
        return {"events": []}
    store = EvidenceStore(EVIDENCE_DIR, config.evidence.retention_days)
    entries = {p.name: p for p in reversed(store.list_events())
               if _EVENT.fullmatch(p.name) and _safe_directory(p)}
    if op == "evidence":
        return {"events": list(entries)[:200]}
    event = request.get("event")
    if event is not None and (not isinstance(event, str) or not _EVENT.fullmatch(event)):
        raise ValueError("evidence_unavailable")
    if op == "evidence_open":
        path = EVIDENCE_DIR if event is None else entries.get(event)
        if path is None:
            raise ValueError("evidence_unavailable")
        # The platform opener is native. Return only an allowlisted leaf name.
        return {"open": event}
    if request.get("confirmed") is not True:
        raise ValueError("confirmation_required")
    pending = EVIDENCE_DIR / ".pending"
    if op == "evidence_delete_all" and (
        any(p.name not in entries for p in store.list_events())
        or pending.is_symlink() or pending.is_junction()
    ):
        raise ValueError("evidence_unavailable")
    database = Database(DB_PATH)
    database.connect()
    try:
        if op == "evidence_delete_all":
            # Reuse the old deletion boundary, including its pending cleanup.
            store.delete_all()
            database.delete_all_incidents()
        elif event in entries:
            path = entries[event]
            store.delete_event(path)
            database.delete_incidents_by_evidence([str(path)])
        else:
            raise ValueError("evidence_unavailable")
    finally:
        database.close()
    return _evidence("evidence", {}, config)


def settings() -> int:
    try:
        request = json.loads(sys.stdin.buffer.readline(8192))
        if not isinstance(request, dict):
            raise ValueError("invalid_settings")
        op = request.get("op")
        if op in ("consent", "withdraw") and request.get("confirmed") is not True:
            raise ValueError("confirmation_required")
        config = AppConfig.load()
        if op == "view":
            result = _view(config)
        elif op in ("consent", "save"):
            if op == "save":
                config = _configure(config, request.get("config"))
            elif not _consented(config):
                config = replace(
                    config,
                    consent=replace(config.consent, patrol_consent_granted=True,
                                    patrol_consent_version=PATROL_CONSENT_VERSION),
                    presence=replace(config.presence, auto_patrol_enabled=False),
                    evidence=replace(config.evidence, show_patrol_banner=True),
                )
            if not AppConfig.save(config):
                raise OSError("settings save failed")
            result = _view(config)
        elif op == "withdraw":
            qt = QCoreApplication([])
            controller = MoeGuardApp()
            controller._config = config
            try:
                controller._load_patrol_runtime()
                result = _view(controller.config) if controller._withdraw_patrol_consent() else {
                    "error": "withdraw_incomplete",
                }
            finally:
                if controller._db is not None:
                    controller._db.close()
                del qt
        elif op in ("evidence", "evidence_open", "evidence_delete", "evidence_delete_all"):
            result = _evidence(op, request, config)
        else:
            raise ValueError("invalid_settings")
    except ValueError as exc:
        known = {"invalid_settings", "setup_required", "evidence_unavailable",
                 "confirmation_required"}
        result = {"error": str(exc) if str(exc) in known else "settings_failed"}
    except Exception:
        logging.exception("Local patrol settings operation failed")
        result = {"error": "settings_failed"}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


def enroll() -> int:
    """Take one preview, release the camera, then wait for explicit save or EOF."""
    if sys.stdin.buffer.readline(16) != b"preview\n":
        return 2
    config = AppConfig.load()
    if not _consented(config) or not config.evidence.show_patrol_banner:
        print('{"error":"setup_required"}', flush=True)
        return 0
    qt = QCoreApplication([])
    camera = CameraCapture()
    try:
        try:
            if not camera.start(device_index=config.camera.device_index, interval_ms=250):
                raise ValueError("camera_unavailable")
            until = time.monotonic() + 5
            frame = camera.grab()
            while frame is None and time.monotonic() < until and camera.is_opened():
                time.sleep(0.05)
                frame = camera.grab()
        finally:
            camera.stop()
        if frame is None:
            raise ValueError("camera_unavailable")
        height, width = frame.shape[:2]
        factor = min(1.0, 320 / max(height, width))
        small = cv2.resize(frame, (max(1, int(width * factor)), max(1, int(height * factor))))
        ok, jpg = cv2.imencode(".jpg", small)
        if not ok or jpg.size > 96 * 1024:
            raise ValueError("camera_unavailable")
        print(json.dumps({"preview": base64.b64encode(jpg.tobytes()).decode("ascii")}), flush=True)
        if sys.stdin.buffer.readline(16) != b"save\n":
            return 0
        controller = MoeGuardApp()
        controller._config = config
        try:
            controller._load_patrol_runtime()
            owner, store = controller._owner, controller._owner_store
            assert owner is not None and store is not None
            if not owner.register(frame):
                raise ValueError("face_not_found")
            store.save(owner.embeddings())
            print('{"registered":true}', flush=True)
        finally:
            if controller._db is not None:
                controller._db.close()
    except ValueError as exc:
        code = str(exc) if str(exc) in {"camera_unavailable", "face_not_found"} else (
            "settings_failed"
        )
        print(json.dumps({"error": code}), flush=True)
    except Exception:
        logging.exception("Local owner registration failed")
        print('{"error":"settings_failed"}', flush=True)
    finally:
        del qt
    return 0
