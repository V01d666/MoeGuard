"""Client-only mailbox tests: no production connections or content in logs."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from moeguard.cloud.role_service_http_client import HttpRoleServiceTransport, RoleServiceHttpError
from moeguard.cloud.role_service_mailbox_client import (
    MAX_READ_MARKERS,
    READ_MARKER_RETENTION_SECONDS,
    MailboxReadStore,
    mailbox_diagnostics,
    read_fingerprint,
)
from moeguard.cloud.role_service_mailbox_contract import (
    Announcement,
    MailboxDiagnostics,
    MailboxEntry,
    MailboxMessageRequest,
    MailboxPostReceipt,
    MailboxThread,
)
from moeguard.cloud.role_service_mailbox_dialog import MailboxDialog


def _wait(dialog: MailboxDialog) -> None:
    deadline = time.monotonic() + 5
    while dialog._job is not None and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.005)
    assert dialog._job is None


class _Transport:
    def __init__(self):
        self.sent = []
        self.fail_posts = 0

    def announcements(self):
        return (
            Announcement("ma_a", "test notice", "<b>plain text</b>", 1, 2000),
            Announcement("ma_b", "expired", "test", 1, 2),
            Announcement("ma_c", "other version", "test", 1, 2000, "99.0.0"),
        )

    def mailbox_thread(self):
        return MailboxThread(
            (
                MailboxEntry("mm_a", "operator", "<b>plain reply</b>", 10),
                MailboxEntry("mm_b", "user", "test", 11),
            ),
            500,
            5,
            4,
        )

    def post_mailbox_message(self, request):
        self.sent.append(request)
        if self.fail_posts:
            self.fail_posts -= 1
            raise OSError("untrusted detail must never be reflected")
        return MailboxPostReceipt("mm_c", 12, 3)


def _dialog(tmp_path: Path, **kwargs) -> tuple[MailboxDialog, _Transport]:
    transport = _Transport()
    dialog = MailboxDialog(
        transport,
        transport,
        MailboxReadStore(tmp_path / "read.json"),
        account_id="test-account",
        clock=lambda: 1000,
        auto_refresh=False,
        **kwargs,
    )
    return dialog, transport


def test_mailbox_transport_uses_existing_routes_and_minimal_payload(monkeypatch):
    calls = []

    def request(self, method, route, **kwargs):
        calls.append((method, route, kwargs))
        if route == "/v1/announcements":
            data = {"announcements": []}
        elif method == "GET":
            data = {
                "messages": [],
                "limits": {"max_body_chars": 500, "daily_limit": 5, "remaining_today": 5},
            }
        else:
            data = {"message_id": "mm_test", "created_at": 1, "remaining_today": 4}
        return json.dumps({"schema_version": 1, "data": data, "future": True}).encode(), {}

    monkeypatch.setattr(HttpRoleServiceTransport, "_request", request)
    transport = HttpRoleServiceTransport("https://roles.example", "test-token")
    assert transport.announcements() == ()
    assert transport.mailbox_thread().remaining_today == 5
    assert (
        transport.post_mailbox_message(
            MailboxMessageRequest("cm_" + "a" * 32, "test")
        ).remaining_today
        == 4
    )
    assert calls[0][2]["authenticated"] is False
    assert "authenticated" not in calls[1][2]
    assert [call[:2] for call in calls] == [
        ("GET", "/v1/announcements"),
        ("GET", "/v1/mailbox/messages"),
        ("POST", "/v1/mailbox/messages"),
    ]
    raw = json.loads(calls[2][2]["body"])
    assert set(raw) == {"schema_version", "client_message_id", "body"}
    assert len(raw["body"]) == 4


def test_cancelled_preview_never_sends_and_diagnostics_default_off(tmp_path):
    previews = []
    dialog, transport = _dialog(
        tmp_path, confirm_send=lambda request: previews.append(request) or False
    )
    dialog.composer.setPlainText("test")
    assert not dialog.attach_diagnostics.isChecked()
    dialog.send_button.click()
    assert transport.sent == [] and len(previews) == 1
    assert previews[0].diagnostics is None
    assert not (tmp_path / "read.json").exists()
    dialog.close()


def test_opt_in_diagnostics_preview_send_and_retry_keep_same_id(tmp_path):
    diagnostics = MailboxDiagnostics("0.3.0", "11", "x86_64", ("network_unavailable",))
    previews = []
    dialog, transport = _dialog(
        tmp_path,
        diagnostics=lambda: diagnostics,
        confirm_send=lambda request: previews.append(request) or True,
    )
    transport.fail_posts = 1
    dialog.composer.setPlainText("test")
    dialog.attach_diagnostics.setChecked(True)
    dialog.send_message()
    _wait(dialog)
    assert len(transport.sent) == 1 and dialog.composer.toPlainText()
    assert "untrusted" not in dialog.status.text()
    dialog.send_message()
    _wait(dialog)
    assert len(transport.sent) == 2
    assert transport.sent[0].client_message_id == transport.sent[1].client_message_id
    assert all(request.diagnostics == diagnostics for request in previews)
    assert dialog.composer.toPlainText() == "" and not dialog.attach_diagnostics.isChecked()
    assert dialog.send_button.isEnabled() is False
    dialog.close()


def test_unread_announcements_filtering_plain_text_and_restart(tmp_path):
    dialog, _ = _dialog(tmp_path)
    unread_counts = []
    dialog.unread_changed.connect(unread_counts.append)
    dialog.refresh()
    _wait(dialog)
    assert dialog.inbox.count() == 2 and dialog.announcements.count() == 1
    assert dialog.tabs.tabText(0).endswith("(2)")
    assert dialog.tabs.tabText(1).endswith("(1)")
    assert unread_counts[-1] == 2
    dialog.inbox.setCurrentRow(0)
    assert dialog.detail.toPlainText().startswith("<b>")
    assert dialog.tabs.tabText(0).endswith("(2)")
    assert unread_counts[-1] == 1
    raw = (tmp_path / "read.json").read_bytes()
    assert b"plain reply" not in raw and b"test-account" not in raw and b"mm_a" not in raw
    dialog.close()
    restarted, _ = _dialog(tmp_path)
    restarted.refresh()
    _wait(restarted)
    assert restarted.tabs.tabText(0).endswith("(2)")
    assert restarted.tabs.tabText(1).endswith("(1)")
    restarted.mark_all_button.click()
    assert restarted.tabs.tabText(1).endswith("(1)")
    assert all(not restarted.inbox.item(i).text().startswith("[未读]")
               for i in range(restarted.inbox.count()))
    restarted.clear_read_button.click()
    assert not (tmp_path / "read.json").exists()
    assert restarted.tabs.tabText(0).endswith("(2)")
    restarted.close()


def test_connection_arriving_during_public_refresh_loads_the_authenticated_thread(tmp_path):
    release = threading.Event()

    class SlowPublic(_Transport):
        def announcements(self):
            assert release.wait(5)
            return super().announcements()

    authenticated = _Transport()
    dialog = MailboxDialog(SlowPublic(), None, MailboxReadStore(tmp_path / "read.json"),
                           clock=lambda: 1000)
    dialog.set_connection(authenticated, "test-account")
    assert dialog._refresh_after_connection
    release.set()
    deadline = time.monotonic() + 5
    while dialog.inbox.count() != 2 and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(.005)
    _wait(dialog)
    assert dialog.inbox.count() == 2 and dialog.tabs.tabText(0).endswith("(2)")
    dialog.composer.setPlainText("test")
    assert dialog.send_button.isEnabled()
    dialog.close()


def test_unbound_user_can_read_announcements_without_sending(tmp_path):
    public = _Transport()
    dialog = MailboxDialog(
        public,
        None,
        MailboxReadStore(tmp_path / "read.json"),
        clock=lambda: 1000,
    )
    _wait(dialog)
    assert dialog.announcements.count() == 1 and dialog.inbox.count() == 0
    dialog.composer.setPlainText("test")
    assert not dialog.send_button.isEnabled()
    dialog.send_message()
    assert public.sent == []
    dialog.close()


def test_unconfigured_mailbox_does_not_claim_success_or_start_a_request(tmp_path):
    dialog = MailboxDialog(None, None, MailboxReadStore(tmp_path / "read.json"))
    assert not dialog.refresh_button.isEnabled() and dialog._job is None
    dialog.refresh()
    assert dialog._job is None and not dialog.send_button.isEnabled()
    assert "尚未配置" in dialog.status.text()
    dialog.close()


@pytest.mark.parametrize("status", [401, 404, 429, 500])
def test_http_failures_stay_local_and_do_not_echo_details(tmp_path, status):
    dialog, transport = _dialog(tmp_path)

    def unavailable():
        raise RoleServiceHttpError(status, "test", "untrusted-body-token-path")

    transport.mailbox_thread = unavailable
    dialog.refresh()
    _wait(dialog)
    assert "untrusted" not in dialog.status.text()
    assert dialog.announcements.count() == 1
    assert dialog.refresh_button.isEnabled()
    dialog.close()


def test_invalid_text_or_oversized_diagnostics_do_not_reach_network(tmp_path):
    dialog, transport = _dialog(tmp_path, confirm_send=lambda _: True)
    for text in ("", "a" * 501, "a\x00b"):
        dialog.composer.setPlainText(text)
        dialog.send_message()
    assert transport.sent == []
    dialog.close()


def test_read_markers_have_retention_capacity_and_cleanup(tmp_path):
    now = [1_800_000_000]
    store = MailboxReadStore(tmp_path / "read.json", clock=lambda: now[0])
    fingerprints = [read_fingerprint("account", str(index), 1) for index in range(250)]
    store.mark_read(fingerprints)
    assert len(store.seen()) == MAX_READ_MARKERS
    now[0] += READ_MARKER_RETENTION_SECONDS + 1
    assert not store.seen() and not store.path.exists()
    store.mark_read((fingerprints[0],))
    store.clear()
    assert not store.path.exists()
    assert read_fingerprint("other-account", "0", 1) != fingerprints[0]


@pytest.mark.parametrize(
    "raw",
    [
        {"schema_version": 1, "seen": {}, "body": "unwanted"},
        {"schema_version": True, "seen": {}},
        {"schema_version": 1, "seen": {"a" * 64: True}},
        {"schema_version": 1, "seen": {"not-a-fingerprint": 1}},
    ],
)
def test_read_marker_privacy_gate_and_corrupt_file_recovery(tmp_path, raw):
    path = tmp_path / "read.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    store = MailboxReadStore(path)
    with pytest.raises(ValueError):
        store.seen()
    store.clear()
    assert not path.exists()


def test_diagnostics_never_collect_full_system_identity(monkeypatch):
    import moeguard.cloud.role_service_mailbox_client as client

    monkeypatch.setattr(client.platform, "release", lambda: "11")
    monkeypatch.setattr(client.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(client, "installed_version", lambda: "0.3.0")
    data = mailbox_diagnostics(("network_unavailable",)).to_dict()
    assert set(data) == {"app_version", "windows_major", "architecture", "recent_error_codes"}
    assert data["windows_major"] == "11" and data["architecture"] == "x86_64"
    with pytest.raises(ValueError):
        mailbox_diagnostics(("invalid/path",))


def test_recent_error_diagnostics_reject_free_form_fields_and_stay_bounded():
    from types import SimpleNamespace

    from moeguard.cloud.role_service_client_events import PreviewClientEventTracker

    tracker = PreviewClientEventTracker(
        SimpleNamespace(enqueue=lambda event: True), app_version="0.2.0", enabled=True
    )
    assert not tracker.record("client_error_shown", properties={"error_code": "invalid/path"})
    assert tracker.recent_error_codes() == ()
    for _ in range(6):
        assert tracker.record(
            "client_error_shown", properties={"error_code": "network_unavailable"}
        )
    assert len(tracker.recent_error_codes()) == 5


def test_reentrant_preview_cannot_send_second_message(tmp_path):
    def confirm(request):
        dialog.send_message()
        return True

    dialog, transport = _dialog(tmp_path, confirm_send=confirm)
    dialog.composer.setPlainText("test")
    dialog.send_message()
    _wait(dialog)
    assert len(transport.sent) == 1
    dialog.close()


def test_real_app_settings_open_mailbox_and_defer_restart_for_draft(tmp_path, monkeypatch):
    import moeguard.ui.settings_dialog as settings
    from moeguard.app import MoeGuardApp
    from moeguard.config import AppConfig
    from moeguard.roles import RoleLibrary

    monkeypatch.setattr(settings, "RoleLibrary", lambda: RoleLibrary(tmp_path / "roles"))
    app = MoeGuardApp()
    app._config = AppConfig()
    app._support_id_provider = lambda: "test-account"
    app._mailbox_dialog_factory = lambda: _dialog(tmp_path)[0]
    app._on_open_settings()
    assert app._settings_dialog.support_id_edit.text() == "test-account"
    app._settings_dialog.mailbox_button.click()
    mailbox = app._mailbox_dialog
    assert isinstance(mailbox, MailboxDialog)
    mailbox.composer.setPlainText("test")
    assert app.update_blocker() == "mailbox_message"
    mailbox.reject()
    QCoreApplication.processEvents()
    assert app._mailbox_dialog is None and app.update_blocker() == ""
    app._settings_dialog.reject()


def test_settings_support_mailbox_and_data_folder_entries(tmp_path, monkeypatch):
    import moeguard.ui.settings_dialog as settings
    from moeguard.config import AppConfig
    from moeguard.roles import RoleLibrary
    from moeguard.ui.settings_dialog import SettingsDialog

    monkeypatch.setattr(settings, "base_dir", lambda: tmp_path / "user-data")
    opened = []
    monkeypatch.setattr(
        settings.QDesktopServices, "openUrl", lambda url: opened.append(url) or True
    )
    dialog = SettingsDialog(
        AppConfig(),
        role_library=RoleLibrary(tmp_path / "roles"),
        mailbox_available=True,
        support_id="test-account",
    )
    requested = []
    dialog.mailbox_requested.connect(lambda: requested.append(True))
    dialog.copy_support_id_button.click()
    assert QApplication.clipboard().text() == "test-account"
    dialog.mailbox_button.click()
    assert requested == [True]
    dialog.data_folder_button.click()
    assert len(opened) == 1 and opened[0].isLocalFile()
    assert Path(opened[0].toLocalFile()) == tmp_path / "user-data"
    dialog.close()
