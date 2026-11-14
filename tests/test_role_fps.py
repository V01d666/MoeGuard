from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from moeguard.pet.frame_animation import FrameAnimationController
from moeguard.pet.role_assets import load_runtime_role_actions


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])


def role_root(path: Path, fps: object = 12) -> Path:
    idle = path / "actions" / "idle"
    idle.mkdir(parents=True)
    Image.new("RGBA", (8, 8), (20, 80, 140, 255)).save(idle / "0001.png")
    (path / "role.json").write_text(json.dumps({"fps": fps}), encoding="utf-8")
    return path


def test_package_fps_drives_all_authored_and_derived_actions(tmp_path, qt_app):
    controller = FrameAnimationController()
    root = role_root(tmp_path / "role", 12)
    profile = load_runtime_role_actions(controller, root, fps=6)
    assert {data.fps for data in controller._actions.values()} == {12}
    assert profile.fps == 12
    controller.play("idle")
    assert controller._timer.interval() == 83
    controller.stop()


def test_role_preferences_survive_save_without_changing_global_or_other_roles(tmp_path):
    from dataclasses import replace

    from moeguard.config import AppConfig, PetConfig

    first = PetConfig(role_id="first", role_package_version=1)
    second = PetConfig(role_id="second", role_package_version=1)
    assert first.fps_key == replace(first, role_package_version=2).fps_key
    assert first.fps_key != replace(first, role_package_version=0).fps_key
    assert first.fps_key != second.fps_key
    legacy = replace(first, role_package_version=0, assets_dir=str(tmp_path / "legacy"))
    assert legacy.fps_key != replace(legacy, assets_dir=str(tmp_path / "other")).fps_key
    overrides = {first.fps_key: 14, second.fps_key: 8}
    config = AppConfig(pet=replace(first, fps=6, fps_overrides=overrides))
    target = tmp_path / "config.toml"
    assert AppConfig.save(config, target)
    restored = AppConfig.load(target)
    assert restored.pet.fps == 6
    assert restored.pet.fps_overrides == overrides
    assert restored.pet.fps_override == 14
    assert replace(restored.pet, role_id="second").fps_override == 8


@pytest.mark.parametrize("value", [None, [], "bad", {"a": True, "b": 0, "c": 61, "d": "12"}])
def test_invalid_fps_preferences_do_not_break_old_config(value):
    from moeguard.config import AppConfig

    config = AppConfig._from_dict({"pet": {"fps_overrides": value}})
    assert config.pet.fps_overrides == {}
    assert config.pet.fps_override is None


def test_runtime_user_override_updates_frames_and_repaint_then_restores_package_default(
    tmp_path, qt_app,
):
    from dataclasses import replace

    from moeguard.app import MoeGuardApp
    from moeguard.config import PetConfig
    from moeguard.ui.pet_window import PetWindow

    root = role_root(tmp_path / "role", 12)
    pet = PetConfig(role_id="demo", assets_dir=str(root))
    pet = replace(pet, fps_overrides={pet.fps_key: 20})
    app = MoeGuardApp()
    app._frame_controller = FrameAnimationController()
    app._pet_window = PetWindow(app._frame_controller, fps=6)
    app._load_pet_frames(pet)
    assert {data.fps for data in app._frame_controller._actions.values()} == {20}
    assert app._pet_window._paint_timer.interval() == 50
    app._load_pet_frames(replace(pet, fps_overrides={}))
    assert {data.fps for data in app._frame_controller._actions.values()} == {12}
    assert app._pet_window._paint_timer.interval() == 83
    app._pet_window.close()


@pytest.mark.parametrize("fps", [None, True, "12", 0, 61])
def test_old_or_invalid_manifest_fps_uses_global_fallback(tmp_path, qt_app, fps):
    controller = FrameAnimationController()
    root = role_root(tmp_path / "role", fps)
    profile = load_runtime_role_actions(controller, root, fps=7)
    assert profile.fps == 7
    assert {data.fps for data in controller._actions.values()} == {7}


