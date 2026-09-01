"""Pure role/action constants shared by the runtime and private providers."""

from __future__ import annotations

HEAD_BODY_RATIO_BY_SILHOUETTE: dict[str, int] = {
    "super_chibi": 2,
    "chibi": 3,
    "petite": 4,
}


def half_head_reveal_fraction(head_body_ratio: int) -> float:
    """Return the share of full character height occupied by half a head."""
    if head_body_ratio not in {2, 3, 4}:
        raise ValueError("head-body ratio must be 2, 3, or 4")
    return 0.5 / head_body_ratio

BASE_ACTIONS: dict[str, int] = {
    "idle": 12,
    "notice": 8,
    "click_reaction": 8,
    "dragging": 18,
    "patrol": 12,
    "welcome": 8,
}

TRANSITION_ACTIONS: dict[str, int] = {
    "drag_pickup": 18,
    "patrol_look_left": 8,
    "patrol_look_right": 8,
}

EDGE_ACTIONS: dict[str, int] = {
    "peek_left": 8,
    "peek_right": 8,
    "sit_down": 8,
}

OFFICIAL_ACTIONS: dict[str, int] = BASE_ACTIONS | EDGE_ACTIONS
REQUIRED_ACTIONS: dict[str, int] = {"idle": BASE_ACTIONS["idle"]}
OPTIONAL_ACTIONS: dict[str, int] = {
    action: count for action, count in OFFICIAL_ACTIONS.items() if action not in REQUIRED_ACTIONS
}

LOOP_ACTIONS = frozenset(
    {
        "idle",
        "notice",
        "dragging",
        "patrol",
        "peek_left",
        "peek_right",
        "sit_down",
    }
)
