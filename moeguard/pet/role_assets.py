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
_REVEAL_BY_SILHOUETTE = {"chibi": 0.45, "petite": 0.38}
_DEFAULT_EDGE_REVEAL_FRACTION = 0.42


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
        if not validate_runtime_role_root(role_root):
            logger.warning("忽略动作包不完整的内置角色 %s: %s", role_id, role_root)
            continue
        seen.add(role_id)
        roles.append(BundledRole(role_id, display_name.strip(), role_root))
    return tuple(roles)


def validate_runtime_role_root(root: Path) -> bool:
    """Return whether every required runtime action contains at least one PNG."""
    if not root.is_dir():
        return False
    return all(
        any((root / action).glob("*.png"))
        for action in _SOURCE_ACTIONS
        if action not in _OPTIONAL_SOURCE_ACTIONS
    )


def resolve_role_root(
    role_id: str,
    assets_dir: str = "",
    *,
    roles_root: Path | None = None,
) -> Path | None:
    """Resolve a custom package override or a validated built-in role ID."""
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
        fraction = interaction.get("edge_reveal_fraction")
        if isinstance(fraction, (int, float)) and 0.15 <= fraction <= 0.75:
            return RoleInteractionProfile(float(fraction), click_lines)

    recipe = _read_json(root / "recipe.json")
    brief = recipe.get("brief", {})
    silhouette = brief.get("silhouette") if isinstance(brief, dict) else None
    return RoleInteractionProfile(
        _REVEAL_BY_SILHOUETTE.get(str(silhouette), _DEFAULT_EDGE_REVEAL_FRACTION),
        click_lines,
    )


def load_runtime_role_actions(
    controller: FrameAnimationController,
    root: Path,
    *,
    fps: int,
) -> RoleInteractionProfile:
    """Load source actions and the deterministic top-edge variant.

    ``peek_left/right`` are authored in viewer coordinates and loaded directly;
    the edge router chooses the opposite direction so both poses face into the
    desktop. ``peek_top`` vertically mirrors ``sit_down`` as a temporary
    head-first top-edge pose. The derived name does not change the source
    package's official-nine contract.
    """
    for action in _SOURCE_ACTIONS:
        paths = sorted(str(path) for path in (root / action).glob("*.png"))
        if not paths and action in _OPTIONAL_SOURCE_ACTIONS:
            continue
        controller.load_action(
            action,
            paths,
            fps=fps,
            loop=action in _LOOP_ACTIONS,
        )

    sit_paths = sorted(str(path) for path in (root / "sit_down").glob("*.png"))
    controller.load_action(
        "peek_top",
        sit_paths,
        fps=fps,
        loop=True,
        flip_vertical=True,
    )
    logger.info("已加载角色运行时动作（含临时顶部探头）: %s", root)
    return load_role_interaction_profile(root)
