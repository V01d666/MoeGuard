from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from moeguard.app import MoeGuardApp
from moeguard.cloud.role_service_binding import (
    RoleServiceBindingManager,
    RoleServiceEnrollmentIdentity,
    RoleServiceEnrollmentStore,
)
from moeguard.cloud.role_service_binding_dialog import RoleServiceBindingDialog
from moeguard.cloud.role_service_http_client import (
    HttpRoleServiceTransport,
    RoleServiceAccountSummary,
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
from moeguard.role_pilot import RolePilotNoticeStore
from moeguard.roles import RoleLibrary


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
    assert dialog.bind_service_button is None
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
    dialog.close()

    credit_dialog = _credit_factory(app)()
    assert isinstance(credit_dialog, RoleCreditDialog)
    assert credit_dialog._service_transport is not None
    credit_dialog.close()


def test_support_entry_requires_rebind_for_expired_protected_session(
    tmp_path: Path, qt_app
) -> None:
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={},
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        session_store=_session_store(tmp_path / "session.json", expires_at=100),
        clock=lambda: 101,
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    notice = "".join(label.text() for label in dialog.service_bar.findChildren(QLabel))
    assert "连接已过期" in notice
    assert "roles.example" not in notice
    assert dialog.bind_service_button is not None
    assert dialog.bind_service_button.text() == "连接生成服务"
    dialog.close()


def test_support_entry_offers_explicit_anonymous_binding_when_origin_is_configured(
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
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    assert dialog._service_client is None
    assert dialog.bind_service_button is not None
    assert dialog.bind_service_button.text() == "连接生成服务"
    assert dialog.prepare_button.isEnabled() is False
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
    )

    dialog = _factory(app)()

    assert isinstance(dialog, RoleWorkbenchDialog)
    assert dialog.bind_service_button is not None
    assert dialog.prepare_button.isEnabled() is False
    dialog.close()


def test_preview_notice_is_required_once_before_real_service_use(
    tmp_path: Path, qt_app
) -> None:
    app = MoeGuardApp()
    prompts: list[str] = []
    store = RolePilotNoticeStore(tmp_path / "pilot-notice.json")
    configure_role_workbench(
        app,
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        service_origin="https://roles.example",
        pilot_notice_enabled=True,
        pilot_notice_store=store,
        pilot_notice_prompt=lambda text: prompts.append(text) or True,
    )

    first = _factory(app)()
    second = _factory(app)()

    assert len(prompts) == 1
    assert "最长 30 天" in prompts[0]
    assert store.accepted() is True
    first.close()
    second.close()


def test_preview_notice_decline_keeps_local_management_available(
    tmp_path: Path, qt_app
) -> None:
    app = MoeGuardApp()
    store = RolePilotNoticeStore(tmp_path / "pilot-notice.json")
    configure_role_workbench(
        app,
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        service_origin="https://roles.example",
        pilot_notice_enabled=True,
        pilot_notice_store=store,
        pilot_notice_prompt=lambda _text: False,
    )

    dialog = _factory(app)()

    assert dialog._service_client is None
    assert dialog.prepare_button.isEnabled() is False
    assert "暂不参加" in "".join(
        label.text() for label in dialog.service_bar.findChildren(QLabel)
    )
    assert store.accepted() is False
    dialog.close()


def test_successful_explicit_binding_refreshes_units_on_reopened_workbench(
    tmp_path: Path, qt_app, monkeypatch
) -> None:
    protector = _TestProtector()
    session_store = RoleServiceSessionStore(tmp_path / "session.json", protector)
    enrollment_store = RoleServiceEnrollmentStore(
        tmp_path / "enrollment.json", protector
    )
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={},
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        session_store=session_store,
        enrollment_store=enrollment_store,
        service_origin="https://roles.example",
        clock=lambda: 1_900_000_000,
    )
    session = RoleServiceSession(
        service_origin="https://roles.example",
        account_id="client-" + "a" * 32,
        expires_at=2_000_000_000,
        bearer_token="mgr_" + "b" * 24 + "_" + "C" * 43,
        enrollment_id="eni_" + "d" * 32,
        enrollment_secret="ens_" + "E" * 43,
    )

    def accept_binding(binding_dialog: RoleServiceBindingDialog) -> int:
        session_store.save(session)
        binding_dialog.session = session
        binding_dialog.accept()
        return QDialog.Accepted

    monkeypatch.setattr(RoleServiceBindingDialog, "exec", accept_binding)
    monkeypatch.setattr(
        HttpRoleServiceTransport,
        "account_summary",
        lambda _transport: RoleServiceAccountSummary(6, 0, 1, 2, 4, 0),
    )
    reopened: list[RoleWorkbenchDialog] = []
    monkeypatch.setattr(
        app,
        "_on_open_custom_role_workbench",
        lambda: reopened.append(_factory(app)()),
    )

    offline = _factory(app)()
    assert offline.bind_service_button is not None
    offline.bind_service_button.click()
    for _ in range(4):
        qt_app.processEvents()

    assert len(reopened) == 1
    online = reopened[0]
    worker = online._worker
    assert worker is not None
    assert worker.wait(2000)
    qt_app.processEvents()

    assert online.account_summary_label is not None
    assert online.account_summary_label.text() == (
        "立绘生成 2 次 · 动作生成 4 次 · 任务占用 0 次 · 已使用 1 次"
    )
    online.close()


