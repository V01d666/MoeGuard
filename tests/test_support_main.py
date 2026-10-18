from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPushButton

from moeguard import updater as updater_module
from moeguard.app import MoeGuardApp
from moeguard.cloud.role_service_binding import (
    RoleServiceEnrollmentStore,
)
from moeguard.cloud.role_service_http_client import (
    HttpRoleServiceTransport,
    RoleServiceAccountSummary,
    RoleServiceConnectionError,
    RoleServiceEnrollment,
)
from moeguard.cloud.role_service_session import RoleServiceSession, RoleServiceSessionStore
from moeguard.cloud.role_workbench import (
    FakeRoleWorkbenchBackend,
    RemoteRoleWorkbenchBackend,
    RoleCreditDialog,
    RoleWorkbenchDialog,
)
from moeguard.role_main import configure_role_workbench
from moeguard.roles import RoleLibrary


def _wait_connection(app, qt_app):
    deadline = time.monotonic() + 5
    while app._service_connection.busy and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(.005)
    qt_app.processEvents()
    assert not app._service_connection.busy


def _automatic_app(tmp_path, monkeypatch, enroll, summary=None):
    from moeguard.config import AppConfig

    monkeypatch.setattr(HttpRoleServiceTransport, "enroll", enroll)
    calls = []

    def account_summary(transport):
        calls.append(transport._bearer_token)
        value = summary(len(calls)) if summary is not None else None
        if isinstance(value, Exception):
            raise value
        return value or RoleServiceAccountSummary(5, 0, 1, 2, 3, 0)

    monkeypatch.setattr(HttpRoleServiceTransport, "account_summary", account_summary)
    library = RoleLibrary(tmp_path / "roles")
    monkeypatch.setattr("moeguard.ui.settings_dialog.RoleLibrary", lambda: library)
    sessions = RoleServiceSessionStore(tmp_path / "session.json", _TestProtector())
    enrollments = RoleServiceEnrollmentStore(tmp_path / "enrollment.json", _TestProtector())
    app = MoeGuardApp()
    app._config = AppConfig()
    configure_role_workbench(
        app, environ={}, storage_root=tmp_path / "workbench", role_library=library,
        session_store=sessions, enrollment_store=enrollments,
        service_origin="https://roles.example", clock=lambda: 1_900_000_000,
        watch_network=False,
    )
    app._summary_calls = calls
    return app, sessions, enrollments


_BALANCE_TEXT = "立绘生成 2 次 · 动作生成 3 次 · 任务占用 0 次 · 已使用 1 次"


def _automatic_enrollment(token_char="C"):
    return RoleServiceEnrollment("client-" + "a" * 32,
                                 "mgr_" + "b" * 24 + "_" + token_char * 43, 2_000_000_000)


def test_startup_connects_off_ui_thread_updates_open_settings_and_reuses_session(
    tmp_path, qt_app, monkeypatch,
):
    calls = []
    release = threading.Event()
    gui_thread = threading.get_ident()

    def enroll(*args):
        calls.append((threading.get_ident(), args))
        assert release.wait(5)
        return _automatic_enrollment()

    app, sessions, _ = _automatic_app(tmp_path, monkeypatch, enroll)
    delivered_on = []
    app._service_connection.changed.connect(
        lambda *_: delivered_on.append(threading.get_ident())
    )
    app.ready.emit()
    app._service_connection.retry()  # Coalesced with startup.
    app._on_open_settings()
    dialog = app._settings_dialog
    assert app._service_connection.busy
    assert dialog.connect_service_button.isHidden()  # Only shown after a failure.
    assert dialog.support_id_edit.text() == ""
    release.set()
    _wait_connection(app, qt_app)
    assert len(calls) == 1 and calls[0][0] != gui_thread
    assert all(thread == gui_thread for thread in delivered_on)
    assert dialog.support_id_edit.text() == _automatic_enrollment().account_id
    assert dialog.copy_support_id_button.isEnabled()
    assert dialog.connect_service_button.isHidden()
    assert "立绘 2 次 · 动作 3 次" in dialog.support_id_edit.placeholderText()
    assert sessions.load() is not None
    assert len(app._summary_calls) == 1  # Connecting and the balance are one request.
    dialog.reject()
    qt_app.processEvents()

    restarted, _, _ = _automatic_app(tmp_path, monkeypatch, enroll)
    restarted.ready.emit()
    _wait_connection(restarted, qt_app)
    assert len(calls) == 1  # The stored session is reused; no enrollment.
    assert len(restarted._summary_calls) == 1
    assert restarted._support_id_provider() == _automatic_enrollment().account_id


