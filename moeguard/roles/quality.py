"""Provider-neutral diagnostics for generated role action frames."""

from __future__ import annotations

import copy
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from moeguard.roles.errors import ContractErrorCode, RoleContractError
from moeguard.roles.package import load_role_package

_STATIONARY_ACTIONS = frozenset({"idle", "notice", "dragging", "sit_down"})


@dataclass(frozen=True)
class AnchorReport:
    """Objective drift signals; never an automatic visual acceptance decision."""

    frame_count: int
    canvas: tuple[int, int]
    overall_center_span_px: float
    subject_center_span_px: float
    center_disagreement_span_px: float
    bottom_anchor_drift_px: float
    bbox_width_span_px: float
    bbox_height_span_px: float
    alpha_coverage_min: float
    alpha_coverage_max: float
    border_touch_frame_count: int
    manual_review_required: bool
    warnings: tuple[str, ...] = ()
    subject_method: str = "upper-alpha-median"

    def to_metrics(self) -> dict[str, bool | int | float]:
        """Return values accepted by ``RolePackage.quality.actions.metrics``."""
        return {
            "anchor_manual_review_required": self.manual_review_required,
            "bottom_anchor_drift_px": self.bottom_anchor_drift_px,
            "bbox_height_span_px": self.bbox_height_span_px,
            "bbox_width_span_px": self.bbox_width_span_px,
            "alpha_coverage_max": self.alpha_coverage_max,
            "alpha_coverage_min": self.alpha_coverage_min,
            "border_touch_frame_count": self.border_touch_frame_count,
            "center_disagreement_span_px": self.center_disagreement_span_px,
            "overall_center_span_px": self.overall_center_span_px,
            "subject_center_span_px": self.subject_center_span_px,
        }


@dataclass(frozen=True)
class StabilizationPreview:
    """Three non-destructive frame sets for user comparison."""

    root: Path
    action: str
    available_levels: tuple[str, ...]
    original: tuple[Path, ...]
    soft: tuple[Path, ...]
    strong: tuple[Path, ...]
    soft_clipped_frames: tuple[int, ...] = ()
    strong_clipped_frames: tuple[int, ...] = ()

    def frames_for(self, level: str) -> tuple[Path, ...]:
        """Return an explicitly available candidate without changing source frames."""
        if level not in self.available_levels:
            raise ValueError(f"stabilization level {level!r} is unavailable for {self.action}")
        return getattr(self, level)


@dataclass(frozen=True)
class PackageAnchorReport:
    """Provider-neutral anchor diagnostics for every action in one package."""

    role_id: str
    package_version: int
    actions: tuple[tuple[str, AnchorReport], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "package_version": self.package_version,
            "actions": {
                action: {
                    "frame_count": report.frame_count,
                    "canvas": list(report.canvas),
                    "subject_method": report.subject_method,
                    "metrics": report.to_metrics(),
                    "warnings": list(report.warnings),
                }
                for action, report in self.actions
            },
        }


@dataclass(frozen=True)
class _FrameAnchor:
    path: Path
    image: Image.Image
    overall_center_x: float
    subject_center_x: float
    bottom_y: float
    bbox_width: float
    bbox_height: float
    alpha_coverage: float
    touches_border: bool


def _span(values: list[float]) -> float:
    return round(max(values) - min(values), 3)


def _measure_frames(
    frame_paths: list[Path] | tuple[Path, ...],
    *,
    alpha_threshold: int,
) -> tuple[tuple[int, int], list[_FrameAnchor]]:
    canvas: tuple[int, int] | None = None
    anchors: list[_FrameAnchor] = []
    for index, path in enumerate(frame_paths):
        try:
            with Image.open(path) as source:
                if source.mode != "RGBA":
                    raise RoleContractError(
                        ContractErrorCode.INVALID_IMAGE,
                        "anchor analysis requires RGBA frames",
                        path=f"frames[{index}]",
                    )
                image = source.copy()
        except RoleContractError:
            raise
        except (OSError, ValueError) as exc:
            raise RoleContractError(
                ContractErrorCode.INVALID_IMAGE,
                f"cannot decode frame: {exc}",
                path=f"frames[{index}]",
            ) from exc
        if canvas is None:
            canvas = image.size
        elif image.size != canvas:
            raise RoleContractError(
                ContractErrorCode.INVALID_IMAGE,
                "all analyzed frames must use the same canvas",
                path=f"frames[{index}]",
            )

        alpha = np.asarray(image.getchannel("A"))
        ys, xs = np.nonzero(alpha >= alpha_threshold)
        if not len(xs):
            raise RoleContractError(
                ContractErrorCode.INVALID_IMAGE,
                "frame has no visible subject pixels",
                path=f"frames[{index}]",
            )
        left, right = int(xs.min()), int(xs.max())
        top, bottom = int(ys.min()), int(ys.max())
        overall_center = (left + right) / 2
        upper_limit = top + max(1, round((bottom - top + 1) * 0.55))
        upper_xs = xs[ys < upper_limit]
        subject_center = float(np.median(upper_xs)) if len(upper_xs) else overall_center
        anchors.append(
            _FrameAnchor(
                path=path,
                image=image,
                overall_center_x=overall_center,
                subject_center_x=subject_center,
                bottom_y=float(bottom),
                bbox_width=float(right - left + 1),
                bbox_height=float(bottom - top + 1),
                alpha_coverage=round(float(len(xs)) / float(image.width * image.height), 6),
                touches_border=(
                    left == 0
                    or top == 0
                    or right == image.width - 1
                    or bottom == image.height - 1
                ),
            )
        )
    assert canvas is not None
    return canvas, anchors


