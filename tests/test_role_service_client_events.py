from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from moeguard.cloud.role_service_client_events import (
    ClientEventOutboxStore,
    PreviewClientEventReporter,
    PreviewClientEventTracker,
)
from moeguard.cloud.role_service_http_client import RoleServiceClientEventReceipt


class _Protector:
    def protect(self, value: bytes) -> bytes:
        return b"protected:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        if not value.startswith(b"protected:"):
            raise ValueError("invalid ciphertext")
        return value.removeprefix(b"protected:")[::-1]


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Transport:
    def __init__(self, *, fail: bool = False, duplicate: bool = False) -> None:
        self.fail = fail
        self.duplicate = duplicate
        self.batches: list[tuple] = []

    def submit_client_events(self, events: tuple) -> RoleServiceClientEventReceipt:
        self.batches.append(events)
        if self.fail:
            raise RuntimeError("offline details must not escape")
        accepted = 0 if self.duplicate else len(events)
        duplicates = len(events) if self.duplicate else 0
        return RoleServiceClientEventReceipt(accepted, duplicates, 1_800_000_001_000)


def _tracker(
    path: Path,
    clock: _Clock,
    *,
    enabled: bool = True,
    max_events: int = 2_000,
    max_bytes: int = 1024 * 1024,
    max_age_ms: int = 7 * 24 * 60 * 60 * 1000,
) -> PreviewClientEventTracker:
    return PreviewClientEventTracker(
        ClientEventOutboxStore(
            path,
            _Protector(),
            clock=clock,
            max_events=max_events,
            max_bytes=max_bytes,
            max_age_ms=max_age_ms,
        ),
        app_version="0.2.0-preview.2",
        enabled=enabled,
        clock=clock,
    )


def _record(tracker: PreviewClientEventTracker) -> bool:
    return tracker.record(
        "workbench_opened",
        properties={"screen": "pet_workshop", "entrypoint": "settings"},
    )


def test_outbox_is_protected_and_recovers_after_offline_restart(tmp_path: Path) -> None:
    path = tmp_path / "client-events.json"
    clock = _Clock()
    tracker = _tracker(path, clock)

    assert _record(tracker)
    queued_event = tracker.outbox.due_batch()[0]
    payload = path.read_bytes()
    assert queued_event.event_id.encode() not in payload
    assert b"workbench_opened" not in payload

    failed = tracker.flush(_Transport(fail=True))
    assert failed.error_code == "delivery_failed"
    assert failed.pending_count == 1

    clock.value += 5
    recovered = _tracker(path, clock)
    transport = _Transport()
    sent = recovered.flush(transport)
    assert sent.accepted_count == 1
    assert sent.pending_count == 0
    assert transport.batches[0][0].event_id == queued_event.event_id
    assert not path.exists()


def test_server_accept_before_local_ack_replays_same_id_safely(
    tmp_path: Path, monkeypatch
) -> None:
    tracker = _tracker(tmp_path / "client-events.json", _Clock())
    assert _record(tracker)
    original = tracker.outbox.due_batch()[0]

    monkeypatch.setattr(
        tracker.outbox,
        "acknowledge",
        lambda _event_ids: (_ for _ in ()).throw(OSError("simulated crash")),
    )
    first = tracker.flush(_Transport())
    assert first.error_code == "ack_failed"
    assert first.pending_count == 1

    monkeypatch.undo()
    duplicate_transport = _Transport(duplicate=True)
    second = tracker.flush(duplicate_transport)
    assert second.accepted_count == 0
    assert second.duplicate_count == 1
    assert duplicate_transport.batches[0][0].event_id == original.event_id
    assert second.pending_count == 0


def test_outbox_count_and_age_limits_drop_oldest_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "client-events.json"
    clock = _Clock()
    tracker = _tracker(path, clock, max_events=2, max_age_ms=1_000)

    assert _record(tracker)
    first = tracker.outbox.due_batch()[0].event_id
    assert _record(tracker)
    assert _record(tracker)
    retained = tracker.outbox.due_batch()
    assert len(retained) == 2
    assert first not in {event.event_id for event in retained}

    clock.value += 2
    assert tracker.outbox.pending_count() == 0
    assert not path.exists()


def test_outbox_file_size_limit_keeps_newest_valid_events(tmp_path: Path) -> None:
    path = tmp_path / "client-events.json"
    tracker = _tracker(path, _Clock(), max_bytes=1_200)

    for _index in range(5):
        assert _record(tracker)

    retained = tracker.outbox.due_batch()
    assert 1 <= len(retained) < 5
    assert path.stat().st_size <= 1_200


def test_disabled_or_broken_telemetry_never_blocks_product_flow(tmp_path: Path) -> None:
    disabled_path = tmp_path / "disabled.json"
    disabled = _tracker(disabled_path, _Clock(), enabled=False)
    transport = _Transport()
    assert not _record(disabled)
    assert disabled.flush(transport).error_code == "disabled"
    assert transport.batches == []
    assert not disabled_path.exists()

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_bytes(b"not a protected outbox")
    broken = _tracker(corrupt_path, _Clock())
    assert not _record(broken)
    assert broken.flush(transport).error_code == "outbox_unavailable"
    assert corrupt_path.read_bytes() == b"not a protected outbox"

    private_path = tmp_path / "private.json"
    private = _tracker(private_path, _Clock())
    assert not private.record(
        "workbench_opened",
        properties={
            "screen": "pet_workshop",
            "entrypoint": "settings",
            "prompt": "must never be queued",
        },
    )
    assert not private_path.exists()


def test_events_use_random_scopes_and_monotonic_session_sequence(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path / "client-events.json", _Clock())
    original_journey = tracker.journey_id
    assert _record(tracker)
    assert _record(tracker)
    tracker.start_journey()
    assert _record(tracker)

    events = tracker.outbox.due_batch()
    assert [event.sequence for event in events] == [0, 1, 2]
    assert len({event.event_id for event in events}) == 3
    assert {event.session_id for event in events} == {tracker.session_id}
    assert events[0].journey_id == events[1].journey_id == original_journey
    assert events[2].journey_id == tracker.journey_id != original_journey


def test_concurrent_records_preserve_unique_monotonic_sequence(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path / "client-events.json", _Clock())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: _record(tracker), range(40)))

    assert all(results)
    events = tracker.outbox.due_batch()
    assert [event.sequence for event in events] == list(range(40))


def test_flush_never_sends_more_than_fifty_events(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path / "client-events.json", _Clock())
    for _index in range(51):
        assert _record(tracker)
    transport = _Transport()

    first = tracker.flush(transport)
    second = tracker.flush(transport)

    assert first.attempted_count == 50
    assert first.pending_count == 1
    assert second.attempted_count == 1
    assert second.pending_count == 0


def test_reporter_delivers_in_background_and_drains_outbox(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path / "client-events.json", _Clock())
    transport = _Transport()
    reporter = PreviewClientEventReporter(tracker, transport)

    assert reporter.record(
        "workbench_opened",
        properties={"screen": "pet_workshop", "entrypoint": "settings"},
    )
    assert reporter.wait_for_idle()

    assert len(transport.batches) == 1
    assert transport.batches[0][0].event_name == "workbench_opened"
    assert tracker.outbox.pending_count() == 0


def test_reporter_failure_never_raises_and_retains_outbox(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path / "client-events.json", _Clock())
    reporter = PreviewClientEventReporter(tracker, _Transport(fail=True))

    assert reporter.record(
        "workbench_opened",
        properties={"screen": "pet_workshop", "entrypoint": "settings"},
    )
    assert reporter.wait_for_idle()
    assert tracker.outbox.pending_count() == 1