def test_startup_failure_is_sanitized_and_settings_retry_keeps_identity(
    tmp_path, qt_app, monkeypatch,
):
    calls = []

    def enroll(*args):
        calls.append(args)
        if len(calls) == 1:
            raise TimeoutError("private request detail")
        return _automatic_enrollment()

    app, _, _ = _automatic_app(tmp_path, monkeypatch, enroll)
    app.ready.emit()
    _wait_connection(app, qt_app)
    assert app._service_connection.offline
    app._on_open_settings()
    dialog = app._settings_dialog
    assert "private" not in dialog.support_id_edit.placeholderText()
    assert not dialog.connect_service_button.isHidden()
    dialog.connect_service_button.click()
    _wait_connection(app, qt_app)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert dialog.support_id_edit.text() == _automatic_enrollment().account_id
    assert dialog.connect_service_button.isHidden()
    dialog.reject()
    qt_app.processEvents()


def test_expired_session_recovers_existing_identity_and_closed_settings_are_safe(
    tmp_path, qt_app, monkeypatch,
):
    calls = []

    def enroll(*args):
        calls.append(args)
        return _automatic_enrollment()

    app, sessions, _ = _automatic_app(tmp_path, monkeypatch, enroll)
    app.ready.emit()
    _wait_connection(app, qt_app)
    previous = sessions.load()
    from dataclasses import replace

    sessions.save(replace(previous, expires_at=1_800_000_000))
    restarted, _, _ = _automatic_app(tmp_path, monkeypatch, enroll)
    restarted._on_open_settings()
    restarted._settings_dialog.reject()
    qt_app.processEvents()
    restarted.ready.emit()
    _wait_connection(restarted, qt_app)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert restarted._service_connection.account_id == previous.account_id


def test_rejected_token_renews_silently_with_the_same_support_id(
    tmp_path, qt_app, monkeypatch,
):
    from moeguard.cloud.role_service_http_client import RoleServiceHttpError

    enrollments = []

    def enroll(*args):
        enrollments.append(args)
        return _automatic_enrollment("C" if len(enrollments) == 1 else "D")

    def summary(call):
        if call == 2:
            return RoleServiceHttpError(401, "invalid_token", "private detail")
        return None

    app, sessions, _ = _automatic_app(tmp_path, monkeypatch, enroll, summary)
    app.ready.emit()
    _wait_connection(app, qt_app)
    first = sessions.load()
    app._service_connection.invalidate()
    _wait_connection(app, qt_app)

    renewed = sessions.load()
    assert len(enrollments) == 2 and enrollments[0] == enrollments[1]
    assert renewed.account_id == first.account_id
    assert renewed.bearer_token != first.bearer_token
    assert app._summary_calls == [first.bearer_token, first.bearer_token,
                                  renewed.bearer_token]
    assert not app._service_connection.offline


def test_legacy_disconnect_marker_no_longer_keeps_the_user_offline(
    tmp_path, qt_app, monkeypatch,
):
    calls = []

    def enroll(*args):
        calls.append(args)
        return _automatic_enrollment()

    (tmp_path / "automatic-connection-disabled").write_bytes(b"")
    app, sessions, _ = _automatic_app(tmp_path, monkeypatch, enroll)
    app.ready.emit()
    _wait_connection(app, qt_app)
    assert len(calls) == 1 and sessions.load() is not None
    assert not (tmp_path / "automatic-connection-disabled").exists()
    assert app._service_connection.account_id == _automatic_enrollment().account_id


