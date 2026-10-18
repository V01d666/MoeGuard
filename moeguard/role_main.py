"""v0.2 client entrypoint with an injected HTTPS custom-role workbench."""

from __future__ import annotations

import sys

# Velopack's install/update hooks start this executable with hook arguments and
# expect it to exit from inside run(); nothing heavier may load before that.
# A no-op for source runs and for builds without the velopack wheel.
if __name__ == "__main__":
    from moeguard.updater import run_velopack_startup

    run_velopack_startup()

import time  # noqa: E402
from collections.abc import Callable, Mapping  # noqa: E402
from pathlib import Path  # noqa: E402

from PySide6.QtCore import QObject, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from moeguard import __version__, updater  # noqa: E402
from moeguard.app import MoeGuardApp, run  # noqa: E402
from moeguard.cloud import role_service_http_client  # noqa: E402
from moeguard.cloud.role_service_binding import (  # noqa: E402
    RoleServiceBindingManager,
    RoleServiceEnrollmentStore,
    SessionTransport,
)
from moeguard.cloud.role_service_bootstrap import (  # noqa: E402
    https_role_service_origin,
    role_service_origin_from_environment,
    role_service_origin_from_file,
    role_service_transport_from_environment,
)
from moeguard.cloud.role_service_client_events import (  # noqa: E402
    ClientEventOutboxStore,
    DailyActiveGate,
    PreviewClientEventReporter,
    PreviewClientEventTracker,
    RequestLatencyBook,
)
from moeguard.cloud.role_service_connection import ServiceConnection  # noqa: E402
from moeguard.cloud.role_service_http_client import HttpRoleServiceTransport  # noqa: E402
from moeguard.cloud.role_service_mailbox_client import (  # noqa: E402
    MailboxReadStore,
    mailbox_diagnostics,
)
from moeguard.cloud.role_service_mailbox_dialog import MailboxDialog  # noqa: E402
from moeguard.cloud.role_service_session import RoleServiceSessionStore  # noqa: E402
from moeguard.cloud.role_workbench import (  # noqa: E402
    RemoteRoleWorkbenchBackend,
    RoleCreditDialog,
    RoleWorkbenchDialog,
    _client_runtime_info,
)
from moeguard.roles import PackageKey, RoleLibrary  # noqa: E402
from moeguard.utils.paths import ROLE_WORKBENCH_DIR, resource_path  # noqa: E402

# How often latency samples are sent and the day boundary is re-checked.
TELEMETRY_PERIOD_MS = 30 * 60 * 1000


