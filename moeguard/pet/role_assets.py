"""Load authored role assets plus deterministic runtime edge variants.

The package remains an auditable set of source actions.  Runtime-only variants
can mirror an accepted source without another probabilistic model request.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from moeguard.pet.frame_animation import FrameAnimationController
from moeguard.roles import PackageKey, RoleContractError, RoleLibrary
from moeguard.roles.spec import (
    HEAD_BODY_RATIO_BY_SILHOUETTE,
    half_head_reveal_fraction,
)
from moeguard.utils.paths import resource_path

logger = logging.getLogger(__name__)

_LOOP_ACTIONS = frozenset(
    {
        "idle",
        "patrol",
        "notice",
        "dragging",
        "peek_left",
        "peek_right",
        "peek_bottom",
        "peek_top",
        "sit_down",
    }
)
_SOURCE_ACTIONS = (
    "idle",
    "notice",
    "click_reaction",
    "dragging",
    "drag_pickup",
    "patrol_look_left",
    "patrol_look_right",
    "peek_left",
    "peek_right",
    "sit_down",
    "patrol",
    "welcome",
)
_OPTIONAL_SOURCE_ACTIONS = frozenset(
    {"drag_pickup", "patrol_look_left", "patrol_look_right"}
)
_REQUIRED_CUSTOM_ACTIONS = ("idle",)
_REQUIRED_BUNDLED_ACTIONS = tuple(
    action for action in _SOURCE_ACTIONS if action not in _OPTIONAL_SOURCE_ACTIONS
)
_DEFAULT_EDGE_REVEAL_FRACTION = half_head_reveal_fraction(3)


@dataclass(frozen=True)
class RoleInteractionProfile:
    """Role-specific layout hints; values remain independent of screen size."""

    edge_reveal_fraction: float = _DEFAULT_EDGE_REVEAL_FRACTION
    click_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class BundledRole:
    """A complete built-in role discovered from its release manifest."""

    role_id: str
    display_name: str
    root: Path


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def discover_bundled_roles(roles_root: Path | None = None) -> tuple[BundledRole, ...]:
    """Discover complete built-in roles without maintaining a second hard-coded list."""
    root = roles_root or resource_path("roles")
    if not root.is_dir():
        return ()

    roles: list[BundledRole] = []
    seen: set[str] = set()
    for role_root in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest = _read_json(role_root / "role.json")
        role_id = manifest.get("role_id")
        display_name = manifest.get("display_name")
        if not isinstance(role_id, str) or not role_id.strip():
            continue
        role_id = role_id.strip()
        if role_id in seen:
            logger.warning("忽略重复的内置角色 ID %s: %s", role_id, role_root)
            continue
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = role_id
        if not validate_runtime_role_root(role_root, require_complete=True):
            logger.warning("忽略动作包不完整的内置角色 %s: %s", role_id, role_root)
            continue
        seen.add(role_id)
        roles.append(BundledRole(role_id, display_name.strip(), role_root))
    return tuple(roles)


def _action_assets_root(root: Path) -> Path:
    """Resolve native v2 ``actions/`` or the legacy flat action layout."""
    native = root / "actions"
    return native if native.is_dir() else root


def _action_frame_paths(root: Path, action: str) -> list[str]:
    return sorted(str(path) for path in (_action_assets_root(root) / action).glob("*.png"))


def validate_runtime_role_root(root: Path, *, require_complete: bool = False) -> bool:
    """Validate a runtime root.

    User-created v2 roles only require ``idle``; every missing event action is
    handled by the animation controller's idle fallback. Bundled v1 roles keep
    the stricter launch-asset completeness gate.
    """
    if not root.is_dir():
        return False
    required = _REQUIRED_BUNDLED_ACTIONS if require_complete else _REQUIRED_CUSTOM_ACTIONS
    return all(_action_frame_paths(root, action) for action in required)


def resolve_role_root(
    role_id: str,
    assets_dir: str = "",
    *,
    package_version: int = 0,
    role_library: RoleLibrary | None = None,
    roles_root: Path | None = None,
) -> Path | None:
    """Resolve a custom package override or a validated built-in role ID."""
    if package_version > 0:
        library = role_library or RoleLibrary()
        try:
            return library.get(PackageKey(role_id, package_version)).root
        except (OSError, RoleContractError) as exc:
            logger.warning("受管角色包不可用 %s@%s: %s", role_id, package_version, exc)
            return None

    if assets_dir:
        custom_root = Path(assets_dir)
        return custom_root if validate_runtime_role_root(custom_root) else None

    roles = discover_bundled_roles(roles_root)
    by_id = {role.role_id: role.root for role in roles}
    return by_id.get(role_id) or by_id.get("lumen") or (roles[0].root if roles else None)


def load_role_interaction_profile(root: Path) -> RoleInteractionProfile:
    """Read explicit package metadata, then fall back to the compiled brief."""
    manifest = _read_json(root / "role.json")
    interaction = manifest.get("interaction", {})
    dialogue = manifest.get("dialogue", {})
    click_lines: tuple[str, ...] = ()
    if isinstance(dialogue, dict):
        raw_lines = dialogue.get("click_lines")
        if isinstance(raw_lines, list):
            click_lines = tuple(
                line.strip()
                for line in raw_lines
                if isinstance(line, str) and line.strip()
            )
    if isinstance(interaction, dict):
        head_body_ratio = interaction.get("head_body_ratio")
        if isinstance(head_body_ratio, int) and not isinstance(head_body_ratio, bool):
            try:
                return RoleInteractionProfile(
                    half_head_reveal_fraction(head_body_ratio), click_lines
                )
            except ValueError:
                pass
        fraction = interaction.get("edge_reveal_fraction")
        if isinstance(fraction, (int, float)) and 0.10 <= fraction <= 0.75:
            return RoleInteractionProfile(float(fraction), click_lines)

    recipe = _read_json(root / "recipe.json")
    brief = recipe.get("brief", {})
    silhouette = brief.get("silhouette") if isinstance(brief, dict) else None
    head_body_ratio = HEAD_BODY_RATIO_BY_SILHOUETTE.get(str(silhouette), 3)
    return RoleInteractionProfile(
        half_head_reveal_fraction(head_body_ratio),
        click_lines,
    )


def load_runtime_role_actions(
    controller: FrameAnimationController,
    root: Path,
    *,
    fps: int,
) -> RoleInteractionProfile:
    """Load source actions and deterministic runtime edge variants.

    ``peek_left/right`` are authored in viewer coordinates. If only one side is
    present, the opposite side is derived by a free horizontal mirror at runtime;
    users may still generate an independent opposite action later. The internal
    vertical source is ``peek_bottom`` when a future package provides it, otherwise
    ``idle``. ``peek_top`` always mirrors that same source vertically. Neither
    vertical runtime name is exposed as a v0.2 workbench generation option.
    """
    for action in _SOURCE_ACTIONS:
        paths = _action_frame_paths(root, action)
        if not paths:
            continue
        controller.load_action(
            action,
            paths,
            fps=fps,
            loop=action in _LOOP_ACTIONS,
        )

    idle_paths = _action_frame_paths(root, "idle")
    left_paths = _action_frame_paths(root, "peek_left")
    right_paths = _action_frame_paths(root, "peek_right")
    if left_paths and not right_paths:
        controller.load_action(
            "peek_right",
            left_paths,
            fps=fps,
            loop=True,
            flip_horizontal=True,
        )
    elif right_paths and not left_paths:
        controller.load_action(
            "peek_left",
            right_paths,
            fps=fps,
            loop=True,
            flip_horizontal=True,
        )

    vertical_paths = _action_frame_paths(root, "peek_bottom") or idle_paths
    if vertical_paths:
        controller.load_action(
            "peek_bottom",
            vertical_paths,
            fps=fps,
            loop=True,
        )
        controller.load_action(
            "peek_top",
            vertical_paths,
            fps=fps,
            loop=True,
            flip_vertical=True,
        )
    logger.info("已加载角色运行时动作（缺失动作回退 idle）: %s", root)
    return load_role_interaction_profile(root)