def test_service_surfaces_share_one_balance_and_refetch_only_when_stale(
    tmp_path, qt_app, monkeypatch,
):
    app, _, _ = _automatic_app(tmp_path, monkeypatch, lambda *_: _automatic_enrollment())
    connection = app._service_connection
    now = [1000.0]
    connection._clock = lambda: now[0]
    app.ready.emit()
    _wait_connection(app, qt_app)
    assert len(app._summary_calls) == 1

    workbench = _factory(app)()
    credit = _credit_factory(app)()
    _wait_connection(app, qt_app)
    assert len(app._summary_calls) == 1  # Fresh cache: opening costs nothing.
    assert workbench.account_summary_label.text() == _BALANCE_TEXT
    assert workbench.account_refresh_button.isHidden()
    assert workbench.support_id_edit.text() == _automatic_enrollment().account_id
    assert credit.balance_label.text() == _BALANCE_TEXT
    assert credit.refresh_button.isHidden()
    workbench.close()
    credit.close()

    now[0] += 5 * 60
    reopened = _factory(app)()
    _wait_connection(app, qt_app)
    assert len(app._summary_calls) == 2  # Stale after five minutes.
    reopened.close()


def test_workbench_opened_before_startup_finishes_follows_the_connection(
    tmp_path, qt_app, monkeypatch,
):
    release = threading.Event()

    def enroll(*_args):
        assert release.wait(5)
        return _automatic_enrollment()

    app, _, _ = _automatic_app(tmp_path, monkeypatch, enroll)
    app.ready.emit()
    dialog = _factory(app)()
    assert dialog.prepare_button.isEnabled()  # Generating connects on demand.
    assert dialog.account_summary_label.text() == "生成次数：正在获取…"
    assert dialog.support_id_row.isHidden()
    release.set()
    _wait_connection(app, qt_app)
    assert dialog.account_summary_label.text() == _BALANCE_TEXT
    assert not dialog.support_id_row.isHidden()
    assert dialog.support_id_edit.text() == _automatic_enrollment().account_id
    dialog.close()


def test_offline_retry_is_event_driven_and_rate_limited(
    tmp_path, qt_app, monkeypatch,
):
    def enroll(*_args):
        raise TimeoutError("offline")

    app, _, _ = _automatic_app(tmp_path, monkeypatch, enroll)
    connection = app._service_connection
    attempts = []
    original = connection.transport.account_summary
    monkeypatch.setattr(
        connection.transport, "account_summary",
        lambda: attempts.append(1) or original(),
    )
    now = [1000.0]
    connection._clock = lambda: now[0]
    connection._retry_timer.stop()
    app.ready.emit()
    _wait_connection(app, qt_app)
    connection._retry_timer.stop()
    assert connection.offline and len(attempts) == 1

    dialog = _factory(app)()  # Opening a surface retries, at most every 30 s.
    _wait_connection(app, qt_app)
    connection._retry_timer.stop()
    assert len(attempts) == 1
    assert not dialog.account_refresh_button.isHidden()
    now[0] += 30
    dialog.close()
    dialog = _factory(app)()
    _wait_connection(app, qt_app)
    connection._retry_timer.stop()
    assert len(attempts) == 2
    dialog.account_refresh_button.click()  # The user's retry is never throttled.
    _wait_connection(app, qt_app)
    connection._retry_timer.stop()
    assert len(attempts) == 3
    dialog.close()


def test_credit_hint_beside_generate_button_names_the_shortfall(
    tmp_path, qt_app, monkeypatch,
):
    app, _, _ = _automatic_app(
        tmp_path, monkeypatch, lambda *_: _automatic_enrollment(),
        lambda _call: RoleServiceAccountSummary(1, 0, 0, 1, 0, 0),
    )
    app.ready.emit()
    _wait_connection(app, qt_app)
    dialog = _factory(app)()
    dialog.show()
    qt_app.processEvents()
    dialog.candidate_count.setValue(2)
    assert not dialog.credit_hint_row.isHidden()
    assert dialog.credit_hint_label.text() == "立绘次数不足：需要 2 次，剩 1 次"
    assert dialog.prepare_button.isEnabled()  # A hint, not a gate.
    dialog.candidate_count.setValue(1)
    assert dialog.credit_hint_row.isHidden()
    dialog.input_tabs.setCurrentIndex(1)  # An uploaded image costs nothing.
    dialog.candidate_count.setValue(4)
    assert dialog.credit_hint_row.isHidden()
    dialog._result_saved = True
    dialog.close()


