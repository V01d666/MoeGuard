#!/usr/bin/env python3
"""M1 安防感知模块测试脚本。

验证 FaceRecognizer + OwnerProfile 的基本功能。
不依赖 PySide6，可在 VPS 上运行。
不产生真人影像（使用合成图像）。

运行方式:
    cd /root/projects/MoeGuard
    python3 tests/test_m1_security.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_face_recognizer_import() -> None:
    """FaceRecognizer 可导入、可实例化。"""
    from moeguard.config import SecurityConfig
    from moeguard.security.face import FaceRecognizer

    config = SecurityConfig()
    recognizer = FaceRecognizer(config=config)
    assert recognizer is not None
    assert not recognizer.is_loaded
    print("[PASS] FaceRecognizer import + instantiate")


def test_cosine_similarity() -> None:
    """余弦相似度计算正确。"""
    from moeguard.security.face import FaceRecognizer

    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    b = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    c = np.array([0.0, 1.0, 0.0], dtype=np.float32)

    sim_same = FaceRecognizer.cosine_similarity(a, b)
    sim_orth = FaceRecognizer.cosine_similarity(a, c)

    assert abs(sim_same - 1.0) < 1e-6, f"Expected ~1.0, got {sim_same}"
    assert abs(sim_orth) < 1e-6, f"Expected ~0.0, got {sim_orth}"
    print(f"[PASS] cosine_similarity: same={sim_same:.6f}, orthogonal={sim_orth:.6f}")


def test_owner_profile_basic() -> None:
    """OwnerProfile 基本功能（不依赖真实人脸）。"""
    from moeguard.config import SecurityConfig
    from moeguard.security.owner import OwnerProfile

    config = SecurityConfig()
    # 不传入 recognizer，仅测试数据管理逻辑
    owner = OwnerProfile.__new__(OwnerProfile)
    owner._config = config  # type: ignore[attr-defined]
    owner._recognizer = None  # type: ignore[attr-defined]
    owner._owner_embeddings = []  # type: ignore[attr-defined]

    assert not owner.is_registered()

    # 模拟注册
    fake_embedding = np.random.randn(512).astype(np.float32)
    owner._owner_embeddings = [fake_embedding]  # type: ignore[attr-defined]
    assert owner.is_registered()

    # 导出/加载
    exported = owner.embeddings()
    assert len(exported) == 1
    assert np.array_equal(exported[0], fake_embedding)

    # 清空
    owner._owner_embeddings = []  # type: ignore[attr-defined]
    assert not owner.is_registered()
    print("[PASS] OwnerProfile basic (register/export/clear)")


def test_owner_profile_store() -> None:
    """OwnerProfileStore 加密存储往返。"""
    from moeguard.storage.owner_profile import OwnerProfileStore
    from moeguard.utils.crypto import CryptoBox

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        key_path = tmp_path / "key"
        store_path = tmp_path / "embeddings.enc"

        crypto = CryptoBox(key_path)
        store = OwnerProfileStore(store_path, crypto)

        # 保存
        embeddings = [
            np.random.randn(512).astype(np.float32),
            np.random.randn(512).astype(np.float32),
        ]
        store.save(embeddings)

        # 加载
        loaded = store.load()
        assert len(loaded) == 2
        for orig, ld in zip(embeddings, loaded):
            assert np.allclose(orig, ld, atol=1e-6), "Embedding round-trip mismatch"

        print("[PASS] OwnerProfileStore encrypt/decrypt round-trip")


def test_owner_profile_store_write_failure_preserves_existing_profile(
    tmp_path: Path, monkeypatch
) -> None:
    """临时写入失败不能截断已注册主人特征。"""
    from moeguard.storage import owner_profile
    from moeguard.storage.owner_profile import OwnerProfileStore
    from moeguard.utils.crypto import CryptoBox

    path = tmp_path / "embeddings.enc"
    store = OwnerProfileStore(path, CryptoBox(tmp_path / "key"))
    original = [np.ones(128, dtype=np.float32)]
    store.save(original)
    before = path.read_bytes()

    def fail_replace(*_args) -> None:
        raise OSError("full")

    monkeypatch.setattr(owner_profile.os, "replace", fail_replace)
    try:
        store.save([np.zeros(128, dtype=np.float32)])
    except OSError:
        pass
    else:
        raise AssertionError("expected atomic replacement failure")
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".embeddings.enc.*"))


def test_owner_profile_store_quarantines_corrupt_profile(tmp_path: Path) -> None:
    """坏密文不应让 UI 启动崩溃或误判为可值守档案。"""
    from moeguard.storage.owner_profile import OwnerProfileStore
    from moeguard.utils.crypto import CryptoBox

    path = tmp_path / "embeddings.enc"
    path.write_bytes(b"not-a-fernet-token")
    store = OwnerProfileStore(path, CryptoBox(tmp_path / "key"))
    assert store.load() == []
    assert store.last_load_error is not None
    assert not path.exists()
    assert len(list(tmp_path.glob("embeddings.enc.corrupt-*"))) == 1


def test_owner_profile_delete_removes_primary_quarantine_and_temp_files(
    tmp_path: Path,
) -> None:
    """撤回同意必须覆盖主档、损坏隔离档和中断写入残留。"""
    from moeguard.storage.owner_profile import OwnerProfileStore
    from moeguard.utils.crypto import CryptoBox

    path = tmp_path / "embeddings.enc"
    store = OwnerProfileStore(path, CryptoBox(tmp_path / "key"))
    path.write_bytes(b"primary")
    quarantine = tmp_path / "embeddings.enc.corrupt-deadbeef"
    quarantine.write_bytes(b"quarantine")
    temp_file = tmp_path / ".embeddings.enc.partial"
    temp_file.write_bytes(b"temporary")

    assert store.delete() == 3
    assert not path.exists()
    assert not quarantine.exists()
    assert not temp_file.exists()


def test_owner_profile_delete_propagates_failure_instead_of_claiming_success(
    tmp_path: Path, monkeypatch
) -> None:
    """主人特征被占用时必须向上层报告失败并允许重试。"""
    import pytest

    from moeguard.storage.owner_profile import OwnerProfileStore
    from moeguard.utils.crypto import CryptoBox

    path = tmp_path / "embeddings.enc"
    path.write_bytes(b"primary")
    store = OwnerProfileStore(path, CryptoBox(tmp_path / "key"))
    original_unlink = Path.unlink

    def fail_owner_unlink(candidate: Path, *args, **kwargs) -> None:
        if candidate == path:
            raise PermissionError("simulated locked owner profile")
        original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_owner_unlink)
    with pytest.raises(PermissionError, match="locked owner profile"):
        store.delete()
    assert path.exists()


def test_corrupt_cached_model_is_quarantined_before_download(
    tmp_path: Path, monkeypatch
) -> None:
    """缓存模型不能仅凭非空加载；完整性失败必须隔离后重取。"""
    from moeguard.security import face
    from moeguard.security.face import FaceRecognizer

    cached = tmp_path / "face_detection_yunet_2023mar.onnx"
    cached.write_bytes(b"truncated")
    monkeypatch.setattr(face, "bundled_model_path", lambda _name: None)
    monkeypatch.setattr(face, "model_path", lambda _name: cached)
    monkeypatch.setattr(
        face,
        "verify_model_file",
        lambda _name, path: (_ for _ in ()).throw(RuntimeError("bad hash"))
        if path == cached
        else None,
    )
    downloaded = []

    def fake_download(_name: str, path: Path) -> None:
        downloaded.append(path)
        path.write_bytes(b"replacement")

    monkeypatch.setattr(FaceRecognizer, "_download_model", staticmethod(fake_download))
    resolved = FaceRecognizer()._resolve_model(cached.name)
    assert resolved == cached
    assert downloaded == [cached]
    assert len(list(tmp_path.glob("*.invalid-*"))) == 1


def test_evidence_store() -> None:
    """EvidenceStore 事件目录创建与过期清理。"""
    from moeguard.storage.evidence_store import EvidenceStore

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "evidence"
        store = EvidenceStore(base, retention_days=7)

        # 创建事件目录
        import time
        ts = time.time()
        event_dir = store.create_event_dir(ts)
        assert event_dir.exists()
        assert event_dir.is_dir()

        # 列出事件
        events = store.list_events()
        assert len(events) == 1

        # 过期清理（创建一个 8 天前的目录，并修改 mtime 使其看起来过期）
        old_ts = ts - 8 * 86400
        old_dir = store.create_event_dir(old_ts)
        assert old_dir.exists()
        # 目录的 mtime 是创建时间（当前），需手动修改为过期时间
        import os
        os.utime(old_dir, (old_ts, old_ts))

        cleaned = store.cleanup_expired(ts)
        assert cleaned >= 1
        assert not old_dir.exists()
        assert event_dir.exists()  # 新的不被清理

        print("[PASS] EvidenceStore create/list/cleanup")


def test_database() -> None:
    """Database 基本操作。"""
    from moeguard.storage.db import Database

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        db.connect()

        # 记录事件
        import time
        now = time.time()
        db.log_incident(now, "stranger", "/evidence/event1")
        db.log_incident(now + 1, "stranger", "/evidence/event2")

        # 查询
        results = db.incidents_since(now - 1)
        assert len(results) == 2

        # 值守会话
        session_id = db.log_patrol_session(now, now + 60, 2, "lock_screen")
        assert session_id > 0

        sessions = db.patrol_sessions_since(now - 1)
        assert len(sessions) == 1
        assert sessions[0]["incident_count"] == 2

        db.close()
        print("[PASS] Database incidents + patrol_sessions")


def test_config_alignment() -> None:
    """配置对齐 D16 决议。"""
    from moeguard.config import SecurityConfig

    config = SecurityConfig()
    assert config.detector_model == "face_detection_yunet_2023mar.onnx"
    assert config.recognizer_model == "face_recognition_sface_2021dec.onnx"
    assert config.face_match_threshold == 0.363
    assert config.stranger_cooldown_sec == 30
    print("[PASS] Config alignment with D16 (YuNet+SFace, threshold=0.363)")


def test_paths() -> None:
    """路径管理模块。"""
    from moeguard.utils import paths

    assert paths.base_dir() == Path.home() / ".moeguard"
    assert paths.evidence_dir() == Path.home() / ".moeguard" / "evidence"
    assert paths.owner_dir() == Path.home() / ".moeguard" / "owner"
    assert paths.MODELS_DIR == Path.home() / ".moeguard" / "models"
    print("[PASS] Paths module (PRD §7.2: ~/.moeguard/)")


def test_license_updated() -> None:
    """__init__.py 许可证已更新为 Apache-2.0。"""
    import moeguard

    assert moeguard.__license__ == "Apache-2.0", f"Expected Apache-2.0, got {moeguard.__license__}"
    print("[PASS] License = Apache-2.0 (D6/D27)")


def main() -> int:
    tests = [
        test_license_updated,
        test_paths,
        test_config_alignment,
        test_face_recognizer_import,
        test_cosine_similarity,
        test_owner_profile_basic,
        test_owner_profile_store,
        test_evidence_store,
        test_database,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            failures += 1

    print(f"\n{'='*40}")
    print(f"Results: {len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
