"""M4.6 P0-1：运行时证据链行为测试（不需真实摄像头）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import QApplication

from moeguard.app import MoeGuardApp
from moeguard.config import EvidenceConfig, SecurityConfig
from moeguard.core.state_machine import StateMachine
from moeguard.security.camera import CameraCapture
from moeguard.security.evidence import EvidenceRecorder, EvidenceResult
from moeguard.storage.evidence_store import EvidenceStore


def _result_collector(recorder: EvidenceRecorder):
    """在启动后台线程前连接信号，避免快速失败结果丢失。"""
    results = []
    recorder.recording_finished.connect(results.append)
    return results


def _wait_for_result(results, timeout: float = 2.0):
    # 同一测试文件还会构造 SettingsDialog；必须从一开始使用 QApplication，
    # 否则先创建 QCoreApplication 后无法在同一进程升级为 GUI application。
    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout
    while not results and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert results, "后台证据录制未在时限内返回结果"
    return results[0]


class _FrameCamera:
    def __init__(self, frame: np.ndarray | None) -> None:
        self._frame = frame

    def grab(self):
        return None if self._frame is None else self._frame.copy()


class _FakeWriter:
    def __init__(self, path: str, *_args) -> None:
        self.path = Path(path)
        self.frames = []

    def isOpened(self) -> bool:
        return True

    def write(self, frame) -> None:
        self.frames.append(frame.copy())

    def release(self) -> None:
        self.path.write_bytes(b"fake-mjpg")


class _FakeVideoCapture:
    def __init__(self, _path: str) -> None:
        self.released = False

    @staticmethod
    def isOpened() -> bool:
        return True

    @staticmethod
    def read():
        return True, np.zeros((24, 32, 3), dtype=np.uint8)

    def release(self) -> None:
        self.released = True


def test_failed_recording_does_not_consume_cooldown(tmp_path: Path) -> None:
    recorder = EvidenceRecorder(EvidenceConfig(), SecurityConfig())
    results = _result_collector(recorder)
    assert recorder.start_recording(_FrameCamera(None), tmp_path, duration_sec=0)
    result = _wait_for_result(results)

    assert not result.succeeded
    assert not recorder.is_in_cooldown()
    recorder.discard_event()
    assert recorder.start_recording(_FrameCamera(None), tmp_path, duration_sec=0)


def test_successful_recording_is_pending_until_explicit_commit(
    tmp_path: Path, monkeypatch
) -> None:
    from moeguard.security import evidence

    monkeypatch.setattr(evidence.cv2, "VideoWriter", _FakeWriter)
    monkeypatch.setattr(evidence.cv2, "VideoCapture", _FakeVideoCapture)
    monkeypatch.setattr(
        evidence.cv2,
        "imwrite",
        lambda path, _frame: Path(path).write_bytes(b"fake-jpeg") is not None,
    )
    recorder = EvidenceRecorder(EvidenceConfig(), SecurityConfig())
    frame = np.zeros((24, 32, 3), dtype=np.uint8)
    results = _result_collector(recorder)
    assert recorder.start_recording(_FrameCamera(frame), tmp_path, duration_sec=0)
    result = _wait_for_result(results)

    assert result.succeeded
    assert result.pending_dir is not None and result.pending_dir.parent.name == ".pending"
    assert result.snapshot_path is not None and result.snapshot_path.exists()
    assert result.video_path is not None and result.video_path.exists()

    store = EvidenceStore(tmp_path, retention_days=7)
    final_dir = store.commit_pending(result.pending_dir, result.ts, result.event_uuid)
    assert final_dir.exists()
    assert final_dir.name.endswith(result.event_uuid)
    assert (
        store.commit_pending(result.pending_dir, result.ts, result.event_uuid)
        == final_dir
    )
    recorder.commit_event(now=result.ts)
    assert recorder.is_in_cooldown(now=result.ts + 1)


def test_empty_video_is_rejected_after_writer_release(tmp_path: Path, monkeypatch) -> None:
    """编码器即使成功打开，也不能把空文件提交为有效事件。"""
    from moeguard.security import evidence

    class _EmptyWriter(_FakeWriter):
        def release(self) -> None:
            self.path.write_bytes(b"")

    monkeypatch.setattr(evidence.cv2, "VideoWriter", _EmptyWriter)
    monkeypatch.setattr(
        evidence.cv2,
        "imwrite",
        lambda path, _frame: Path(path).write_bytes(b"fake-jpeg") is not None,
    )
    recorder = EvidenceRecorder(EvidenceConfig(), SecurityConfig())
    results = _result_collector(recorder)
    assert recorder.start_recording(
        _FrameCamera(np.zeros((24, 32, 3), dtype=np.uint8)),
        tmp_path,
        duration_sec=0,
    )
    result = _wait_for_result(results)

    assert not result.succeeded
    assert result.error is not None and "视频文件为空" in result.error
    assert not (tmp_path / ".pending").exists() or not any(
        (tmp_path / ".pending").iterdir()
    )


def test_cancel_and_wait_releases_writer_and_removes_pending(
    tmp_path: Path, monkeypatch
) -> None:
    """退出/撤回可以同步等到编码器释放，不留下未提交媒体。"""
    from moeguard.security import evidence

    monkeypatch.setattr(evidence.cv2, "VideoWriter", _FakeWriter)
    monkeypatch.setattr(
        evidence.cv2,
        "imwrite",
        lambda path, _frame: Path(path).write_bytes(b"fake-jpeg") is not None,
    )
    recorder = EvidenceRecorder(EvidenceConfig(), SecurityConfig())
    results = _result_collector(recorder)
    assert recorder.start_recording(
        _FrameCamera(np.zeros((24, 32, 3), dtype=np.uint8)),
        tmp_path,
        duration_sec=10,
    )

    assert recorder.cancel_and_wait(timeout=2.0)
    result = _wait_for_result(results)
    assert not result.succeeded
    assert result.error is not None and "录制已取消" in result.error
    assert not (tmp_path / ".pending").exists() or not any(
        (tmp_path / ".pending").iterdir()
    )


def test_privacy_blur_rejects_evidence_without_a_recognizer(tmp_path: Path) -> None:
    """隐私模式不得在检测器缺失时静默保存原始人脸画面。"""
    recorder = EvidenceRecorder(
        EvidenceConfig(blur_stranger_faces=True), SecurityConfig()
    )
    results = _result_collector(recorder)
    frame = np.zeros((24, 32, 3), dtype=np.uint8)
    assert recorder.start_recording(_FrameCamera(frame), tmp_path, duration_sec=0)
    result = _wait_for_result(results)

    assert not result.succeeded
    assert result.error is not None and "隐私模糊" in result.error
    pending_root = tmp_path / ".pending"
    assert not pending_root.exists() or not any(pending_root.iterdir())
    assert not recorder.is_in_cooldown()


def test_privacy_blur_rejects_evidence_when_detection_fails() -> None:
    """检测器异常同样必须 fail-closed，而不能返回原始帧。"""

    class _BrokenRecognizer:
        @staticmethod
        def detect(_frame):
            raise RuntimeError("model failure")

    recorder = EvidenceRecorder(
        EvidenceConfig(blur_stranger_faces=True),
        SecurityConfig(),
        _BrokenRecognizer(),
    )
    with np.testing.assert_raises_regex(RuntimeError, "拒绝保存原始画面"):
        recorder._process_frame(np.zeros((8, 8, 3), dtype=np.uint8), recorder._config)


def test_privacy_blur_pixelates_full_frame_when_no_face_is_detected() -> None:
    """零检测不能把原始画面原样写盘，须走整帧隐私降采样。"""

    class _EmptyRecognizer:
        @staticmethod
        def detect(_frame):
            return []

    recorder = EvidenceRecorder(
        EvidenceConfig(blur_stranger_faces=True),
        SecurityConfig(),
        _EmptyRecognizer(),
    )
    frame = np.arange(64 * 64 * 3, dtype=np.uint8).reshape((64, 64, 3))
    protected = recorder._process_frame(frame, recorder._config)
    assert protected.shape == frame.shape
    assert not np.array_equal(protected, frame)


def test_incident_count_changes_only_after_evidence_commit() -> None:
    machine = StateMachine()
    machine.on_manual_start()
    machine.on_stranger()
    assert machine.incident_count == 0
    machine.confirm_incident()
    assert machine.incident_count == 1


class _EventRecorder:
    def __init__(self) -> None:
        self.committed = 0
        self.discarded = 0

    def commit_event(self) -> None:
        self.committed += 1

    def discard_event(self) -> None:
        self.discarded += 1


class _EventState:
    is_patrolling = True

    def __init__(self) -> None:
        self.confirmed = 0

    def confirm_incident(self) -> None:
        self.confirmed += 1


class _FailingEventDatabase:
    def commit_evidence_event(self, **_kwargs):
        raise OSError("simulated database failure")


class _CommittedEventDatabase:
    def commit_evidence_event(self, **_kwargs):
        return 7, True


class _CountingEventDatabase:
    def __init__(self) -> None:
        self.calls = 0

    def commit_evidence_event(self, **_kwargs):
        self.calls += 1
        return 7, True


class _FailingEvidenceStore:
    def __init__(self) -> None:
        self.discarded = []

    @staticmethod
    def commit_pending(*_args):
        raise OSError("simulated rename failure")

    def discard_pending(self, pending_dir) -> None:
        self.discarded.append(pending_dir)

    @staticmethod
    def delete_event(_event_dir) -> None:
        raise AssertionError("no final directory should exist after rename failure")


class _FailingFeedback:
    @staticmethod
    def alert_stranger() -> None:
        raise RuntimeError("simulated alert failure")


def _event_result(tmp_path: Path) -> EvidenceResult:
    event_uuid = "c" * 32
    pending_dir = tmp_path / ".pending" / event_uuid
    pending_dir.mkdir(parents=True)
    snapshot = pending_dir / "snapshot.jpg"
    video = pending_dir / "clip.avi"
    snapshot.write_bytes(b"snapshot")
    video.write_bytes(b"video")
    return EvidenceResult(1.0, event_uuid, pending_dir, snapshot, video)


def _event_app(tmp_path: Path, database, feedback):
    app = MoeGuardApp.__new__(MoeGuardApp)
    app._evidence = _EventRecorder()
    app._evidence_store = EvidenceStore(tmp_path, retention_days=7)
    app._db = database
    app._feedback = feedback
    app._state_machine = _EventState()
    app._pending_evidence_kind = "stranger"
    app._today = lambda: "2026-08-10"
    return app


def test_evidence_transaction_failure_removes_media_before_state_confirmation(
    tmp_path: Path,
) -> None:
    """数据库事务失败时，媒体补偿且状态机/冷却均不得前进。"""
    result = _event_result(tmp_path)
    app = _event_app(tmp_path, _FailingEventDatabase(), _FailingFeedback())

    app._on_evidence_recording_finished(result)

    assert not app._evidence_store.list_events()
    assert app._state_machine.confirmed == 0
    assert app._evidence.committed == 0
    assert app._evidence.discarded == 1


def test_alert_failure_does_not_rollback_a_committed_evidence_event(tmp_path: Path) -> None:
    """非持久化的提示失败不能删除已经原子提交的证据。"""
    result = _event_result(tmp_path)
    app = _event_app(tmp_path, _CommittedEventDatabase(), _FailingFeedback())

    app._on_evidence_recording_finished(result)

    assert len(app._evidence_store.list_events()) == 1
    assert app._state_machine.confirmed == 1
    assert app._evidence.committed == 1
    assert app._evidence.discarded == 0


def test_evidence_rename_failure_does_not_reach_database_or_state(tmp_path: Path) -> None:
    """目录提交失败时不得写 SQLite、确认事件或消费录制槽位。"""
    result = _event_result(tmp_path)
    database = _CountingEventDatabase()
    app = _event_app(tmp_path, database, _FailingFeedback())
    app._evidence_store = _FailingEvidenceStore()

    app._on_evidence_recording_finished(result)

    assert database.calls == 0
    assert app._state_machine.confirmed == 0
    assert app._evidence.committed == 0
    assert app._evidence.discarded == 1
    assert app._evidence_store.discarded == [result.pending_dir]


def test_patrol_consent_defaults_to_denied_and_persists(tmp_path: Path) -> None:
    from dataclasses import replace

    from moeguard.config import AppConfig

    path = tmp_path / "config.toml"
    default = AppConfig()
    assert not default.consent.patrol_consent_granted
    approved = replace(
        default,
        consent=replace(default.consent, patrol_consent_granted=True),
    )
    AppConfig.save(approved, path)
    assert AppConfig.load(path).consent.patrol_consent_granted


def test_patrol_consent_requires_the_current_notice_version(tmp_path: Path) -> None:
    """旧知会或缺失版本都不能继续启动摄像头值守。"""
    from dataclasses import replace

    from moeguard.app import MoeGuardApp
    from moeguard.config import PATROL_CONSENT_VERSION, AppConfig

    app = MoeGuardApp()
    app._config = replace(
        AppConfig(),
        consent=replace(
            AppConfig().consent,
            patrol_consent_granted=True,
            patrol_consent_version="2026-08-m4.6",
        ),
    )
    assert not app._has_patrol_consent()

    app._config = replace(
        app.config,
        consent=replace(
            app.config.consent,
            patrol_consent_version=PATROL_CONSENT_VERSION,
        ),
    )
    assert app._has_patrol_consent()

    legacy_path = tmp_path / "legacy.toml"
    legacy_path.write_text(
        "[consent]\npatrol_consent_granted = true\n", encoding="utf-8"
    )
    legacy = AppConfig.load(legacy_path)
    assert legacy.consent.patrol_consent_version == ""


def test_mvp_settings_hide_cloud_and_never_probe_camera(monkeypatch) -> None:
    """打开 MVP 设置只展示本地功能，且绝不访问摄像头硬件。"""
    from dataclasses import replace

    from PySide6.QtWidgets import QApplication

    from moeguard.config import AppConfig
    from moeguard.security import camera
    from moeguard.ui.settings_dialog import SettingsDialog

    camera_opens: list[int] = []

    def reject_camera_open(index: int):
        camera_opens.append(index)
        raise AssertionError("opening SettingsDialog must not open a camera")

    monkeypatch.setattr(camera.cv2, "VideoCapture", reject_camera_open)
    qt_app = QApplication.instance() or QApplication([])
    config = AppConfig()
    config = replace(config, camera=replace(config.camera, device_index=7))
    dialog = SettingsDialog(config)
    assert camera_opens == []
    assert dialog.camera_index.currentData() == 7
    labels = [dialog._tabs.tabText(index) for index in range(dialog._tabs.count())]
    assert labels == ["安防", "通用"]
    dialog.close()
    assert qt_app is not None


def test_settings_replaces_a_missing_managed_role_with_lumen() -> None:
    from dataclasses import replace

    from moeguard.config import AppConfig
    from moeguard.ui.settings_dialog import SettingsDialog

    class EmptyLibrary:
        @staticmethod
        def list():
            return ()

    config = AppConfig()
    config = replace(
        config,
        pet=replace(
            config.pet,
            role_id="pet-aebbf3072f104527",
            role_package_version=2,
        ),
    )
    _qt_app = QApplication.instance() or QApplication([])
    dialog = SettingsDialog(
        config,
        role_library=EmptyLibrary(),
        custom_role_workbench_available=True,
        role_credit_dialog_available=True,
    )
    dialog.show()
    _qt_app.processEvents()

    assert dialog.role_selector.currentData() == "lumen"
    assert "当前不可用" not in " ".join(
        dialog.role_selector.itemText(index)
        for index in range(dialog.role_selector.count())
    )
    row = (
        dialog.import_role_button,
        dialog.custom_role_button,
        dialog.role_pilot_notice_button,
        dialog.remove_role_button,
    )
    assert len({button.height() for button in row}) == 1
    assert max(button.width() for button in row) - min(
        button.width() for button in row
    ) <= 1
    assert dialog.role_credit_button.height() == row[0].height()
    dialog.close()


def test_screen_edge_uses_fixed_bbox_pixels_and_physical_display(monkeypatch) -> None:
    from PySide6.QtCore import QRect

    from moeguard.ui.pet_window import PetWindow, edge_snap_position

    _qt_app = QApplication.instance() or QApplication([])
    surface = QRect(0, 0, 1920, 1080)
    content = QRect(30, 88, 140, 159)
    point = edge_snap_position(
        surface,
        QRect(500, 700, 200, 300),
        content,
        "bottom",
        0.75,
        reveal_pixels=64,
    )
    assert surface.bottom() + 1 - (point.y() + content.y()) == 64

    window = PetWindow()
    captured: list[tuple[QRect, str]] = []

    class ScreenStub:
        @staticmethod
        def geometry():
            return QRect(surface)

        @staticmethod
        def availableGeometry():
            return QRect(0, 0, 1920, 1040)

    monkeypatch.setattr(PetWindow, "screen", lambda _self: ScreenStub())
    monkeypatch.setattr(
        window,
        "snap_to_surface",
        lambda target, direction: captured.append((QRect(target), direction)),
    )
    window.move(500, surface.bottom() - window.height() + 10)
    window._check_edge_snap()
    assert captured == [(surface, "bottom")]
    window.close()


def test_tray_has_no_paid_auto_patrol_dead_entry() -> None:
    """免费锁屏自动值守只在设置中配置，托盘不保留误导性的付费死入口。"""
    from PySide6.QtGui import QIcon

    from moeguard.ui.tray import TrayIcon

    _qt_app = QApplication.instance() or QApplication([])
    tray = TrayIcon(QIcon())
    labels = [action.text() for action in tray._tray.contextMenu().actions()]
    assert all("付费" not in label and "离座检测" not in label for label in labels)
    tray.shutdown()


def test_tray_switches_between_companion_and_patrol_icons() -> None:
    from PySide6.QtGui import QColor, QIcon, QPixmap

    from moeguard.ui.tray import TrayIcon

    _qt_app = QApplication.instance() or QApplication([])
    companion_pixmap = QPixmap(8, 8)
    companion_pixmap.fill(QColor("#2384d6"))
    patrol_pixmap = QPixmap(8, 8)
    patrol_pixmap.fill(QColor("#f0a070"))
    companion = QIcon(companion_pixmap)
    patrol = QIcon(patrol_pixmap)
    tray = TrayIcon(companion, patrol_icon=patrol)

    tray.set_state(False)
    assert tray._tray.icon().cacheKey() == companion.cacheKey()
    tray.set_state(True)
    assert tray._tray.icon().cacheKey() == patrol.cacheKey()
    tray.shutdown()


def test_tray_only_shows_direct_role_entry_when_workbench_is_available() -> None:
    from PySide6.QtGui import QIcon

    from moeguard.ui.tray import TrayIcon

    qt_app = QApplication.instance() or QApplication([])
    tray = TrayIcon(QIcon())
    opened: list[bool] = []
    tray.open_custom_role_workbench.connect(lambda: opened.append(True))

    assert not tray._act_custom_role.isVisible()
    tray.set_custom_role_workbench_available(True)
    assert tray._act_custom_role.isVisible()
    tray._act_custom_role.trigger()
    assert opened == [True]

    tray.set_custom_role_workbench_available(False)
    assert not tray._act_custom_role.isVisible()
    assert qt_app is not None
    tray.shutdown()


def test_tray_shutdown_is_idempotent_and_detaches_native_menu() -> None:
    from PySide6.QtGui import QIcon

    from moeguard.ui.tray import TrayIcon

    qt_app = QApplication.instance() or QApplication([])
    tray = TrayIcon(QIcon())
    assert tray._tray.contextMenu() is tray._menu

    tray.shutdown()
    assert tray._tray.contextMenu() is None
    tray.shutdown()
    qt_app.processEvents()

    assert tray._shutdown_started is True


def test_evidence_store_can_delete_one_or_all_events(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path, retention_days=7)
    first = store.create_event_dir(ts=1)
    second = store.create_event_dir(ts=2)
    assert store.delete_event(first)
    assert not first.exists()
    assert store.delete_all() == 1
    assert not second.exists()


def test_evidence_store_delete_failure_is_reported_and_media_remains(
    tmp_path: Path, monkeypatch
) -> None:
    """Windows 文件占用等删除错误不能伪装为成功。"""
    import pytest

    from moeguard.storage import evidence_store

    store = EvidenceStore(tmp_path, retention_days=7)
    event_dir = store.create_event_dir(ts=1)
    (event_dir / "clip.avi").write_bytes(b"locked")

    def fail_delete(_path) -> None:
        raise PermissionError("simulated locked file")

    monkeypatch.setattr(evidence_store.shutil, "rmtree", fail_delete)
    with pytest.raises(PermissionError, match="locked file"):
        store.delete_event(event_dir)
    assert event_dir.exists()


def test_windows_locked_evidence_stays_visible_until_retry(tmp_path: Path) -> None:
    """在真实 Windows 独占句柄下验证删除失败可见，释放后可以重试。"""
    import os

    if os.name != "nt":
        import pytest

        pytest.skip("Windows exclusive-file semantics only")

    import ctypes
    from ctypes import wintypes

    import pytest

    store = EvidenceStore(tmp_path, retention_days=7)
    event_dir = store.create_event_dir(ts=1)
    video = event_dir / "clip.avi"
    video.write_bytes(b"locked")

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(str(video), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(OSError):
            store.delete_event(event_dir)
        assert event_dir.exists()
        assert video.exists()
    finally:
        assert close_handle(handle)

    assert store.delete_event(event_dir)
    assert not event_dir.exists()


def test_stale_pending_cleanup_preserves_active_recording(tmp_path: Path) -> None:
    """崩溃残留会被清理，仍在安全录制窗口内的目录不会被误删。"""
    import os

    store = EvidenceStore(tmp_path, retention_days=7)
    pending_root = tmp_path / ".pending"
    stale = pending_root / "stale"
    active = pending_root / "active"
    stale.mkdir(parents=True)
    active.mkdir()
    os.utime(stale, (100.0, 100.0))
    os.utime(active, (950.0, 950.0))

    assert store.cleanup_stale_pending(now=1000.0, max_age_seconds=100.0) == [stale]
    assert not stale.exists()
    assert active.exists()


class _WithdrawalOwner:
    def __init__(self) -> None:
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True


class _WithdrawalRecorder:
    def __init__(self, stopped: bool = True) -> None:
        self.stopped = stopped
        self.calls = 0

    def cancel_and_wait(self) -> bool:
        self.calls += 1
        return self.stopped


class _WithdrawalOwnerStore:
    @staticmethod
    def delete() -> int:
        return 2


class _WithdrawalEvidenceStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def delete_all(self) -> int:
        if self.fail:
            raise PermissionError("simulated locked evidence")
        return 3


class _WithdrawalDatabase:
    def __init__(self) -> None:
        self.delete_all_incidents_calls = 0
        self.reconcile_calls = 0
        self.delete_usage_calls = 0

    def delete_all_incidents(self) -> int:
        self.delete_all_incidents_calls += 1
        return 4

    def delete_incidents_with_missing_evidence(self) -> int:
        self.reconcile_calls += 1
        return 1

    def delete_all_usage(self) -> int:
        self.delete_usage_calls += 1
        return 5


def _withdrawal_app(*, evidence_fail: bool = False, persist: bool = True):
    from dataclasses import replace

    from moeguard.config import AppConfig

    app = MoeGuardApp()
    base = AppConfig()
    app._config = replace(
        base,
        consent=replace(base.consent, patrol_consent_granted=True),
        presence=replace(base.presence, auto_patrol_enabled=True),
    )
    app._state_machine = None
    app._owner = _WithdrawalOwner()
    app._owner_store = _WithdrawalOwnerStore()
    app._evidence = _WithdrawalRecorder()
    app._evidence_store = _WithdrawalEvidenceStore(fail=evidence_fail)
    app._db = _WithdrawalDatabase()
    app._tray = None
    app._persist_config = lambda *_args, **_kwargs: persist
    return app


def test_withdrawal_reports_success_only_after_all_deletions_complete() -> None:
    app = _withdrawal_app()
    results = []
    app.withdrawal_completed.connect(
        lambda success, message: results.append((success, message))
    )

    assert app._withdraw_patrol_consent()
    assert results and results[-1][0]
    assert app._owner.cleared
    assert app._evidence.calls == 1
    assert app._db.delete_all_incidents_calls == 1
    assert app._db.reconcile_calls == 0
    assert app._db.delete_usage_calls == 1
    assert not app._config.consent.patrol_consent_granted
    assert not app._config.presence.auto_patrol_enabled


def test_withdrawal_failure_is_visible_and_keeps_remaining_evidence_index() -> None:
    app = _withdrawal_app(evidence_fail=True, persist=False)
    results = []
    app.withdrawal_completed.connect(
        lambda success, message: results.append((success, message))
    )

    assert not app._withdraw_patrol_consent()
    assert results and not results[-1][0]
    assert "未能" in results[-1][1]
    assert app._owner.cleared
    assert app._db.delete_all_incidents_calls == 0
    assert app._db.reconcile_calls == 1
    assert app._db.delete_usage_calls == 1
    assert not app._config.consent.patrol_consent_granted


def test_camera_reads_only_from_its_background_thread(monkeypatch) -> None:
    from moeguard.security import camera

    class _FakeCapture:
        def __init__(self, _index: int) -> None:
            self.opened = True
            self.read_threads: list[int] = []

        def isOpened(self) -> bool:
            return self.opened

        def read(self):
            self.read_threads.append(threading.get_ident())
            return True, np.zeros((8, 8, 3), dtype=np.uint8)

        def release(self) -> None:
            self.opened = False

    fake = _FakeCapture(0)
    monkeypatch.setattr(camera.cv2, "VideoCapture", lambda _index: fake)
    capture = CameraCapture()
    main_thread = threading.get_ident()
    assert capture.start(interval_ms=20)
    deadline = time.monotonic() + 1.0
    while capture.grab() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    capture.stop()

    assert fake.read_threads
    assert all(thread_id != main_thread for thread_id in fake.read_threads)


def test_camera_rejects_restart_until_a_blocked_reader_has_exited(monkeypatch) -> None:
    """旧驱动 read 卡死时，不能让旧线程读取新 session 的设备。"""
    from moeguard.security import camera

    class _BlockingCapture:
        def __init__(self, _index: int) -> None:
            self.opened = True
            self.entered = threading.Event()
            self.unblock = threading.Event()

        def isOpened(self) -> bool:
            return self.opened

        def read(self):
            self.entered.set()
            self.unblock.wait(timeout=2.0)
            return True, np.zeros((8, 8, 3), dtype=np.uint8)

        def release(self) -> None:
            self.opened = False

    class _WorkingCapture:
        def __init__(self, _index: int) -> None:
            self.opened = True

        def isOpened(self) -> bool:
            return self.opened

        @staticmethod
        def read():
            return True, np.zeros((8, 8, 3), dtype=np.uint8)

        def release(self) -> None:
            self.opened = False

    blocked = _BlockingCapture(0)
    factory_calls = 0

    def factory(index: int):
        nonlocal factory_calls
        factory_calls += 1
        return blocked if factory_calls == 1 else _WorkingCapture(index)

    monkeypatch.setattr(camera.cv2, "VideoCapture", factory)
    capture = CameraCapture()
    assert capture.start(0, interval_ms=10)
    assert blocked.entered.wait(timeout=1.0)

    capture.stop()
    assert not capture.start(1, interval_ms=10)
    assert capture.last_error == "摄像头驱动停止超时，请稍后重试"
    assert factory_calls == 1

    blocked.unblock.set()
    deadline = time.monotonic() + 1.0
    while capture._capture_thread is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert capture.start(1, interval_ms=10)
    capture.stop()
    assert factory_calls == 2


def test_camera_device_discovery_lists_openable_indices(monkeypatch) -> None:
    from moeguard.security import camera

    class _ProbeCapture:
        def __init__(self, index: int) -> None:
            self.index = index

        def isOpened(self) -> bool:
            return self.index in {1, 3}

        def release(self) -> None:
            pass

    monkeypatch.setattr(camera.cv2, "VideoCapture", _ProbeCapture)
    assert CameraCapture.available_device_indices(max_devices=4) == [1, 3]


def test_camera_reports_a_gap_after_capture_resumes(monkeypatch) -> None:
    from moeguard.security import camera

    class _DelayedCapture:
        def __init__(self, _index: int) -> None:
            self.opened = True
            self.reads = 0

        def isOpened(self) -> bool:
            return self.opened

        def read(self):
            self.reads += 1
            if self.reads == 2:
                time.sleep(0.08)
            return True, np.zeros((8, 8, 3), dtype=np.uint8)

        def release(self) -> None:
            self.opened = False

    monkeypatch.setattr(camera.cv2, "VideoCapture", _DelayedCapture)
    capture = CameraCapture()
    capture._gap_threshold_sec = lambda: 0.05  # type: ignore[method-assign]
    gaps: list[float] = []
    capture.capture_gap_detected.connect(gaps.append)
    assert capture.start(interval_ms=10)
    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + 1.0
    while not gaps and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    capture.stop()
    assert gaps and gaps[0] >= 0.05


def test_camera_reports_a_gap_before_repeated_read_failure(monkeypatch) -> None:
    """S0 后驱动无法恢复时，也必须报告暂停造成的采集空洞。"""
    from moeguard.security import camera

    class _SuspendedCapture:
        def __init__(self, _index: int) -> None:
            self.opened = True
            self.reads = 0

        def isOpened(self) -> bool:
            return self.opened

        def read(self):
            self.reads += 1
            if self.reads == 1:
                return True, np.zeros((8, 8, 3), dtype=np.uint8)
            if self.reads == 2:
                time.sleep(0.08)
            return False, None

        def release(self) -> None:
            self.opened = False

    monkeypatch.setattr(camera.cv2, "VideoCapture", _SuspendedCapture)
    capture = CameraCapture()
    capture._gap_threshold_sec = lambda: 0.05  # type: ignore[method-assign]
    gaps: list[float] = []
    failures: list[str] = []
    capture.capture_gap_detected.connect(gaps.append)
    capture.camera_failed.connect(failures.append)
    assert capture.start(interval_ms=10)
    app = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + 1.0
    while not failures and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    capture.stop()
    assert failures
    assert gaps and gaps[0] >= 0.05


def test_unlock_handler_reveals_pending_interruption_to_user() -> None:
    """锁屏期间的托盘提示可能不可见，解锁后必须补显中断信息。"""
    from moeguard.app import MoeGuardApp

    class _State:
        def __init__(self) -> None:
            self.unlocked = False

        def on_unlock(self) -> None:
            self.unlocked = True

    class _Message:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def emit(self, message: str) -> None:
            self.messages.append(message)

    class _Feedback:
        def __init__(self) -> None:
            self.message = _Message()

    app = MoeGuardApp()
    app._state_machine = _State()  # type: ignore[assignment]
    app._feedback = _Feedback()  # type: ignore[assignment]
    app._pending_interruption_notice = "值守曾中断约 22 秒；该时段未计入正常值守。"
    app._on_unlock()
    assert app._state_machine.unlocked
    assert app._feedback.message.messages == ["值守曾中断约 22 秒；该时段未计入正常值守。"]
    assert app._pending_interruption_notice is None