class _TestProtector:
    def protect(self, value: bytes) -> bytes:
        return b"test:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"test:"):
            raise ValueError("invalid test ciphertext")
        return value.removeprefix(b"test:")[::-1]


def _session_store(path: Path, *, expires_at: int = 2_000_000_000):
    store = RoleServiceSessionStore(path, _TestProtector())
    store.save(
        RoleServiceSession(
            service_origin="https://roles.example",
            account_id="client-" + "a" * 32,
            expires_at=expires_at,
            bearer_token="mgr_" + "b" * 24 + "_" + "C" * 43,
            enrollment_id="eni_" + "d" * 32,
            enrollment_secret="ens_" + "E" * 43,
        )
    )
    return store


@pytest.fixture
def qt_app(monkeypatch) -> QApplication:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _factory(app: MoeGuardApp):
    factory = app._custom_role_workbench_factory
    assert factory is not None
    return factory


def _credit_factory(app: MoeGuardApp):
    factory = app._role_credit_dialog_factory
    assert factory is not None
    return factory


def test_busy_workbench_uses_rotating_activity_instead_of_fake_percent(
    tmp_path: Path, qt_app
) -> None:
    dialog = RoleWorkbenchDialog(FakeRoleWorkbenchBackend(tmp_path / "sessions"))

    dialog._set_busy(True)
    first = dialog.activity_indicator.text()
    dialog._rotate_activity_indicator()
    second = dialog.activity_indicator.text()
    dialog._on_progress("模型正在生成立绘…", 1)

    assert dialog._activity_timer.isActive()
    assert dialog.activity_indicator.isVisibleTo(dialog)
    assert "正在认真制作中" in first
    assert first != second
    assert dialog.status.text() == "模型正在生成立绘…"

    dialog._set_busy(False)
    assert not dialog._activity_timer.isActive()
    assert dialog.activity_indicator.isHidden()
    dialog.close()


def test_remote_backend_refuses_accidental_local_execution(tmp_path: Path) -> None:
    backend = RemoteRoleWorkbenchBackend(tmp_path / "workbench")

    assert backend.is_fake is False
    assert backend.storage_root == tmp_path / "workbench"
    with pytest.raises(RuntimeError, match="必须通过角色服务"):
        backend.prepare_candidates(object(), object())


def test_support_entry_shows_workbench_without_falling_back_to_fake(
    tmp_path: Path, qt_app
) -> None:
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={},
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        service_config_path=tmp_path / "missing-role-service.json",
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    assert isinstance(dialog._backend, RemoteRoleWorkbenchBackend)
    assert dialog._service_client is None
    assert "离线管理模式" in dialog.mode_hint.text()
    assert dialog.prepare_button.isEnabled() is False
    assert "尚未配置生成服务地址" in "".join(
        label.text() for label in dialog.service_bar.findChildren(QLabel)
    )
    dialog._prepare_candidates()
    assert dialog._worker is None
    assert dialog._candidate_result is None
    dialog.close()


def test_settings_open_reconciles_a_vanished_managed_role(qt_app, monkeypatch) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from moeguard.config import AppConfig
    from moeguard.pet.frame_animation import FrameAnimationController

    app = MoeGuardApp()
    config = AppConfig()
    app._config = replace(
        config,
        pet=replace(
            config.pet,
            role_id="pet-aebbf3072f104527",
            role_package_version=2,
        ),
    )
    app._frame_controller = FrameAnimationController()

    class MissingLibrary:
        @staticmethod
        def get(_key):
            raise OSError("package vanished")

    class PetWindowStub:
        def set_edge_reveal_fraction(self, _value):
            pass

    class FeedbackStub:
        restored = False

        def set_click_lines(self, _lines):
            pass

        def restore_animation(self):
            self.restored = True

    app._pet_window = PetWindowStub()
    app._feedback = FeedbackStub()
    persisted: list[str] = []
    monkeypatch.setattr("moeguard.app.RoleLibrary", MissingLibrary)
    monkeypatch.setattr(
        app,
        "_load_pet_frames",
        lambda _pet: SimpleNamespace(edge_reveal_fraction=1 / 6, click_lines=()),
    )
    monkeypatch.setattr(
        app,
        "_persist_config",
        lambda reason, **_kwargs: persisted.append(reason) or True,
    )

    app._on_open_settings()
    qt_app.processEvents()
    assert app._config.pet.role_id == "lumen"
    assert app._config.pet.role_package_version == 0
    assert app._settings_dialog is not None
    assert app._settings_dialog.role_selector.currentData() == "lumen"
    assert persisted == ["失效角色绑定自动回退"]
    assert app._feedback.restored is True
    app._settings_dialog.reject()