def test_anonymous_binding_dialog_runs_network_exchange_off_the_ui_thread(
    tmp_path: Path, qt_app
) -> None:
    protector = _TestProtector()
    manager = RoleServiceBindingManager(
        RoleServiceSessionStore(tmp_path / "session.json", protector),
        RoleServiceEnrollmentStore(tmp_path / "enrollment.json", protector),
        enroll=lambda *_args: RoleServiceEnrollment(
            account_id="client-" + "a" * 32,
            bearer_token="mgr_" + "b" * 24 + "_" + "C" * 43,
            expires_at=2_000_000_000,
        ),
        clock=lambda: 1_900_000_000,
    )
    dialog = RoleServiceBindingDialog(manager, "https://roles.example")

    dialog._start_binding()
    thread = dialog._thread
    assert thread is not None
    assert thread.wait(2000)
    qt_app.processEvents()

    assert dialog.session is not None
    assert dialog.result() == QDialog.Accepted


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
    assert dialog.disconnect_service_button is None
    dialog._refresh_account_summary()
    worker = dialog._worker
    assert worker is not None
    assert worker.wait(2000)
    qt_app.processEvents()

    assert dialog.account_summary_label.text() == (
        "立绘生成 1 次 · 动作生成 3 次 · 任务占用 1 次 · 已使用 7 次"
    )
    dialog.close()


def test_online_workbench_can_delete_local_anonymous_binding(
    tmp_path: Path, qt_app, monkeypatch
) -> None:
    protector = _TestProtector()
    session_store = _session_store(tmp_path / "session.json")
    enrollment_store = RoleServiceEnrollmentStore(
        tmp_path / "enrollment.json", protector
    )
    enrollment_store.save(
        RoleServiceEnrollmentIdentity(
            "https://roles.example",
            "eni_" + "d" * 32,
            "ens_" + "E" * 43,
        )
    )
    app = MoeGuardApp()
    configure_role_workbench(
        app,
        environ={},
        storage_root=tmp_path / "workbench",
        role_library=RoleLibrary(tmp_path / "roles"),
        session_store=session_store,
        enrollment_store=enrollment_store,
        service_origin="https://roles.example",
        clock=lambda: 1_900_000_000,
    )
    reopened: list[bool] = []
    monkeypatch.setattr(
        app, "_on_open_custom_role_workbench", lambda: reopened.append(True)
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: QMessageBox.Yes)

    dialog = _factory(app)()

    assert dialog.disconnect_service_button is not None
    dialog.disconnect_service_button.click()
    qt_app.processEvents()

    assert session_store.load() is None
    assert enrollment_store.load() is None
    assert reopened == [True]

    offline = _factory(app)()
    assert offline.bind_service_button is not None
    assert offline.prepare_button.isEnabled() is False
    offline.close()