class AppTelemetry(QObject):
    """App-wide v0.4.0 heartbeats that do not belong to the workshop.

    ``daily_active`` (at most one per local day) answers whether the pet is
    still on the desktop -- with a valid stored session v0.3.0 started
    without any request, so nothing showed an installed copy was in use.
    ``network_sample`` reports client-measured request latency per route
    family. Both carry only enums and numbers, never names, paths or text,
    and neither ever raises into the app.
    """

    def __init__(
        self,
        app: MoeGuardApp,
        reporter: PreviewClientEventReporter,
        gate: DailyActiveGate,
        latency: RequestLatencyBook,
        *,
        role_library: RoleLibrary,
        period_ms: int = TELEMETRY_PERIOD_MS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(app)
        self._app = app
        self.reporter = reporter
        self.gate = gate
        self.latency = latency
        self._library = role_library
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(period_ms)
        self._timer.timeout.connect(self.tick)

    def start(self) -> None:
        self.record_daily_active()
        self._timer.start()

    def tick(self) -> None:
        self.record_daily_active()
        self.record_network_samples()

    def shutdown(self) -> None:
        self._timer.stop()
        self.record_network_samples()

    def _record(self, event_name: str, properties: dict[str, object]) -> bool:
        try:
            return bool(
                self.reporter.record(
                    event_name,
                    properties={"screen": "runtime", "entrypoint": "startup", **properties},
                )
            )
        except Exception:  # noqa: BLE001 - telemetry never affects the app
            return False

    def _role_snapshot(self) -> dict[str, object]:
        config = getattr(self._app, "_config", None)
        pet = getattr(config, "pet", None)
        if pet is None:
            kind = "none"
        elif getattr(pet, "role_package_version", 0) > 0 or getattr(pet, "assets_dir", ""):
            kind = "custom"
        else:
            kind = "builtin"
        snapshot: dict[str, object] = {"active_role_kind": kind}
        try:
            roles = {installed.key.role_id for installed in self._library.list()}
            snapshot["custom_role_count"] = min(1000, len(roles))
        except Exception:  # noqa: BLE001 - a broken library only drops the count
            pass
        return snapshot

    def record_daily_active(self) -> bool:
        try:
            if not self.gate.due():
                return False
            runtime = _client_runtime_info()
            properties: dict[str, object] = {
                "install_kind": updater.install_kind(),
                "uptime_minutes": max(
                    0, min(7 * 24 * 60, int((self._monotonic() - self._started_at) // 60))
                ),
                "windows_major": (
                    runtime.windows_major
                    if runtime.windows_major in {"10", "11"}
                    else "unknown"
                ),
                "architecture": runtime.architecture,
                "display_scale_percent": runtime.display_scale_percent,
                **self._role_snapshot(),
            }
        except Exception:  # noqa: BLE001
            return False
        if not self._record("daily_active", properties):
            return False
        self.gate.mark()
        return True

    def record_network_samples(self) -> int:
        try:
            samples = self.latency.drain()
        except Exception:  # noqa: BLE001
            return 0
        return sum(1 for sample in samples if self._record("network_sample", sample))


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
    client_event_tracker: PreviewClientEventTracker | None = None,
    watch_network: bool = True,
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
    event_reporter: PreviewClientEventReporter | None = None
    shared_tracker: PreviewClientEventTracker | None = client_event_tracker

    def event_tracker() -> PreviewClientEventTracker:
        # One tracker (one outbox file and one sequence) for every reporter.
        nonlocal shared_tracker
        if shared_tracker is None:
            shared_tracker = PreviewClientEventTracker(
                ClientEventOutboxStore(workspace / "state" / "client-events.json"),
                app_version=__version__,
                enabled=lambda: pilot_notice_enabled,
                clock=clock,
            )
        return shared_tracker

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

    # One device session for every surface.  It connects on demand, renews a
    # rejected token with the same identity, and keeps the support ID forever;
    # there is no connect or disconnect step for the user.
    connection = (
        ServiceConnection(
            SessionTransport(binding, configured_origin),
            app,
            watch_network=watch_network,
        )
        if configured_origin is not None
        else None
    )
    app._service_connection = connection
    if connection is not None:
        app.ready.connect(connection.start)

    app_reporter: PreviewClientEventReporter | None = None
    app._telemetry = None
    if connection is not None and pilot_notice_enabled:
        app_reporter = PreviewClientEventReporter(event_tracker(), connection.transport)
        latency = RequestLatencyBook()
        telemetry = AppTelemetry(
            app,
            app_reporter,
            DailyActiveGate(workspace / "state" / "daily-active.json"),
            latency,
            role_library=library,
        )
        app._telemetry = telemetry
        role_service_http_client.set_request_observer(latency.observe)
        updater.set_feed_observer(latency.observe)
        # After the connection starts, so the first heartbeat reuses the
        # session that startup request establishes.
        app.ready.connect(telemetry.start)
        qt_app = QApplication.instance()
        if qt_app is not None:
            qt_app.aboutToQuit.connect(telemetry.shutdown)

    def stored_account_id() -> str:
        if connection is not None and connection.account_id:
            return connection.account_id
        try:
            session = sessions.load()
        except (OSError, ValueError):
            return ""
        return session.account_id if session is not None else ""

    def developer_transport() -> tuple[object | None, bool]:
        """An explicit developer token wins; a broken one is reported."""
        try:
            return role_service_transport_from_environment(environ), False
        except ValueError:
            pass
        # A service URL without a token only names the origin, which the
        # device session already uses; anything else is a misconfiguration.
        try:
            origin_only = role_service_origin_from_environment(environ) is not None
        except ValueError:
            origin_only = False
        return None, not (origin_only and connection is not None)

    def make_credit_dialog() -> QDialog:
        transport, invalid = developer_transport()
        if transport is not None:
            return RoleCreditDialog(transport, account_id=stored_account_id())
        if connection is not None and not invalid:
            return RoleCreditDialog(
                connection.transport,
                account_id=stored_account_id(),
                connection=connection,
            )
        return RoleCreditDialog(
            None,
            unavailable_message=(
                "生成服务连接信息无效，暂时无法查看或兑换生成次数。"
                if invalid
                else "此候选版尚未配置生成服务地址，暂时无法查看或兑换生成次数。"
            ),
        )

    def make_workbench():
        nonlocal event_reporter
        transport, invalid = developer_transport()
        workbench_connection = None
        if transport is None and connection is not None and not invalid:
            transport = connection.transport
            workbench_connection = connection
        generation_available = transport is not None
        unavailable_message = ""
        if not generation_available:
            unavailable_message = (
                "生成服务连接信息无效；本地桌宠仍可查看和管理。"
                if invalid
                else "此候选版尚未配置生成服务地址；本地桌宠仍可查看和管理。"
            )
        if (
            transport is not None
            and pilot_notice_enabled
            and event_reporter is None
        ):
            if workbench_connection is not None and app_reporter is not None:
                event_reporter = app_reporter
            else:
                event_reporter = PreviewClientEventReporter(event_tracker(), transport)
        dialog = RoleWorkbenchDialog(
            RemoteRoleWorkbenchBackend(workspace),
            show_costs=False,
            role_library=library,
            service_transport=transport,
            generation_available=generation_available,
            generation_unavailable_message=unavailable_message,
            client_events=event_reporter if transport is not None else None,
            client_event_entrypoint="settings",
            account_id=stored_account_id() if transport is not None else "",
            notify_user=app.notify_workbench_result,
            announce_wait=app.announce_workbench_wait,
            connection=workbench_connection,
        )

        def install(key: object) -> None:
            if not isinstance(key, PackageKey):
                QMessageBox.critical(dialog, "角色切换失败", "工作台返回了无效角色版本。")
                return
            success, message = app.activate_managed_role(key, role_library=library)
            if success:
                dialog.record_role_activated()
                QMessageBox.information(dialog, "角色已切换", message)
            else:
                QMessageBox.critical(dialog, "角色切换失败", message)

        dialog.install_requested.connect(install)
        return dialog

    app.set_custom_role_workbench_factory(make_workbench)
    app.set_role_credit_dialog_factory(make_credit_dialog)

    def make_mailbox() -> QDialog:
        origin = configured_origin
        public = HttpRoleServiceTransport(origin, "") if origin else None
        # Read markers are keyed by the support ID, so the private thread is
        # attached once the ID is known; until then announcements still load
        # and the shared connection attaches it the moment it connects.
        account_id = stored_account_id()
        transport = (
            connection.transport if connection is not None and account_id else None
        )
        if connection is not None and not account_id:
            connection.ensure_fresh()

        def diagnostics():
            tracker = shared_tracker
            codes = ()
            if tracker is not None and hasattr(tracker, "recent_error_codes"):
                codes = tracker.recent_error_codes()
            return mailbox_diagnostics(codes)

        dialog = MailboxDialog(
            public, transport,
            MailboxReadStore(workspace / "state" / "mailbox-read.json", clock=clock),
            account_id=account_id if transport is not None else "",
            diagnostics=diagnostics, clock=clock,
        )
        return dialog

    app._support_id_provider = stored_account_id
    app._mailbox_dialog_factory = make_mailbox
    if connection is not None:
        def update_open_mailbox(session: object) -> None:
            dialog = app._mailbox_dialog
            account_id = getattr(session, "account_id", "")
            if isinstance(dialog, MailboxDialog) and account_id:
                dialog.set_connection(connection.transport, account_id)

        connection.connected.connect(update_open_mailbox)


def main() -> int:
    def configure(app: MoeGuardApp) -> None:
        configure_role_workbench(app, pilot_notice_enabled=True)
        # Update-enabled builds embed a public source; ordinary builds have
        # no source. MOEGUARD_UPDATE_FEED remains an explicit experiment override.
        from moeguard.update_controller import configure_self_update

        app._update_controller = configure_self_update(app)

    return run(sys.argv, configure=configure)


if __name__ == "__main__":
    raise SystemExit(main())