def test_support_entry_rejects_invalid_configuration_without_echoing_details(
    tmp_path: Path, qt_app
) -> None:
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={
            "MOEGUARD_ROLE_SERVICE_URL": "https://private-host.example",
            "MOEGUARD_ROLE_SERVICE_TOKEN_FILE": str(tmp_path / "private.token"),
        },
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    notice = "".join(label.text() for label in dialog.service_bar.findChildren(QLabel))
    assert "连接信息无效" in notice
    assert "private-host" not in notice
    assert "private.token" not in notice
    assert dialog.prepare_button.isEnabled() is False
    dialog.close()


def test_support_entry_builds_https_workbench_from_token_file(
    tmp_path: Path, qt_app
) -> None:
    token_file = tmp_path / "role-service.token"
    token_file.write_text("mgr_" + "a" * 24 + "_" + "B" * 43, encoding="ascii")
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={
            "MOEGUARD_ROLE_SERVICE_MODE": "https",
            "MOEGUARD_ROLE_SERVICE_URL": "https://roles.example",
            "MOEGUARD_ROLE_SERVICE_TOKEN_FILE": str(token_file.resolve()),
        },
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    assert isinstance(dialog._backend, RemoteRoleWorkbenchBackend)
    assert dialog._service_client is not None
    assert "在线生成模式" in dialog.mode_hint.text()
    assert dialog._show_costs is False
    dialog.close()


def test_support_entry_prefers_os_protected_session_without_environment(
    tmp_path: Path, qt_app
) -> None:
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={},
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        session_store=_session_store(tmp_path / "session.json"),
        clock=lambda: 1_900_000_000,
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    assert isinstance(dialog._backend, RemoteRoleWorkbenchBackend)
    assert dialog._service_client is not None
    assert dialog.support_id_edit is not None
    assert dialog.support_id_edit.text() == "client-" + "a" * 32
    dialog.close()

    credit_dialog = _credit_factory(app)()
    assert isinstance(credit_dialog, RoleCreditDialog)
    assert credit_dialog._service_transport is not None
    assert credit_dialog.support_id_edit is not None
    assert credit_dialog.support_id_edit.text() == "client-" + "a" * 32
    credit_dialog.close()


def test_expired_protected_session_still_opens_an_online_workbench(
    tmp_path: Path, qt_app
) -> None:
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={},
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        session_store=_session_store(tmp_path / "session.json", expires_at=100),
        service_origin="https://roles.example",
        clock=lambda: 101,
        watch_network=False,
    )

    dialog = _factory(app)()

    # The expired token is renewed with the same identity on the first
    # request; the user is never sent to a "reconnect" step.
    assert isinstance(dialog, RoleWorkbenchDialog)
    assert dialog.service_bar is None
    assert dialog._service_client is not None
    assert dialog.prepare_button.isEnabled() is True
    assert dialog.support_id_edit.text() == "client-" + "a" * 32
    dialog.close()


def test_configured_origin_opens_an_online_workbench_without_a_binding_step(
    tmp_path: Path, qt_app
) -> None:
    protector = _TestProtector()
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        session_store=RoleServiceSessionStore(tmp_path / "session.json", protector),
        enrollment_store=RoleServiceEnrollmentStore(
            tmp_path / "enrollment.json", protector
        ),
        environ={
            "MOEGUARD_ROLE_SERVICE_MODE": "https",
            "MOEGUARD_ROLE_SERVICE_URL": "https://roles.example",
        },
        watch_network=False,
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    assert dialog._service_client is not None
    assert dialog.service_bar is None
    assert dialog.prepare_button.isEnabled() is True
    assert dialog.support_id_row.isHidden()  # Until the first request enrolls.
    dialog.close()


