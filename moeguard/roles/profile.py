"""Editable appearance section of the CharacterProfile v1 contract."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import Any

from moeguard.roles._validation import (
    ROLE_ID_RE,
    expect_choice,
    expect_int,
    expect_object,
    expect_sha256,
    expect_string,
    fail,
    reject_unknown,
    required,
)
from moeguard.roles.errors import ContractErrorCode, RoleContractError

_PRESENTATIONS = {"masculine", "feminine", "neutral"}
_SILHOUETTES = {"super_chibi", "chibi", "petite"}
_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp"}


def _text(value: Any, path: str, maximum: int) -> str:
    return unicodedata.normalize(
        "NFC", expect_string(value, path, minimum=0, maximum=maximum)
    )


@dataclass(frozen=True)
class ProfileInput:
    kind: str
    sha256: str = ""
    media_type: str = ""

    @classmethod
    def from_dict(cls, raw: Any, path: str = "$.input") -> ProfileInput:
        value = expect_object(raw, path)
        reject_unknown(value, {"kind", "sha256", "media_type"}, path)
        kind = expect_choice(required(value, "kind", path), {"text", "image"}, f"{path}.kind")
        if kind == "text":
            if set(value).difference({"kind"}):
                fail(
                    ContractErrorCode.INVALID_VALUE,
                    "text input must not contain image metadata",
                    path,
                )
            return cls(kind="text")
        digest = expect_sha256(required(value, "sha256", path), f"{path}.sha256")
        media_type = expect_choice(
            required(value, "media_type", path), _IMAGE_MEDIA_TYPES, f"{path}.media_type"
        )
        return cls(kind="image", sha256=digest, media_type=media_type)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind}
        if self.kind == "image":
            value.update({"sha256": self.sha256, "media_type": self.media_type})
        return value


@dataclass(frozen=True)
class VisualIdentity:
    presentation: str
    silhouette: str
    description: str = ""
    negative_description: str = ""
    style_and_mood: str = ""
    palette: str = ""
    hair: str = ""
    eyes: str = ""
    face: str = ""
    clothing: str = ""
    accessories: str = ""
    special_features: str = ""
    identity_anchors: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any, path: str = "$.visual") -> VisualIdentity:
        value = expect_object(raw, path)
        allowed = {
            "presentation",
            "silhouette",
            "description",
            "negative_description",
            "style_and_mood",
            "palette",
            "hair",
            "eyes",
            "face",
            "clothing",
            "accessories",
            "special_features",
            "identity_anchors",
        }
        reject_unknown(value, allowed, path)
        anchors_raw = value.get("identity_anchors", [])
        if not isinstance(anchors_raw, list):
            fail(ContractErrorCode.INVALID_TYPE, "must be an array", f"{path}.identity_anchors")
        if len(anchors_raw) > 8:
            fail(
                ContractErrorCode.INVALID_VALUE,
                "must contain at most 8 anchors",
                f"{path}.identity_anchors",
            )
        anchors = tuple(
            _text(anchor, f"{path}.identity_anchors[{index}]", 160)
            for index, anchor in enumerate(anchors_raw)
        )
        if any(not anchor for anchor in anchors) or len(set(anchors)) != len(anchors):
            fail(
                ContractErrorCode.INVALID_VALUE,
                "anchors must be non-empty and unique",
                f"{path}.identity_anchors",
            )
        return cls(
            presentation=expect_choice(
                required(value, "presentation", path),
                _PRESENTATIONS,
                f"{path}.presentation",
            ),
            silhouette=expect_choice(
                required(value, "silhouette", path), _SILHOUETTES, f"{path}.silhouette"
            ),
            description=_text(value.get("description", ""), f"{path}.description", 1200),
            negative_description=_text(
                value.get("negative_description", ""), f"{path}.negative_description", 500
            ),
            style_and_mood=_text(
                value.get("style_and_mood", ""), f"{path}.style_and_mood", 480
            ),
            palette=_text(value.get("palette", ""), f"{path}.palette", 480),
            hair=_text(value.get("hair", ""), f"{path}.hair", 480),
            eyes=_text(value.get("eyes", ""), f"{path}.eyes", 480),
            face=_text(value.get("face", ""), f"{path}.face", 480),
            clothing=_text(value.get("clothing", ""), f"{path}.clothing", 480),
            accessories=_text(value.get("accessories", ""), f"{path}.accessories", 480),
            special_features=_text(
                value.get("special_features", ""), f"{path}.special_features", 480
            ),
            identity_anchors=anchors,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "presentation": self.presentation,
            "silhouette": self.silhouette,
            "description": self.description,
            "negative_description": self.negative_description,
            "style_and_mood": self.style_and_mood,
            "palette": self.palette,
            "hair": self.hair,
            "eyes": self.eyes,
            "face": self.face,
            "clothing": self.clothing,
            "accessories": self.accessories,
            "special_features": self.special_features,
            "identity_anchors": list(self.identity_anchors),
        }


@dataclass(frozen=True)
class CharacterProfile:
    """The editable v0.2 appearance profile; never a provider request snapshot.

    Personality is intentionally a separately versioned future section. Its
    edits must not invalidate an accepted identity image or generated actions.
    """

    profile_id: str
    appearance_revision: int
    display_name: str
    input: ProfileInput
    visual: VisualIdentity
    schema_version: int = 1

    @classmethod
    def from_dict(cls, raw: Any) -> CharacterProfile:
        value = expect_object(raw, "$")
        reject_unknown(
            value,
            {
                "schema_version",
                "profile_id",
                "appearance_revision",
                "display_name",
                "input",
                "visual",
            },
            "$",
        )
        schema_version = expect_int(
            required(value, "schema_version", "$"), "$.schema_version", minimum=1, maximum=999
        )
        if schema_version != 1:
            fail(
                ContractErrorCode.UNSUPPORTED_SCHEMA,
                f"CharacterProfile schema {schema_version} is not supported",
                "$.schema_version",
            )
        profile_id = expect_string(
            required(value, "profile_id", "$"), "$.profile_id", minimum=3, maximum=48
        )
        if not ROLE_ID_RE.fullmatch(profile_id):
            fail(
                ContractErrorCode.INVALID_ID,
                "must use 3-48 lowercase ASCII letters, digits, or hyphens",
                "$.profile_id",
            )
        display_name = _text(required(value, "display_name", "$"), "$.display_name", 80)
        if not display_name:
            fail(ContractErrorCode.INVALID_VALUE, "must not be empty", "$.display_name")
        return cls(
            schema_version=1,
            profile_id=profile_id,
            appearance_revision=expect_int(
                required(value, "appearance_revision", "$"),
                "$.appearance_revision",
                minimum=1,
                maximum=2_147_483_647,
            ),
            display_name=display_name,
            input=ProfileInput.from_dict(required(value, "input", "$")),
            visual=VisualIdentity.from_dict(required(value, "visual", "$")),
        )

    @classmethod
    def from_json(cls, text: str) -> CharacterProfile:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RoleContractError(
                ContractErrorCode.INVALID_JSON,
                f"invalid JSON at line {exc.lineno} column {exc.colno}",
            ) from exc
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "appearance_revision": self.appearance_revision,
            "display_name": self.display_name,
            "input": self.input.to_dict(),
            "visual": self.visual.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