def test_settings_slider_keeps_separate_pending_values_and_reset_follows_package(
    tmp_path, qt_app, monkeypatch,
):
    from moeguard.config import AppConfig
    from moeguard.pet.role_assets import BundledRole
    from moeguard.roles import RoleLibrary
    from moeguard.ui.settings_dialog import SettingsDialog

    first = role_root(tmp_path / "first", 12)
    second = role_root(tmp_path / "second", 8)
    monkeypatch.setattr("moeguard.ui.settings_dialog.discover_bundled_roles", lambda: (
        BundledRole("lumen", "First", first), BundledRole("other", "Second", second),
    ))
    config = AppConfig()
    dialog = SettingsDialog(config, role_library=RoleLibrary(tmp_path / "library"))
    assert dialog.role_fps_slider.minimum() == 1
    assert dialog.role_fps_slider.maximum() == 60
    assert dialog.role_fps_slider.value() == 12
    dialog.role_fps_slider.setValue(20)
    assert dialog.role_fps_value.text() == "20 FPS"
    dialog.role_selector.setCurrentIndex(1)
    assert dialog.role_fps_slider.value() == 8
    dialog.role_fps_slider.setValue(10)
    dialog.role_selector.setCurrentIndex(0)
    assert dialog.role_fps_slider.value() == 20
    dialog.role_fps_reset.click()
    assert dialog.role_fps_slider.value() == 12
    assert dialog.values()["fps_overrides"] == {"builtin:other": 10}
    assert config.pet.fps_overrides == {}
    dialog.reject()
    assert config.pet.fps_overrides == {}
    dialog.deleteLater()


def test_restoring_legacy_global_fps_above_slider_range_keeps_playback_running(tmp_path, qt_app):
    from dataclasses import replace

    from moeguard.config import PetConfig
    from moeguard.ui.settings_dialog import SettingsDialog

    root = role_root(tmp_path / "legacy", None)
    (root / "role.json").unlink()
    pet = PetConfig(assets_dir=str(root), fps=90)
    pet = replace(pet, fps_overrides={pet.fps_key: 20})
    app = running_app(pet)
    dialog = SettingsDialog(app._config)
    assert dialog.role_fps_slider.maximum() == 60
    dialog.role_fps_reset.click()
    assert dialog.role_fps_value.text() == "90 FPS"
    app._apply_settings(dialog.values())
    assert app._config.pet.fps == 90
    assert app._config.pet.fps_override is None
    assert app._frame_controller.is_playing
    assert app._frame_controller._timer.interval() == 11
    assert app._pet_window._paint_timer.interval() == 11
    app._frame_controller.stop()
    app._pet_window.close()
    dialog.deleteLater()


def running_app(pet):
    from types import SimpleNamespace

    from moeguard.app import MoeGuardApp
    from moeguard.config import AppConfig
    from moeguard.ui.pet_window import PetWindow

    app = MoeGuardApp()
    app._config = AppConfig(pet=pet)
    app._frame_controller = FrameAnimationController()
    app._pet_window = PetWindow(app._frame_controller, fps=6)
    app._face_worker = SimpleNamespace(set_motion_recording_enabled=lambda value: None)
    app._evidence = SimpleNamespace(update_config=lambda *args: None)
    app._evidence_store = SimpleNamespace(update_retention_days=lambda value: None)
    app._feedback = SimpleNamespace(
        set_click_lines=lambda lines: None,
        restore_animation=lambda: app._frame_controller.play("idle"),
    )
    app._load_pet_frames(pet)
    return app


def test_saving_only_current_role_fps_applies_without_switch_or_restart(tmp_path, qt_app):
    from moeguard.config import PetConfig
    from moeguard.ui.settings_dialog import SettingsDialog

    pet = PetConfig(assets_dir=str(role_root(tmp_path / "role", 12)))
    app = running_app(pet)
    dialog = SettingsDialog(app._config)
    dialog.role_fps_slider.setValue(20)
    app._apply_settings(dialog.values())
    assert app._config.pet.fps == 6
    assert app._config.pet.fps_override == 20
    assert app._frame_controller._timer.interval() == 50
    assert app._pet_window._paint_timer.interval() == 50
    dialog.role_fps_reset.click()
    app._apply_settings(dialog.values())
    assert app._config.pet.fps_override is None
    assert app._frame_controller._timer.interval() == 83
    assert app._pet_window._paint_timer.interval() == 83
    app._frame_controller.stop()
    app._pet_window.close()
    dialog.deleteLater()


@pytest.mark.parametrize("persist", [True, False])
def test_managed_role_activation_and_save_failure_rollback_restore_both_clocks(
    tmp_path, qt_app, monkeypatch, persist,
):
    from dataclasses import replace
    from types import SimpleNamespace

    from moeguard.config import PetConfig
    from moeguard.roles import PackageKey

    old = PetConfig(assets_dir=str(role_root(tmp_path / "old", 12)))
    app = running_app(old)
    new_root = role_root(tmp_path / "new", 8)
    new = replace(old, role_id="new", role_package_version=1, assets_dir="")
    app._config = replace(app._config, pet=replace(old, fps_overrides={new.fps_key: 20}))
    library = SimpleNamespace(get=lambda key: SimpleNamespace(root=new_root, display_name="New"))
    monkeypatch.setattr(app, "_persist_config", lambda *args, **kwargs: persist)
    success, _ = app.activate_managed_role(PackageKey("new", 1), role_library=library)
    assert success is persist
    expected = 20 if persist else 12
    assert {data.fps for data in app._frame_controller._actions.values()} == {expected}
    assert app._pet_window._paint_timer.interval() == int(1000 / expected)
    assert app._frame_controller._timer.interval() == int(1000 / expected)
    assert app._config.pet.assets_dir == ("" if persist else old.assets_dir)
    app._frame_controller.stop()
    app._pet_window.close()