def test_support_entry_reads_packaged_public_service_origin(
    tmp_path: Path, qt_app
) -> None:
    protector = _TestProtector()
    config_path = tmp_path / "role-service.json"
    config_path.write_text(
        '{"schema_version":1,"service_origin":"https://roles.example"}\n',
        encoding="utf-8",
    )
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={},
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        session_store=RoleServiceSessionStore(tmp_path / "session.json", protector),
        enrollment_store=RoleServiceEnrollmentStore(
            tmp_path / "enrollment.json", protector
        ),
        service_config_path=config_path,
        watch_network=False,
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    assert dialog._service_client is not None
    assert dialog.prepare_button.isEnabled() is True
    assert dialog._service_transport.service_origin == "https://roles.example"
    dialog.close()


def test_preview_data_notice_does_not_open_a_blocking_dialog(
    tmp_path: Path, qt_app, monkeypatch
) -> None:
    app = MoeGuardApp()
    message_boxes: list[QMessageBox] = []
    monkeypatch.setattr(
        QMessageBox,
        "exec",
        lambda message_box: message_boxes.append(message_box) or QDialog.Rejected,
    )
    configure_role_workbench(
        app,
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        service_origin="https://roles.example",
        pilot_notice_enabled=True,
        session_store=_session_store(tmp_path / "session.json"),
        clock=lambda: 1_900_000_000,
    )

    dialog = _factory(app)()

    assert message_boxes == []
    assert dialog._service_client is not None
    assert dialog._client_events is not None
    assert dialog.prepare_button.isEnabled() is True
    dialog.close()


def test_accepted_preview_notice_enables_events_for_bound_service_only(
    tmp_path: Path, qt_app
) -> None:
    class Tracker:
        pass

    app = MoeGuardApp()
    tracker = Tracker()
    configure_role_workbench(
        app,
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        service_origin="https://roles.example",
        pilot_notice_enabled=True,
        session_store=_session_store(tmp_path / "session.json"),
        clock=lambda: 1_900_000_000,
        client_event_tracker=tracker,
    )

    dialog = _factory(app)()

    assert dialog._service_client is not None
    assert dialog._client_events is not None
    assert dialog._client_events.tracker is tracker
    dialog.close()


def test_online_workbench_refreshes_atomic_generation_units_on_demand(
    tmp_path: Path, qt_app
) -> None:
    class AccountSummaryTransport:
        @staticmethod
        def account_summary() -> RoleServiceAccountSummary:
            return RoleServiceAccountSummary(4, 1, 7, 1, 3, 0)

    transport = AccountSummaryTransport()
    dialog = RoleWorkbenchDialog(
        FakeRoleWorkbenchBackend(tmp_path / "sessions"),
        service_transport=transport,
    )

    assert dialog.account_summary_label is not None
    assert dialog.account_summary_label.text() == "生成次数：点击刷新"
    dialog._refresh_account_summary()
    worker = dialog._worker
    assert worker is not None
    assert worker.wait(2000)
    qt_app.processEvents()

    assert dialog.account_summary_label.text() == (
        "立绘生成 1 次 · 动作生成 3 次 · 任务占用 1 次 · 已使用 7 次"
    )
    dialog.close()


def test_workbench_and_settings_offer_no_disconnect(
    tmp_path: Path, qt_app, monkeypatch
) -> None:
    app, _, enrollments = _automatic_app(
        tmp_path, monkeypatch, lambda *_: _automatic_enrollment()
    )
    app.ready.emit()
    _wait_connection(app, qt_app)
    dialog = _factory(app)()
    texts = {button.text() for button in dialog.findChildren(QPushButton)}
    assert not any("断开" in text for text in texts)
    assert not hasattr(dialog, "disconnect_service_button")
    assert enrollments.load() is not None  # The identity is never deleted.
    dialog.close()