def analyze_action_anchors(
    frame_paths: list[Path] | tuple[Path, ...],
    *,
    alpha_threshold: int = 16,
    review_threshold_fraction: float = 0.02,
) -> AnchorReport:
    """Compare whole-silhouette and upper-subject anchors across RGBA frames.

    The upper-subject median is deliberately only a candidate anchor. Hair,
    horns, wings, poses and occlusion can invalidate it, so suspicious results
    always request user review instead of modifying frames automatically.
    """
    if not frame_paths:
        raise RoleContractError(
            ContractErrorCode.INVALID_IMAGE,
            "anchor analysis requires at least one frame",
            path="frames",
        )
    if not 1 <= alpha_threshold <= 254:
        raise ValueError("alpha_threshold must be between 1 and 254")
    if not 0 < review_threshold_fraction <= 0.25:
        raise ValueError("review_threshold_fraction must be in (0, 0.25]")

    canvas, anchors = _measure_frames(frame_paths, alpha_threshold=alpha_threshold)
    overall_centers = [frame.overall_center_x for frame in anchors]
    subject_centers = [frame.subject_center_x for frame in anchors]
    disagreements = [
        frame.overall_center_x - frame.subject_center_x for frame in anchors
    ]
    bottoms = [frame.bottom_y for frame in anchors]
    bbox_widths = [frame.bbox_width for frame in anchors]
    bbox_heights = [frame.bbox_height for frame in anchors]
    alpha_coverages = [frame.alpha_coverage for frame in anchors]
    overall_span = _span(overall_centers)
    subject_span = _span(subject_centers)
    disagreement_span = _span(disagreements)
    bottom_drift = _span(bottoms)
    bbox_width_span = _span(bbox_widths)
    bbox_height_span = _span(bbox_heights)
    border_touch_count = sum(frame.touches_border for frame in anchors)
    threshold = max(2.0, canvas[0] * review_threshold_fraction)
    warnings: list[str] = []
    if disagreement_span > threshold:
        warnings.append("anchor-overall-subject-disagreement")
    if subject_span > threshold:
        warnings.append("subject-anchor-drift")
    if bottom_drift > threshold:
        warnings.append("bottom-anchor-drift")
    if border_touch_count:
        warnings.append("visible-alpha-touches-canvas-border")

    return AnchorReport(
        frame_count=len(frame_paths),
        canvas=canvas,
        overall_center_span_px=overall_span,
        subject_center_span_px=subject_span,
        center_disagreement_span_px=disagreement_span,
        bottom_anchor_drift_px=bottom_drift,
        bbox_width_span_px=bbox_width_span,
        bbox_height_span_px=bbox_height_span,
        alpha_coverage_min=min(alpha_coverages),
        alpha_coverage_max=max(alpha_coverages),
        border_touch_frame_count=border_touch_count,
        manual_review_required=bool(warnings),
        warnings=tuple(warnings),
    )


def analyze_package_anchors(
    package_root: Path,
    *,
    alpha_threshold: int = 16,
    review_threshold_fraction: float = 0.02,
) -> PackageAnchorReport:
    """Analyze every declared action without changing package acceptance state."""
    package = load_role_package(package_root)
    reports = tuple(
        (
            action,
            analyze_action_anchors(
                [package_root / frame.path for frame in package_action.frames],
                alpha_threshold=alpha_threshold,
                review_threshold_fraction=review_threshold_fraction,
            ),
        )
        for action, package_action in package.actions
    )
    return PackageAnchorReport(
        role_id=package.role_id,
        package_version=package.package_version,
        actions=reports,
    )


