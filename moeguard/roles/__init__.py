"""Versioned, provider-neutral contracts for user-created MoeGuard roles."""

from moeguard.roles.archive import (
    extract_role_archive,
    inspect_role_archive,
    verify_role_directory,
)
from moeguard.roles.drafts import RoleDraft, RoleDraftStore
from moeguard.roles.editor import ActionRevision, build_package_revision
from moeguard.roles.errors import ContractErrorCode, RoleContractError
from moeguard.roles.library import InstalledRole, PackageKey, RoleLibrary
from moeguard.roles.package import DirectionReview, RolePackage, load_role_package
from moeguard.roles.profile import CharacterProfile
from moeguard.roles.quality import (
    AnchorReport,
    PackageAnchorReport,
    StabilizationPreview,
    analyze_action_anchors,
    analyze_package_anchors,
    merge_anchor_report,
    render_stabilization_previews,
)
from moeguard.roles.tasks import (
    RoleTaskArtifactStore,
    RoleTaskRecord,
    RoleTaskSpec,
    RoleTaskStore,
)

__all__ = [
    "CharacterProfile",
    "AnchorReport",
    "ActionRevision",
    "ContractErrorCode",
    "DirectionReview",
    "InstalledRole",
    "PackageKey",
    "PackageAnchorReport",
    "RoleContractError",
    "RoleDraft",
    "RoleDraftStore",
    "RoleLibrary",
    "RolePackage",
    "RoleTaskRecord",
    "RoleTaskSpec",
    "RoleTaskStore",
    "RoleTaskArtifactStore",
    "StabilizationPreview",
    "extract_role_archive",
    "inspect_role_archive",
    "load_role_package",
    "merge_anchor_report",
    "render_stabilization_previews",
    "verify_role_directory",
    "analyze_action_anchors",
    "analyze_package_anchors",
    "build_package_revision",
]