def _validate_event(event_name: str, properties: dict[str, object]) -> None:
    import uuid

    from moeguard.cloud.role_service_event_contract import ClientEvent

    ClientEvent.from_dict(
        {
            "event_id": str(uuid.uuid4()),
            "event_name": event_name,
            "client_at_ms": 1,
            "sequence": 0,
            "session_id": str(uuid.uuid4()),
            "journey_id": str(uuid.uuid4()),
            "app_version": "0.4.0",
            "properties": properties,
        }
    )


# -- v0.4.0 app heartbeats ----------------------------------------------------


class _FakeReporter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event_name: str, *, properties: dict[str, object]) -> bool:
        _validate_event(event_name, properties)
        self.events.append((event_name, properties))
        return True


def test_daily_active_is_sent_once_per_day_with_valid_properties(
    tmp_path, qt_app, monkeypatch
) -> None:
    from moeguard.cloud.role_service_client_events import (
        DailyActiveGate,
        RequestLatencyBook,
    )
    from moeguard.config import AppConfig
    from moeguard.role_main import AppTelemetry

    app = MoeGuardApp()
    app._config = AppConfig()
    reporter = _FakeReporter()
    today = ["2026-10-18"]
    gate = DailyActiveGate(tmp_path / "daily.json", today=lambda: today[0])
    latency = RequestLatencyBook()
    telemetry = AppTelemetry(
        app, reporter, gate, latency, role_library=RoleLibrary(tmp_path / "roles")
    )

    telemetry.start()
    telemetry.tick()
    assert [name for name, _p in reporter.events] == ["daily_active"]
    properties = reporter.events[0][1]
    assert properties["install_kind"] == "source"
    assert properties["active_role_kind"] == "builtin"
    assert properties["custom_role_count"] == 0
    assert properties["windows_major"] in {"10", "11", "unknown"}

    latency.observe("tasks", 800, ok=True)
    today[0] = "2026-10-19"
    telemetry.tick()
    assert [name for name, _p in reporter.events][1:] == ["daily_active", "network_sample"]
    telemetry.shutdown()
    assert len(reporter.events) == 3


def test_pilot_build_wires_one_shared_tracker_and_request_timing(
    tmp_path, qt_app, monkeypatch
) -> None:
    from moeguard.cloud import role_service_http_client
    from moeguard.config import AppConfig

    monkeypatch.setattr(
        HttpRoleServiceTransport, "submit_client_events", lambda *_a: (_ for _ in ()).throw(
            RoleServiceConnectionError("service_unreachable")
        )
    )
    monkeypatch.setattr(role_service_http_client, "_request_observer", None)
    monkeypatch.setattr(updater_module, "_feed_observer", None)
    from moeguard.cloud.role_service_client_events import (
        ClientEventOutboxStore,
        PreviewClientEventTracker,
    )

    library = RoleLibrary(tmp_path / "roles")
    outbox = tmp_path / "workbench" / "state" / "client-events.json"
    tracker = PreviewClientEventTracker(
        ClientEventOutboxStore(outbox, protector=_TestProtector()),
        app_version="0.4.0",
        enabled=True,
        clock=lambda: 1_900_000_000,
    )
    app = MoeGuardApp()
    app._config = AppConfig()
    configure_role_workbench(
        app, environ={}, storage_root=tmp_path / "workbench", role_library=library,
        session_store=RoleServiceSessionStore(tmp_path / "s.json", _TestProtector()),
        enrollment_store=RoleServiceEnrollmentStore(tmp_path / "e.json", _TestProtector()),
        service_origin="https://roles.example", clock=lambda: 1_900_000_000,
        watch_network=False, pilot_notice_enabled=True, client_event_tracker=tracker,
    )
    telemetry = app._telemetry
    assert telemetry is not None
    assert role_service_http_client._request_observer == telemetry.latency.observe
    assert updater_module._feed_observer == telemetry.latency.observe
    assert telemetry.reporter.tracker is tracker
    assert telemetry.record_daily_active() is True
    assert outbox.exists()  # Undelivered events wait in the shared outbox.
    assert (tmp_path / "workbench" / "state" / "daily-active.json").exists()
    telemetry.shutdown()
