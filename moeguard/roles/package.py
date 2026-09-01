"""Immutable RolePackage v2 contract plus the bundled v1 compatibility adapter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from moeguard.roles._validation import (
    FIELD_ID_RE,
    ROLE_ID_RE,
    expect_bool,
    expect_choice,
    expect_int,
    expect_number,
    expect_object,
    expect_sha256,
    expect_string,
    fail,
    reject_unknown,
    required,
)
from moeguard.roles.errors import ContractErrorCode, RoleContractError
from moeguard.roles.spec import (
    LOOP_ACTIONS,
    OFFICIAL_ACTIONS,
    REQUIRED_ACTIONS,
    TRANSITION_ACTIONS,
)

_REVIEW_STATUSES = {"pending", "accepted", "rejected"}
_ACTION_SOURCES = {"generated", "mirrored", "conjugate", "local-derived", "legacy"}
_DERIVED_ACTION_SOURCES = {"mirrored", "conjugate", "local-derived"}
_PROCESSING_LEVELS = {"raw", "soft", "strong"}
_RIGHTS_SCOPES = {"private-use", "shareable"}
_PEEK_SEMANTICS = {"viewer_direction"}
_DIRECTION_EXPECTATIONS = {"peek_left": "left", "peek_right": "right"}
_DIRECTION_REVIEW_METHODS = {"manual-screen-review"}


def _safe_frame_path(value: Any, path: str) -> str:
    result = expect_string(value, path, minimum=1, maximum=240).replace("\\", "/")
    pure = PurePosixPath(result)
    if (
        not pure.parts
        or pure.is_absolute()
        or ":" in pure.parts[0]
        or ".." in pure.parts
        or "." in pure.parts
        or pure.suffix.lower() != ".png"
    ):
        fail(
            ContractErrorCode.INVALID_PATH,
            "must be a relative PNG path without dot segments",
            path,
        )
    return pure.as_posix()


@dataclass(frozen=True)
class FrameReference:
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, raw: Any, path: str) -> FrameReference:
        value = expect_object(raw, path)
        reject_unknown(value, {"path", "sha256"}, path)
        return cls(
            path=_safe_frame_path(required(value, "path", path), f"{path}.path"),
            sha256=expect_sha256(required(value, "sha256", path), f"{path}.sha256"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class PackageAction:
    loop: bool
    frames: tuple[FrameReference, ...]

    @classmethod
    def from_dict(cls, action: str, raw: Any, path: str) -> PackageAction:
        value = expect_object(raw, path)
        reject_unknown(value, {"loop", "frames"}, path)
        frames_raw = required(value, "frames", path)
        if not isinstance(frames_raw, list):
            fail(ContractErrorCode.INVALID_TYPE, "must be an array", f"{path}.frames")
        expected = (OFFICIAL_ACTIONS | TRANSITION_ACTIONS).get(action)
        if expected is None:
            if not 1 <= len(frames_raw) <= 240:
                fail(
                    ContractErrorCode.INVALID_VALUE,
                    "custom actions must contain 1-240 frames",
                    f"{path}.frames",
                )
        elif len(frames_raw) != expected:
            fail(
                ContractErrorCode.INVALID_VALUE,
                f"must contain exactly {expected} frames",
                f"{path}.frames",
            )
        frames = tuple(
            FrameReference.from_dict(frame, f"{path}.frames[{index}]")
            for index, frame in enumerate(frames_raw)
        )
        if len({frame.path for frame in frames}) != len(frames):
            fail(ContractErrorCode.INVALID_VALUE, "frame paths must be unique", f"{path}.frames")
        for index, frame in enumerate(frames, start=1):
            expected_path = f"actions/{action}/{index:04d}.png"
            if frame.path != expected_path:
                fail(
                    ContractErrorCode.INVALID_PATH,
                    f"frame {index} must be stored as {expected_path}",
                    f"{path}.frames[{index - 1}].path",
                )
        loop = expect_bool(required(value, "loop", path), f"{path}.loop")
        known_actions = OFFICIAL_ACTIONS | TRANSITION_ACTIONS
        if action in known_actions and loop != (action in LOOP_ACTIONS):
            fail(
                ContractErrorCode.INVALID_VALUE,
                f"loop must be {action in LOOP_ACTIONS} for {action}",
                f"{path}.loop",
            )
        return cls(loop=loop, frames=frames)

    def to_dict(self) -> dict[str, Any]:
        return {"loop": self.loop, "frames": [frame.to_dict() for frame in self.frames]}


@dataclass(frozen=True)
class DirectionReview:
    expected: str
    status: str
    method: str

    @classmethod
    def from_dict(cls, raw: Any, action: str, path: str) -> DirectionReview:
        value = expect_object(raw, path)
        reject_unknown(value, {"expected", "status", "method"}, path)
        expected = expect_choice(
            required(value, "expected", path), {"left", "right"}, f"{path}.expected"
        )
        required_expected = _DIRECTION_EXPECTATIONS[action]
        if expected != required_expected:
            fail(
                ContractErrorCode.INVALID_VALUE,
                f"{action} must be reviewed as viewer-{required_expected}",
                f"{path}.expected",
            )
        return cls(
            expected=expected,
            status=expect_choice(
                required(value, "status", path), _REVIEW_STATUSES, f"{path}.status"
            ),
            method=expect_choice(
                required(value, "method", path),
                _DIRECTION_REVIEW_METHODS,
                f"{path}.method",
            ),
        )

    def to_dict(self) -> dict[str, str]:
        return {"expected": self.expected, "status": self.status, "method": self.method}


@dataclass(frozen=True)
class ActionQuality:
    status: str
    source: str
    processing: str
    source_action: str | None = None
    direction: DirectionReview | None = None
    metrics: tuple[tuple[str, bool | int | float], ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, action: str, path: str) -> ActionQuality:
        value = expect_object(raw, path)
        reject_unknown(
            value,
            {
                "status",
                "source",
                "source_action",
                "processing",
                "direction",
                "metrics",
                "warnings",
            },
            path,
        )
        metrics_raw = expect_object(value.get("metrics", {}), f"{path}.metrics")
        metrics: list[tuple[str, bool | int | float]] = []
        for key, metric in sorted(metrics_raw.items()):
            if not FIELD_ID_RE.fullmatch(key):
                fail(
                    ContractErrorCode.INVALID_ID,
                    "metric names must use lowercase ASCII letters, digits, or underscores",
                    f"{path}.metrics",
                )
            if isinstance(metric, bool):
                metrics.append((key, metric))
            elif isinstance(metric, int) and not isinstance(metric, bool):
                if abs(metric) > 1_000_000:
                    fail(
                        ContractErrorCode.INVALID_VALUE,
                        "metric is out of range",
                        f"{path}.metrics.{key}",
                    )
                metrics.append((key, metric))
            elif isinstance(metric, float):
                metrics.append(
                    (
                        key,
                        expect_number(
                            metric,
                            f"{path}.metrics.{key}",
                            minimum=-1_000_000,
                            maximum=1_000_000,
                        ),
                    )
                )
            else:
                fail(
                    ContractErrorCode.INVALID_TYPE,
                    "metrics must be booleans or finite numbers",
                    f"{path}.metrics.{key}",
                )
        warnings_raw = value.get("warnings", [])
        if not isinstance(warnings_raw, list) or len(warnings_raw) > 32:
            fail(
                ContractErrorCode.INVALID_TYPE,
                "warnings must be an array with at most 32 entries",
                f"{path}.warnings",
            )
        warnings = tuple(
            expect_string(warning, f"{path}.warnings[{index}]", minimum=1, maximum=80)
            for index, warning in enumerate(warnings_raw)
        )
        status = expect_choice(
            required(value, "status", path), _REVIEW_STATUSES, f"{path}.status"
        )
        source = expect_choice(
            required(value, "source", path), _ACTION_SOURCES, f"{path}.source"
        )
        source_action_raw = value.get("source_action")
        source_action = None
        if source_action_raw is not None:
            source_action = expect_string(
                source_action_raw, f"{path}.source_action", minimum=1, maximum=64
            )
            if not FIELD_ID_RE.fullmatch(source_action):
                fail(
                    ContractErrorCode.INVALID_ID,
                    "source_action must be a valid action ID",
                    f"{path}.source_action",
                )
        if source in _DERIVED_ACTION_SOURCES and source_action is None:
            fail(
                ContractErrorCode.MISSING_FIELD,
                f"{source} actions must declare source_action",
                path,
            )
        if source not in _DERIVED_ACTION_SOURCES and source_action is not None:
            fail(
                ContractErrorCode.INVALID_VALUE,
                f"{source} actions cannot declare source_action",
                f"{path}.source_action",
            )

        direction = None
        if action in _DIRECTION_EXPECTATIONS:
            direction = DirectionReview.from_dict(
                required(value, "direction", path), action, f"{path}.direction"
            )
            if status == "accepted" and direction.status != "accepted":
                fail(
                    ContractErrorCode.INVALID_VALUE,
                    "accepted directional actions require accepted manual direction review",
                    f"{path}.status",
                )
            if direction.status == "rejected" and status != "rejected":
                fail(
                    ContractErrorCode.INVALID_VALUE,
                    "rejected direction review requires rejected action quality",
                    f"{path}.status",
                )
        elif "direction" in value:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "direction review is only valid for viewer-direction actions",
                f"{path}.direction",
            )

        return cls(
            status=status,
            source=source,
            processing=expect_choice(
                required(value, "processing", path),
                _PROCESSING_LEVELS,
                f"{path}.processing",
            ),
            source_action=source_action,
            direction=direction,
            metrics=tuple(metrics),
            warnings=warnings,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "source": self.source,
            "processing": self.processing,
            "metrics": dict(self.metrics),
            "warnings": list(self.warnings),
        }
        if self.source_action is not None:
            result["source_action"] = self.source_action
        if self.direction is not None:
            result["direction"] = self.direction.to_dict()
        return result


@dataclass(frozen=True)
class QualitySummary:
    status: str
    actions: tuple[tuple[str, ActionQuality], ...]

    @classmethod
    def from_dict(cls, raw: Any, action_names: set[str], path: str = "$.quality") -> QualitySummary:
        value = expect_object(raw, path)
        reject_unknown(value, {"status", "actions"}, path)
        actions_raw = expect_object(required(value, "actions", path), f"{path}.actions")
        if set(actions_raw) != action_names:
            missing = sorted(action_names.difference(actions_raw))
            extra = sorted(set(actions_raw).difference(action_names))
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if extra:
                detail.append("unknown " + ", ".join(extra))
            fail(
                ContractErrorCode.INVALID_VALUE,
                "quality actions must match package actions: " + "; ".join(detail),
                f"{path}.actions",
            )
        actions = tuple(
            (
                action,
                ActionQuality.from_dict(
                    actions_raw[action], action, f"{path}.actions.{action}"
                ),
            )
            for action in sorted(action_names)
        )
        status = expect_choice(
            required(value, "status", path), _REVIEW_STATUSES, f"{path}.status"
        )
        action_statuses = {quality.status for _, quality in actions}
        quality_by_action = dict(actions)
        for action, quality in actions:
            if quality.source_action is None:
                continue
            if quality.source_action == action:
                fail(
                    ContractErrorCode.INVALID_VALUE,
                    "derived action cannot cite itself as source_action",
                    f"{path}.actions.{action}.source_action",
                )
            source_quality = quality_by_action.get(quality.source_action)
            if source_quality is None:
                fail(
                    ContractErrorCode.INVALID_VALUE,
                    "source_action must exist in the same immutable package",
                    f"{path}.actions.{action}.source_action",
                )
            if source_quality.status != "accepted":
                fail(
                    ContractErrorCode.INVALID_VALUE,
                    "derived action requires an accepted source_action",
                    f"{path}.actions.{action}.source_action",
                )
        if status == "accepted" and action_statuses != {"accepted"}:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "overall accepted status requires every action to be accepted",
                f"{path}.status",
            )
        if status == "rejected" and "rejected" not in action_statuses:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "overall rejected status requires at least one rejected action",
                f"{path}.status",
            )
        return cls(status=status, actions=actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "actions": {action: quality.to_dict() for action, quality in self.actions},
        }


@dataclass(frozen=True)
class RightsConfirmation:
    input_rights_confirmed: bool
    output_accepted: bool
    scope: str

    @classmethod
    def from_dict(cls, raw: Any, path: str = "$.rights") -> RightsConfirmation:
        value = expect_object(raw, path)
        reject_unknown(value, {"input_rights_confirmed", "output_accepted", "scope"}, path)
        return cls(
            input_rights_confirmed=expect_bool(
                required(value, "input_rights_confirmed", path),
                f"{path}.input_rights_confirmed",
            ),
            output_accepted=expect_bool(
                required(value, "output_accepted", path), f"{path}.output_accepted"
            ),
            scope=expect_choice(required(value, "scope", path), _RIGHTS_SCOPES, f"{path}.scope"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_rights_confirmed": self.input_rights_confirmed,
            "output_accepted": self.output_accepted,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class RolePackage:
    """A provider-free, immutable runtime delivery contract."""

    role_id: str
    package_version: int
    profile_id: str
    appearance_revision: int
    identity_sha256: str
    display_name: str
    fps: int
    canvas: tuple[int, int]
    edge_reveal_fraction: float
    head_body_ratio: int | None
    peek_action_semantics: str
    actions: tuple[tuple[str, PackageAction], ...]
    quality: QualitySummary
    rights: RightsConfirmation
    click_lines: tuple[str, ...] = ()
    schema_version: int = 2
    source_schema_version: int = 2

    @classmethod
    def from_dict(cls, raw: Any) -> RolePackage:
        value = expect_object(raw, "$")
        reject_unknown(
            value,
            {
                "schema_version",
                "role_id",
                "package_version",
                "profile",
                "display_name",
                "fps",
                "canvas",
                "interaction",
                "actions",
                "quality",
                "rights",
                "dialogue",
            },
            "$",
        )
        schema_version = expect_int(
            required(value, "schema_version", "$"), "$.schema_version", minimum=1, maximum=999
        )
        if schema_version != 2:
            fail(
                ContractErrorCode.UNSUPPORTED_SCHEMA,
                f"RolePackage schema {schema_version} is not supported by the v2 parser",
                "$.schema_version",
            )
        role_id = expect_string(required(value, "role_id", "$"), "$.role_id", minimum=3, maximum=48)
        if not ROLE_ID_RE.fullmatch(role_id):
            fail(
                ContractErrorCode.INVALID_ID,
                "must use 3-48 lowercase ASCII letters, digits, or hyphens",
                "$.role_id",
            )
        profile = expect_object(required(value, "profile", "$"), "$.profile")
        reject_unknown(
            profile,
            {"id", "appearance_revision", "identity_sha256"},
            "$.profile",
        )
        profile_id = expect_string(
            required(profile, "id", "$.profile"), "$.profile.id", minimum=3, maximum=48
        )
        if not ROLE_ID_RE.fullmatch(profile_id):
            fail(ContractErrorCode.INVALID_ID, "invalid profile ID", "$.profile.id")
        if profile_id != role_id:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "profile.id must equal role_id for v0.2 packages",
                "$.profile.id",
            )
        canvas_raw = required(value, "canvas", "$")
        if not isinstance(canvas_raw, list) or len(canvas_raw) != 2:
            fail(ContractErrorCode.INVALID_TYPE, "must be [width, height]", "$.canvas")
        canvas = (
            expect_int(canvas_raw[0], "$.canvas[0]", minimum=64, maximum=2048),
            expect_int(canvas_raw[1], "$.canvas[1]", minimum=64, maximum=2048),
        )
        interaction = expect_object(required(value, "interaction", "$"), "$.interaction")
        reject_unknown(
            interaction,
            {"edge_reveal_fraction", "head_body_ratio", "peek_action_semantics"},
            "$.interaction",
        )
        actions_raw = expect_object(required(value, "actions", "$"), "$.actions")
        action_names = set(actions_raw)
        if not set(REQUIRED_ACTIONS).issubset(action_names):
            missing = sorted(set(REQUIRED_ACTIONS).difference(action_names))
            fail(
                ContractErrorCode.INCOMPLETE_ACTIONS,
                "missing required actions: " + ", ".join(missing),
                "$.actions",
            )
        if len(action_names) > 64:
            fail(ContractErrorCode.INVALID_VALUE, "too many actions", "$.actions")
        for action in action_names:
            if not FIELD_ID_RE.fullmatch(action):
                fail(ContractErrorCode.INVALID_ID, "invalid action ID", f"$.actions.{action}")
        actions = tuple(
            (
                action,
                PackageAction.from_dict(action, actions_raw[action], f"$.actions.{action}"),
            )
            for action in sorted(action_names)
        )
        dialogue = expect_object(value.get("dialogue", {}), "$.dialogue")
        reject_unknown(dialogue, {"click_lines"}, "$.dialogue")
        lines_raw = dialogue.get("click_lines", [])
        if not isinstance(lines_raw, list) or len(lines_raw) > 20:
            fail(
                ContractErrorCode.INVALID_TYPE,
                "click_lines must be an array with at most 20 entries",
                "$.dialogue.click_lines",
            )
        click_lines = tuple(
            expect_string(line, f"$.dialogue.click_lines[{index}]", minimum=1, maximum=120)
            for index, line in enumerate(lines_raw)
        )
        return cls(
            role_id=role_id,
            package_version=expect_int(
                required(value, "package_version", "$"),
                "$.package_version",
                minimum=1,
                maximum=2_147_483_647,
            ),
            profile_id=profile_id,
            appearance_revision=expect_int(
                required(profile, "appearance_revision", "$.profile"),
                "$.profile.appearance_revision",
                minimum=1,
                maximum=2_147_483_647,
            ),
            identity_sha256=expect_sha256(
                required(profile, "identity_sha256", "$.profile"),
                "$.profile.identity_sha256",
            ),
            display_name=expect_string(
                required(value, "display_name", "$"), "$.display_name", minimum=1, maximum=80
            ),
            fps=expect_int(required(value, "fps", "$"), "$.fps", minimum=1, maximum=60),
            canvas=canvas,
            edge_reveal_fraction=expect_number(
                required(interaction, "edge_reveal_fraction", "$.interaction"),
                "$.interaction.edge_reveal_fraction",
                minimum=0.10,
                maximum=0.75,
            ),
            head_body_ratio=(
                expect_int(
                    interaction["head_body_ratio"],
                    "$.interaction.head_body_ratio",
                    minimum=2,
                    maximum=4,
                )
                if "head_body_ratio" in interaction
                else None
            ),
            peek_action_semantics=expect_choice(
                required(interaction, "peek_action_semantics", "$.interaction"),
                _PEEK_SEMANTICS,
                "$.interaction.peek_action_semantics",
            ),
            actions=actions,
            quality=QualitySummary.from_dict(
                required(value, "quality", "$"), action_names
            ),
            rights=RightsConfirmation.from_dict(required(value, "rights", "$")),
            click_lines=click_lines,
        )

    @classmethod
    def from_json(cls, text: str) -> RolePackage:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RoleContractError(
                ContractErrorCode.INVALID_JSON,
                f"invalid JSON at line {exc.lineno} column {exc.colno}",
            ) from exc
        return cls.from_dict(value)

    @property
    def installable(self) -> bool:
        return (
            self.quality.status == "accepted"
            and self.rights.input_rights_confirmed
            and self.rights.output_accepted
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role_id": self.role_id,
            "package_version": self.package_version,
            "profile": {
                "id": self.profile_id,
                "appearance_revision": self.appearance_revision,
                "identity_sha256": self.identity_sha256,
            },
            "display_name": self.display_name,
            "fps": self.fps,
            "canvas": list(self.canvas),
            "interaction": {
                "edge_reveal_fraction": self.edge_reveal_fraction,
                **(
                    {"head_body_ratio": self.head_body_ratio}
                    if self.head_body_ratio is not None
                    else {}
                ),
                "peek_action_semantics": self.peek_action_semantics,
            },
            "actions": {action: item.to_dict() for action, item in self.actions},
            "quality": self.quality.to_dict(),
            "rights": self.rights.to_dict(),
            "dialogue": {"click_lines": list(self.click_lines)},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RoleContractError(
            ContractErrorCode.INVALID_JSON, f"cannot read role.json: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RoleContractError(
            ContractErrorCode.INVALID_JSON,
            f"invalid role.json at line {exc.lineno} column {exc.colno}",
        ) from exc
    return dict(expect_object(value, "$"))


def _adapt_v1_manifest(manifest: dict[str, Any], root: Path) -> RolePackage:
    role_id = manifest.get("role_id")
    display_name = manifest.get("display_name")
    if not isinstance(role_id, str) or not ROLE_ID_RE.fullmatch(role_id):
        fail(ContractErrorCode.INVALID_ID, "legacy role_id is invalid", "$.role_id")
    if not isinstance(display_name, str) or not display_name.strip():
        fail(ContractErrorCode.INVALID_VALUE, "legacy display_name is invalid", "$.display_name")
    if manifest.get("tier") != "official-9":
        fail(
            ContractErrorCode.INCOMPLETE_ACTIONS,
            "only official-9 legacy packages are compatible",
            "$.tier",
        )
    canvas_raw = manifest.get("canvas")
    if (
        not isinstance(canvas_raw, list)
        or len(canvas_raw) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in canvas_raw)
    ):
        fail(ContractErrorCode.INVALID_TYPE, "legacy canvas is invalid", "$.canvas")
    if not all(64 <= item <= 2048 for item in canvas_raw):
        fail(ContractErrorCode.INVALID_VALUE, "legacy canvas is out of range", "$.canvas")
    fps = manifest.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 60:
        fail(ContractErrorCode.INVALID_VALUE, "legacy fps is invalid", "$.fps")
    actions: list[tuple[str, PackageAction]] = []
    qualities: list[tuple[str, ActionQuality]] = []
    review = manifest.get("review")
    review = review if isinstance(review, dict) else {}
    directional = review.get("directional_actions")
    directional = directional if isinstance(directional, dict) else {}
    distribution_ready = manifest.get("distribution_ready") is True
    for action, count in OFFICIAL_ACTIONS.items():
        frame_paths = [root / action / f"{index:04d}.png" for index in range(1, count + 1)]
        if not all(path.is_file() for path in frame_paths):
            fail(
                ContractErrorCode.INCOMPLETE_ACTIONS,
                f"legacy action {action} is incomplete",
                f"$.actions.{action}",
            )
        frames = tuple(
            FrameReference(
                path=path.relative_to(root).as_posix(),
                sha256=sha256(path.read_bytes()).hexdigest(),
            )
            for path in frame_paths
        )
        actions.append((action, PackageAction(loop=action in LOOP_ACTIONS, frames=frames)))
        direction_status = None
        if action in {"peek_left", "peek_right"}:
            direction_entry = directional.get(action)
            if isinstance(direction_entry, dict):
                direction_status = direction_entry.get("status")
        accepted = distribution_ready and (
            action not in _DIRECTION_EXPECTATIONS or direction_status == "accepted"
        )
        qualities.append(
            (
                action,
                ActionQuality(
                    status="accepted" if accepted else "pending",
                    source="legacy",
                    processing="raw",
                    direction=(
                        DirectionReview(
                            expected=_DIRECTION_EXPECTATIONS[action],
                            status=(
                                direction_status
                                if direction_status in _REVIEW_STATUSES
                                else "pending"
                            ),
                            method="manual-screen-review",
                        )
                        if action in _DIRECTION_EXPECTATIONS
                        else None
                    ),
                ),
            )
        )
    all_accepted = all(item.status == "accepted" for _, item in qualities)
    interaction = manifest.get("interaction")
    interaction = interaction if isinstance(interaction, dict) else {}
    edge_fraction = interaction.get("edge_reveal_fraction", 1 / 6)
    if (
        isinstance(edge_fraction, bool)
        or not isinstance(edge_fraction, (int, float))
        or not 0.10 <= float(edge_fraction) <= 0.75
    ):
        edge_fraction = 1 / 6
    dialogue = manifest.get("dialogue")
    dialogue = dialogue if isinstance(dialogue, dict) else {}
    click_lines_raw = dialogue.get("click_lines", [])
    click_lines_raw = click_lines_raw if isinstance(click_lines_raw, list) else []
    click_lines = tuple(
        line.strip()
        for line in click_lines_raw
        if isinstance(line, str) and line.strip()
    )[:20]
    provenance = manifest.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    rights_confirmed = distribution_ready and provenance.get("status") == "release-approved"
    return RolePackage(
        role_id=role_id,
        package_version=1,
        profile_id=role_id,
        appearance_revision=1,
        identity_sha256=next(
            frame.sha256
            for action, item in actions
            if action == "idle"
            for frame in item.frames[:1]
        ),
        display_name=display_name.strip(),
        fps=fps,
        canvas=(canvas_raw[0], canvas_raw[1]),
        edge_reveal_fraction=float(edge_fraction),
        head_body_ratio=(
            int(interaction["head_body_ratio"])
            if isinstance(interaction.get("head_body_ratio"), int)
            and not isinstance(interaction.get("head_body_ratio"), bool)
            and interaction["head_body_ratio"] in {2, 3, 4}
            else None
        ),
        peek_action_semantics="viewer_direction",
        actions=tuple(actions),
        quality=QualitySummary(
            status="accepted" if all_accepted else "pending",
            actions=tuple(qualities),
        ),
        rights=RightsConfirmation(
            input_rights_confirmed=rights_confirmed,
            output_accepted=rights_confirmed,
            scope="shareable" if rights_confirmed else "private-use",
        ),
        click_lines=click_lines,
        source_schema_version=1,
    )


def load_role_package(root: Path) -> RolePackage:
    """Load a native v2 package or adapt a complete bundled v1 package in memory."""
    manifest = _read_manifest(root / "role.json")
    schema_version = manifest.get("schema_version")
    if schema_version == 2:
        return RolePackage.from_dict(manifest)
    if schema_version == 1:
        return _adapt_v1_manifest(manifest, root)
    fail(
        ContractErrorCode.UNSUPPORTED_SCHEMA,
        f"RolePackage schema {schema_version!r} is not supported",
        "$.schema_version",
    )
    raise AssertionError("unreachable")
