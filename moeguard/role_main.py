"""v0.2 client entrypoint with an injected HTTPS custom-role workbench."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QMessageBox

from moeguard.app import MoeGuardApp, run
from moeguard.cloud.role_service_binding import (
    RoleServiceBindingManager,
    RoleServiceEnrollmentStore,
)
from moeguard.cloud.role_service_binding_dialog import RoleServiceBindingDialog
from moeguard.cloud.role_service_bootstrap import (
    https_role_service_origin,
    role_service_origin_from_environment,
    role_service_origin_from_file,
    role_service_transport_from_environment,
)
from moeguard.cloud.role_service_session import RoleServiceSessionStore
from moeguard.cloud.role_workbench import (
    RemoteRoleWorkbenchBackend,
    RoleCreditDialog,
    RoleWorkbenchDialog,
)
from moeguard.role_pilot import (
    PILOT_NOTICE_TEXT,
    RolePilotNoticeStore,
)
from moeguard.roles import PackageKey, RoleLibrary
from moeguard.utils.paths import ROLE_WORKBENCH_DIR, resource_path


def configure_role_workbench(
    app: MoeGuardApp,
    *,
    environ: Mapping[str, str] | None = None,
    storage_root: Path | None = None,
    role_library: RoleLibrary | None = None,
    session_store: RoleServiceSessionStore | None = None,
    enrollment_store: RoleServiceEnrollmentStore | None = None,
    service_origin: str | None = None,
    service_config_path: Path | None = None,
    clock: Callable[[], float] = time.time,
    pilot_notice_enabled: bool = False,
    pilot_notice_store: RolePilotNoticeStore | None = None,
    pilot_notice_prompt: Callable[[str], bool] | None = None,
) -> None:
    """Inject the public workbench without embedding provider credentials."""

    workspace = Path(storage_root or ROLE_WORKBENCH_DIR / "service-client")
    library = role_library or RoleLibrary()
    sessions = session_store or RoleServiceSessionStore(
        workspace / "role-service-session.json"
    )
    enrollments = enrollment_store or RoleServiceEnrollmentStore(
        workspace / "role-service-enrollment.json"
    )
    binding = RoleServiceBindingManager(
        sessions,
        enrollments,
        clock=clock,
    )
    refresh_account_on_next_open = False
    notice_store = pilot_notice_store or RolePilotNoticeStore(
        workspace / "pilot-notice.json"
    )

    def confirm_pilot_notice() -> bool:
        if not pilot_notice_enabled or notice_store.accepted():
            return True
        if pilot_notice_prompt is not None:
            accepted = bool(pilot_notice_prompt(PILOT_NOTICE_TEXT))
        else:
            notice = QMessageBox()
            notice.setIcon(QMessageBox.Information)
            notice.setWindowTitle("桌宠工坊内测说明 · 萌卫")
            notice.setText(PILOT_NOTICE_TEXT)
            accept_button = notice.addButton(
                "参加内测并继续", QMessageBox.AcceptRole
            )
            notice.addButton("暂不使用", QMessageBox.RejectRole)
            notice.setDefaultButton(accept_button)
            notice.exec()
            accepted = notice.clickedButton() is accept_button
        if accepted:
            notice_store.accept()
        return accepted
    try:
        if service_origin is not None:
            configured_origin = https_role_service_origin(service_origin)
        else:
            configured_origin = role_service_origin_from_file(
                service_config_path or resource_path("role-service.json")
            )
            if configured_origin is None:
                configured_origin = role_service_origin_from_environment(environ)
    except ValueError:
        configured_origin = None

    def make_binding_dialog(
        origin: str | None = None,
        *,
        reopen: Callable[[], None] | None = None,
    ) -> RoleServiceBindingDialog:
        target_origin = origin or configured_origin
        assert target_origin is not None
        dialog = RoleServiceBindingDialog(binding, target_origin)

        def reopen_bound_destination() -> None:
            nonlocal refresh_account_on_next_open
            destination = reopen or app._on_open_custom_role_workbench
            if destination == app._on_open_custom_role_workbench:
                refresh_account_on_next_open = True
            QTimer.singleShot(0, destination)

        dialog.accepted.connect(reopen_bound_destination)
        return dialog

    def make_credit_dialog() -> QDialog:
        remembered_origin = None
        unavailable_message = ""
        try:
            stored = sessions.load()
            if stored is not None:
                remembered_origin = stored.service_origin
            session = binding.current_session()
            transport = (
                session.transport()
                if session is not None
                else role_service_transport_from_environment(environ)
            )
        except (OSError, ValueError):
            session = None
            transport = None
            unavailable_message = (
                "生成服务连接信息无效；请重新连接后再查看或兑换生成次数。"
            )

        if transport is not None:
            return RoleCreditDialog(transport)

        binding_origin = configured_origin or remembered_origin
        if binding_origin is not None:
            return make_binding_dialog(
                binding_origin,
                reopen=app._on_open_role_credit_dialog,
            )
        return RoleCreditDialog(
            None,
            unavailable_message=(
                unavailable_message
                or "此候选版尚未配置生成服务地址，暂时无法查看或兑换生成次数。"
            ),
        )

    def make_workbench():
        nonlocal refresh_account_on_next_open
        refresh_account = refresh_account_on_next_open
        refresh_account_on_next_open = False
        session = None
        remembered_origin = None
        unavailable_message = ""
        try:
            session = sessions.load()
            if session is not None:
                remembered_origin = session.service_origin
            if session is not None and session.expires_at <= int(clock()):
                unavailable_message = (
                    "生成服务连接已过期；本地桌宠仍可查看和管理。"
                    "重新连接后可继续生成。"
                )
                session = None
            transport = (
                session.transport()
                if session is not None
                else role_service_transport_from_environment(environ)
            )
        except (OSError, ValueError):
            transport = None
            unavailable_message = (
                "生成服务连接信息无效；本地桌宠仍可查看和管理。"
                "重新连接后可继续生成。"
            )
        binding_origin = configured_origin or remembered_origin
        pilot_accepted = binding_origin is None or confirm_pilot_notice()
        if not pilot_accepted:
            transport = None
            binding_origin = None
            unavailable_message = (
                "你已选择暂不参加本轮桌宠工坊内测；本地桌宠仍可查看和管理。"
            )
        generation_available = transport is not None
        if not generation_available and not unavailable_message:
            unavailable_message = (
                "尚未连接生成服务；本地桌宠仍可查看和管理。"
                if binding_origin is not None
                else "此候选版尚未配置生成服务地址；本地桌宠仍可查看和管理。"
            )
        dialog = RoleWorkbenchDialog(
            RemoteRoleWorkbenchBackend(workspace),
            show_costs=False,
            role_library=library,
            service_transport=transport,
            generation_available=generation_available,
            generation_unavailable_message=unavailable_message,
            binding_available=binding_origin is not None,
            service_unbinding_available=session is not None,
        )

        if binding_origin is not None and not generation_available:
            def begin_binding() -> None:
                dialog.accept()
                make_binding_dialog(binding_origin).exec()

            dialog.binding_requested.connect(begin_binding)

        if session is not None:
            def remove_binding() -> None:
                answer = QMessageBox.warning(
                    dialog,
                    "断开角色生成服务？",
                    "这会删除这台电脑保存的匿名连接凭据。已安装桌宠不受影响，"
                    "但这个匿名账号的剩余生成次数和未完成任务将无法在本机找回。\n\n"
                    "是否继续？",
                    QMessageBox.Yes | QMessageBox.Cancel,
                    QMessageBox.Cancel,
                )
                if answer != QMessageBox.Yes:
                    return
                try:
                    binding.unbind()
                except (OSError, ValueError):
                    QMessageBox.critical(
                        dialog,
                        "无法断开生成服务",
                        "本机连接凭据未能完整删除。请重启萌卫检查连接状态后再试。",
                    )
                    return
                dialog.accept()
                QTimer.singleShot(0, app._on_open_custom_role_workbench)

            dialog.unbinding_requested.connect(remove_binding)

        def install(key: object) -> None:
            if not isinstance(key, PackageKey):
                QMessageBox.critical(dialog, "角色切换失败", "工作台返回了无效角色版本。")
                return
            success, message = app.activate_managed_role(key, role_library=library)
            if success:
                QMessageBox.information(dialog, "角色已切换", message)
            else:
                QMessageBox.critical(dialog, "角色切换失败", message)

        dialog.install_requested.connect(install)
        if refresh_account and session is not None:
            QTimer.singleShot(
                0, lambda: dialog._refresh_account_summary(quiet=True)
            )
        return dialog

    app.set_custom_role_workbench_factory(make_workbench)
    app.set_role_credit_dialog_factory(make_credit_dialog)


def main() -> int:
    return run(
        sys.argv,
        configure=lambda app: configure_role_workbench(
            app,
            pilot_notice_enabled=True,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