@pytest.mark.parametrize("outcome", ["success", "save_failure", "load_failure", "stale", "cancel"])
def test_incremental_activation_keeps_old_pet_until_complete(
    tmp_path, qt_app, monkeypatch, outcome,
):
    import time
    from dataclasses import replace
    from types import SimpleNamespace

    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtGui import QPixmap

    import moeguard.pet.frame_animation as frames_mod
    from moeguard.config import PetConfig
    from moeguard.roles import InstalledRole, PackageKey

    old = PetConfig(assets_dir=str(role_root(tmp_path / "old", 12)))
    app = running_app(old)
    app._frame_controller.play("idle")
    root = role_root(tmp_path / "new", 8)
    for i in range(2, 9):
        Image.new("RGBA", (32, 32), (20, 80, 140, 255)).save(
            root / "actions" / "idle" / f"{i:04}.png"
        )
    manifest = json.loads((root / "role.json").read_text())
    manifest["schema_version"] = 2
    (root / "role.json").write_text(json.dumps(manifest))
    library = SimpleNamespace(get=lambda key: pytest.fail("GUI must not revalidate the package"))
    prepared = InstalledRole(PackageKey("new", 1), None, root, name="New")
    previous = app._config
    old_data = app._frame_controller._actions["idle"]
    old_key = old_data.frames[0].cacheKey()
    saved = []
    monkeypatch.setattr(app, "_persist_config", lambda *args, **kw: (
        saved.append(app._config.pet.role_id) or outcome != "save_failure"
    ))
    reads = []

    def read_frame(*args, **kwargs):
        assert QThread.currentThread() == qt_app.thread()
        if args and isinstance(args[0], str) and str(root) in args[0]:
            reads.append(args[0])
            if outcome == "load_failure":
                raise OSError("isolated decode failure")
            time.sleep(.003)
        return QPixmap(*args, **kwargs)

    read_frame.fromImage = QPixmap.fromImage
    monkeypatch.setattr(frames_mod, "QPixmap", read_frame)
    observed = []
    heartbeat = QTimer()
    heartbeat.timeout.connect(lambda: observed.append((
        len(reads), app._config.pet.role_id,
        app._frame_controller.get_current_frame().cacheKey(),
    )))
    heartbeat.start(1)
    completed = []
    try:
        app.activate_managed_role_async(
            PackageKey("new", 1), lambda *result: completed.append(result), role_library=library,
            prepared_role=prepared,
        )
        assert reads == [] and completed == []  # Not synchronous work hidden behind a callback.
        assert app._config is previous
        rejected = []
        app.activate_managed_role_async(
            PackageKey("other", 1), lambda *r: rejected.append(r), role_library=library,
            prepared_role=prepared,
        )
        assert len(rejected) == 1 and rejected[0][0] is False
        deadline = time.monotonic() + 6
        while not completed and time.monotonic() < deadline:
            qt_app.processEvents()
            if reads and outcome == "stale" and app._config is previous:
                app._config = replace(previous, pet=replace(previous.pet, fps=7))
            if reads and outcome == "cancel":
                app.cancel_role_activation()
        assert len(completed) == 1
        assert completed[0][0] is (outcome == "success")
        assert app._role_activation_timer is None
        assert all(k == old_key for n, _role, k in observed if n < 24)
        if outcome == "success":
            assert len({n for n, role, _k in observed if n and role == old.role_id}) > 3
            assert app._config.pet.role_id == "new"
            assert app._frame_controller._timer.interval() == 125
            assert app._pet_window._paint_timer.interval() == 125
        else:
            assert app._config.pet.role_id == old.role_id
            assert app._frame_controller.get_current_frame().cacheKey() == old_key
            assert app._frame_controller._timer.interval() == 83
            assert app._pet_window._paint_timer.interval() == 83
            if outcome != "save_failure":
                assert saved == []
            if outcome == "stale":
                assert app._config.pet.fps == 7
    finally:
        heartbeat.stop()
        if getattr(app, "_role_activation_timer", None) is not None:
            app.cancel_role_activation()
        app._frame_controller.stop()
        app._pet_window.close()