def merge_anchor_report(
    manifest: dict[str, Any],
    action: str,
    report: AnchorReport,
) -> dict[str, Any]:
    """Return a copy with diagnostics merged into one v2 quality action.

    A new warning can revoke acceptance but a clean report never grants it.
    Human review remains the only path from pending to accepted.
    """
    result = copy.deepcopy(manifest)
    try:
        quality = result["quality"]
        action_quality = quality["actions"][action]
    except (KeyError, TypeError) as exc:
        raise RoleContractError(
            ContractErrorCode.INVALID_VALUE,
            "manifest does not contain quality metadata for the action",
            path=f"$.quality.actions.{action}",
        ) from exc
    if not isinstance(quality, dict) or not isinstance(action_quality, dict):
        raise RoleContractError(
            ContractErrorCode.INVALID_TYPE,
            "quality metadata must be objects",
            path=f"$.quality.actions.{action}",
        )
    metrics = action_quality.setdefault("metrics", {})
    warnings = action_quality.setdefault("warnings", [])
    if not isinstance(metrics, dict) or not isinstance(warnings, list):
        raise RoleContractError(
            ContractErrorCode.INVALID_TYPE,
            "quality metrics/warnings use invalid containers",
            path=f"$.quality.actions.{action}",
        )
    metrics.update(report.to_metrics())
    warnings[:] = list(dict.fromkeys([*warnings, *report.warnings]))
    if report.manual_review_required:
        action_quality["status"] = "pending"
        quality["status"] = "pending"
    return result


def _shifted_preview(frame: _FrameAnchor, dx: int) -> tuple[Image.Image, bool]:
    output = Image.new("RGBA", frame.image.size, (0, 0, 0, 0))
    output.alpha_composite(frame.image, (dx, 0))
    before = int(np.asarray(frame.image.getchannel("A"), dtype=np.uint64).sum())
    after = int(np.asarray(output.getchannel("A"), dtype=np.uint64).sum())
    return output, after != before


def render_stabilization_previews(
    frame_paths: list[Path] | tuple[Path, ...],
    output_root: Path,
    *,
    action: str = "idle",
    alpha_threshold: int = 16,
) -> StabilizationPreview:
    """Write safe comparison candidates without mutating source frames.

    Actions with intentional horizontal travel expose only the original frames.
    This keeps a generic anchor correction from erasing click, welcome, patrol,
    or viewer-direction semantics.
    """
    if output_root.exists():
        raise RoleContractError(
            ContractErrorCode.ALREADY_EXISTS,
            "preview output already exists",
            path=str(output_root),
        )
    if not frame_paths:
        raise RoleContractError(
            ContractErrorCode.INVALID_IMAGE,
            "preview generation requires at least one frame",
            path="frames",
        )
    _, anchors = _measure_frames(frame_paths, alpha_threshold=alpha_threshold)
    available_levels = (
        ("original", "soft", "strong")
        if action in _STATIONARY_ACTIONS
        else ("original",)
    )
    target_x = float(np.median([frame.subject_center_x for frame in anchors]))
    original_dir = output_root / "original"
    soft_dir = output_root / "soft"
    strong_dir = output_root / "strong"
    try:
        directories = [original_dir]
        if "soft" in available_levels:
            directories.extend((soft_dir, strong_dir))
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=False)
        original_paths: list[Path] = []
        soft_paths: list[Path] = []
        strong_paths: list[Path] = []
        soft_clipped: list[int] = []
        strong_clipped: list[int] = []
        for index, frame in enumerate(anchors, start=1):
            name = f"{index:04d}.png"
            original_path = original_dir / name
            shutil.copy2(frame.path, original_path)
            original_paths.append(original_path)

            if "soft" in available_levels:
                full_dx = round(target_x - frame.subject_center_x)
                soft_image, was_soft_clipped = _shifted_preview(
                    frame, round(full_dx * 0.5)
                )
                strong_image, was_strong_clipped = _shifted_preview(frame, full_dx)
                soft_path = soft_dir / name
                strong_path = strong_dir / name
                soft_image.save(soft_path)
                strong_image.save(strong_path)
                soft_paths.append(soft_path)
                strong_paths.append(strong_path)
                if was_soft_clipped:
                    soft_clipped.append(index)
                if was_strong_clipped:
                    strong_clipped.append(index)
    except Exception:
        if output_root.is_dir():
            shutil.rmtree(output_root)
        raise
    return StabilizationPreview(
        root=output_root,
        action=action,
        available_levels=available_levels,
        original=tuple(original_paths),
        soft=tuple(soft_paths),
        strong=tuple(strong_paths),
        soft_clipped_frames=tuple(soft_clipped),
        strong_clipped_frames=tuple(strong_clipped),
    )
