"""Opt-in self-update through Velopack, with MoeGuard owning the network.

Velopack replaces the program folder and restarts the app; the user data under
``%USERPROFILE%\\.moeguard`` is outside that folder and is never touched.

Two deliberate departures from the stock integration:

* **MoeGuard downloads, Velopack installs.**  Velopack's HTTP source appends
  ``localVersion``, ``id`` and a persistent random ``stagingId`` to every feed
  request, and ``UpdateManager(None)`` silently talks to the vendor's hosted
  service.  Neither is acceptable for an anonymous product whose data notice
  promises that checking for updates carries no identity.  So this module
  fetches the static feed and packages itself -- plain GETs, no query string,
  no credentials, no cookies, no redirects -- verifies every file's SHA-256,
  and hands Velopack a local staging directory to install from.
* **Velopack is optional at import time.**  Source checkouts, tests and builds
  without the ``velopack`` wheel run unchanged; the updater simply reports that
  it is unavailable.

Nothing here runs unless a feed is configured, and nothing restarts the app
without the caller asking for it.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FEED_ENV = "MOEGUARD_UPDATE_FEED"
CHANNEL = "win"
USER_AGENT = "MoeGuard-Updater/1"
MAX_FEED_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 1024 * 1024 * 1024
_CHUNK = 1024 * 1024
_SPACE_RESERVE = 100 * 1024 * 1024
_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}\.nupkg$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class UpdateError(RuntimeError):
    """An update step failed; the running version stays in place."""

    def __init__(
        self, message: str, *, retryable: bool = False, user_message: str | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.user_message = user_message


def _check_storage(requirements: tuple[tuple[Path, int], ...]) -> None:
    """Probe writes and add simultaneous allocations on each filesystem.

    This is a conservative capacity check, not a reservation: another process
    may consume space or change permissions afterwards. No user files are changed.
    """

    volumes: dict[int, tuple[Path, int]] = {}
    try:
        for directory, required in requirements:
            directory.mkdir(exist_ok=True)
            with tempfile.TemporaryFile(prefix=".moeguard-update-", dir=directory) as probe:
                probe.write(b"update preflight")
                probe.flush()
                os.fsync(probe.fileno())
            volume = directory.stat().st_dev
            previous = volumes.get(volume, (directory, 0))
            volumes[volume] = (previous[0], previous[1] + required)
        for directory, required in volumes.values():
            if shutil.disk_usage(directory).free < required + _SPACE_RESERVE:
                raise UpdateError(
                    "insufficient free space for update",
                    user_message="更新所需磁盘空间不足，请清理程序、下载缓存或临时目录所在磁盘后重试。",
                )
    except OSError as exc:
        raise UpdateError(
            "update directory is not writable",
            user_message="更新目录无法写入，请检查文件夹权限、只读状态或安全软件拦截后重试。",
        ) from exc


def default_staging_dir() -> Path:
    """Downloads wait next to the user data, never inside the program folder."""

    from moeguard.utils.paths import base_dir

    return base_dir() / "update-staging"


def validate_public_feed_url(source: str) -> str:
    """Validate the non-secret HTTPS address embedded in a release."""

    if not isinstance(source, str) or len(source) > 2048 or any(c.isspace() for c in source):
        raise UpdateError("bundled update feed URL is invalid")
    try:
        if _source_kind(source) != "https":
            raise UpdateError("bundled update feed must use HTTPS")
        urllib.parse.urlsplit(source).port  # Reject malformed port numbers.
    except ValueError as exc:
        raise UpdateError("bundled update feed URL is invalid") from exc
    return source.rstrip("/")


def bundled_feed(path: Path | None = None) -> str:
    """Read opt-in build configuration; a normal build has no update source."""

    if path is None:
        from moeguard.utils.paths import resource_path

        path = resource_path("update-source.json")
    try:
        if path.stat().st_size > 4096:
            raise UpdateError("bundled update configuration is too large")
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("bundled update configuration is unreadable") from exc
    if (
        not isinstance(config, dict)
        or set(config) != {"schema_version", "feed_url"}
        or type(config["schema_version"]) is not int
        or config["schema_version"] != 1
    ):
        raise UpdateError("bundled update configuration schema is invalid")
    return validate_public_feed_url(config["feed_url"])


# -- startup ----------------------------------------------------------------


def _load_velopack(module: Any | None = None) -> Any | None:
    if module is not None:
        return module
    try:
        return importlib.import_module("velopack")
    except ImportError:
        return None


def run_velopack_startup(
    *, velopack_module: Any | None = None, frozen: bool | None = None
) -> bool:
    """Hand control to Velopack's install/update hooks before anything else.

    Must run first in the entry point: during install, update and uninstall
    Velopack starts the app with hook arguments and expects it to exit from
    inside ``run()``.  Auto-apply is off because an update must never restart
    the app behind a running generation task or an active patrol; applying is
    always an explicit call.  Returns whether Velopack ran.
    """

    if not (getattr(sys, "frozen", False) if frozen is None else frozen):
        return False
    module = _load_velopack(velopack_module)
    if module is None:
        return False
    try:
        module.App().set_auto_apply_on_startup(False).run()
    except Exception:
        # A portable ZIP extracted by hand is not a Velopack install; the app
        # must still start.
        logger.warning("Velopack startup skipped", exc_info=True)
        return False
    global _velopack_started
    _velopack_started = True
    return True


# Whether ``run_velopack_startup`` handed this process to Velopack (telemetry).
_velopack_started = False


def install_kind(
    *,
    frozen: bool | None = None,
    executable: str | None = None,
    local_appdata: str | None = None,
) -> str:
    """How the running copy was installed, for the ``daily_active`` heartbeat.

    The Setup.exe install lives under ``%LOCALAPPDATA%`` and runs through
    Velopack; a ZIP extracted anywhere else is portable. Source runs are
    ``source``. Anything unexpected reports ``unknown`` rather than guessing.
    """

    try:
        if not (getattr(sys, "frozen", False) if frozen is None else frozen):
            return "source"
        base = os.environ.get("LOCALAPPDATA", "") if local_appdata is None else local_appdata
        if _velopack_started and base:
            exe = Path(executable or sys.executable).resolve()
            try:
                exe.relative_to(Path(base).resolve())
                return "installed"
            except ValueError:
                pass
        return "portable"
    except (OSError, RuntimeError, ValueError):
        return "unknown"


# -- feed latency (v0.4.0 ``network_sample``, route ``updates``) -------------
FeedObserver = Callable[[str, int, bool], None]
_feed_observer: FeedObserver | None = None


def set_feed_observer(observer: FeedObserver | None) -> None:
    global _feed_observer
    _feed_observer = observer


def _observe_feed(started: float, ok: bool) -> None:
    observer = _feed_observer
    if observer is None:
        return
    try:
        observer("updates", int((time.monotonic() - started) * 1000), ok)
    except Exception:  # noqa: BLE001 - telemetry never affects an update check
        pass


# -- feed and transfer ------------------------------------------------------


def _source_kind(source: str) -> str:
    parsed = urllib.parse.urlsplit(source)
    if parsed.scheme == "https" and parsed.hostname:
        kind = "https"
    elif parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
        kind = "http-loopback"
    elif not parsed.scheme or len(parsed.scheme) == 1:  # "D:\\feed" parses as scheme "d"
        if not Path(source).is_absolute():
            raise UpdateError("update feed directory must be an absolute path")
        return "directory"
    else:
        raise UpdateError("update feed must be HTTPS, loopback HTTP or a local directory")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UpdateError("update feed URL must not carry credentials or a query")
    return kind


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        raise UpdateError("update feed redirects are refused")


def _default_opener() -> urllib.request.OpenerDirector:
    # No cookie processor, no proxy auth, no redirects: the request carries
    # nothing beyond the path and a fixed User-Agent.
    return urllib.request.build_opener(_RejectRedirect())


def _url(source: str, name: str) -> str:
    return source.rstrip("/") + "/" + urllib.parse.quote(name)


def _read_limited(stream: Any, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("update feed response is too large")
    return data


def fetch_feed_bytes(
    source: str, *, opener: urllib.request.OpenerDirector | None = None
) -> bytes:
    name = f"releases.{CHANNEL}.json"
    if _source_kind(source) == "directory":
        path = Path(source) / name
        try:
            if path.stat().st_size > MAX_FEED_BYTES:
                raise UpdateError("update feed is too large")
            return path.read_bytes()
        except OSError as exc:
            raise UpdateError("update feed is unreadable") from exc
    request = urllib.request.Request(
        _url(source, name), method="GET", headers={"User-Agent": USER_AGENT}
    )
    started = time.monotonic()
    try:
        with (opener or _default_opener()).open(request, timeout=20) as response:
            data = _read_limited(response, MAX_FEED_BYTES)
        _observe_feed(started, True)
        return data
    except urllib.error.HTTPError as exc:
        _observe_feed(started, exc.code < 500)
        raise UpdateError(
            "update feed is unreachable", retryable=exc.code == 429 or exc.code >= 500
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _observe_feed(started, False)
        raise UpdateError("update feed is unreachable", retryable=True) from exc


def parse_feed(raw: bytes, *, package_id: str) -> tuple[dict[str, Any], ...]:
    """Return this app's valid assets; anything malformed is dropped.

    The staged feed handed to Velopack is rebuilt from these dicts only, so a
    hostile file name or a missing digest never reaches the installer.
    """

    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("update feed is not JSON") from exc
    assets = document.get("Assets") if isinstance(document, dict) else None
    if not isinstance(assets, list):
        raise UpdateError("update feed has no asset list")
    valid: list[dict[str, Any]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("FileName")
        size = asset.get("Size")
        if (
            asset.get("PackageId") != package_id
            or asset.get("Type") not in {"Full", "Delta"}
            or not isinstance(asset.get("Version"), str)
            or not isinstance(name, str)
            or not _SAFE_FILE_RE.fullmatch(name)
            or not isinstance(asset.get("SHA256"), str)
            or not _SHA256_RE.fullmatch(asset["SHA256"])
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= MAX_ASSET_BYTES
        ):
            continue
        valid.append(asset)
    return tuple(valid)


def download_asset(
    source: str,
    asset: dict[str, Any],
    directory: Path,
    *,
    opener: urllib.request.OpenerDirector | None = None,
    progress: Callable[[int], None] | None = None,
) -> Path:
    """Fetch one package into ``directory`` and verify size and SHA-256."""

    name = str(asset["FileName"])
    if not _SAFE_FILE_RE.fullmatch(name):
        raise UpdateError("update file name is unsafe")
    expected_size = int(asset["Size"])
    expected_digest = str(asset["SHA256"]).lower()
    target = directory / name
    partial = directory / f"{name}.partial"
    digest = hashlib.sha256()
    written = 0
    try:
        if _source_kind(source) == "directory":
            reader: Any = (Path(source) / name).open("rb")
        else:
            request = urllib.request.Request(
                _url(source, name), method="GET", headers={"User-Agent": USER_AGENT}
            )
            reader = (opener or _default_opener()).open(request, timeout=60)
        with reader as stream, partial.open("wb") as output:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_size:
                    raise UpdateError("update file is larger than announced")
                digest.update(chunk)
                output.write(chunk)
                if progress is not None:
                    progress(written)
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError("update download failed") from exc
    if written != expected_size or digest.hexdigest() != expected_digest:
        partial.unlink(missing_ok=True)
        raise UpdateError("update file failed verification")
    os.replace(partial, target)
    return target


# -- the updater ------------------------------------------------------------


@dataclass(frozen=True)
class UpdateOffer:
    version: str
    download_bytes: int
    is_delta: bool
    info: Any
    files: tuple[dict[str, Any], ...]
    full: dict[str, Any]


class Updater:
    """Check, download and apply with MoeGuard doing every network request."""

    def __init__(
        self,
        source: str,
        *,
        staging_dir: Path,
        velopack_module: Any,
        opener: urllib.request.OpenerDirector | None = None,
        locator: Any | None = None,
    ) -> None:
        _source_kind(source)
        self.source = source
        self.staging_dir = Path(staging_dir)
        self._velopack = velopack_module
        self._opener = opener
        # Always an explicit local source: UpdateManager(None) would contact
        # the vendor's hosted service instead.  The channel is pinned so the
        # staged feed name never depends on what the package manifest says.
        options = velopack_module.UpdateOptions(False, 10, CHANNEL)
        self._manager = velopack_module.UpdateManager(
            str(self.staging_dir), options, locator
        )
        self._install_root: Path | None = None
        self._packages_dir: Path | None = None
        self._current_dir: Path | None = None
        if locator is not None:
            self._install_root = Path(locator.RootAppDir)
            self._packages_dir = Path(locator.PackagesDir)
            self._current_dir = Path(locator.CurrentBinaryDir)
        elif getattr(sys, "frozen", False) and sys.platform == "win32":
            # The manager has already validated that this is a Velopack install.
            # Windows packages put the real executable in root/current.
            self._current_dir = Path(sys.executable).resolve().parent
            self._install_root = self._current_dir.parent
            self._packages_dir = self._install_root / "packages"
        # Keep the receipt outside staging (checks clear staging), and scope it
        # to this installation so another portable copy cannot consume it.
        installation = os.path.normcase(os.path.abspath(sys.executable))
        key = hashlib.sha256(installation.encode("utf-8")).hexdigest()[:24]
        self._attempt_path = self.staging_dir.parent / f"update-attempt-{key}.json"

    @classmethod
    def create(
        cls,
        source: str,
        *,
        staging_dir: Path,
        velopack_module: Any | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        locator: Any | None = None,
    ) -> Updater | None:
        """Return an updater, or None when this build cannot self-update."""

        module = _load_velopack(velopack_module)
        if module is None:
            return None
        try:
            Path(staging_dir).mkdir(parents=True, exist_ok=True)
            return cls(
                source,
                staging_dir=staging_dir,
                velopack_module=module,
                opener=opener,
                locator=locator,
            )
        except UpdateError:
            raise
        except OSError as exc:
            raise UpdateError("update staging directory is unavailable") from exc
        except Exception:
            # Not a Velopack install (a source run or a hand-copied folder).
            logger.info("self-update unavailable in this installation", exc_info=True)
            return None

    @property
    def current_version(self) -> str:
        return str(self._manager.get_current_version())

    def consume_failed_apply(self) -> str | None:
        """Detect a restart into the old version after native apply failed.

        Consume the receipt once: this launch reports failure and skips its
        automatic check; a later deliberate launch may retry the update.
        """

        try:
            raw = self._attempt_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UpdateError("update attempt receipt is unreadable") from exc
        try:
            attempt = json.loads(raw)
            source = attempt["source_version"]
            target = attempt["target_version"]
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError("invalid versions")
        except (ValueError, KeyError, TypeError):
            logger.warning("discarding invalid update attempt receipt")
            source = target = None
        current = self.current_version
        try:
            self._attempt_path.unlink()
        except OSError as exc:
            raise UpdateError("update attempt receipt could not be cleared") from exc
        if current == source and target != source:
            logger.warning("native update to %s did not replace %s", target, current)
            return target
        return None

    def check(self) -> UpdateOffer | None:
        package_id = str(self._manager.get_app_id())
        assets = parse_feed(
            fetch_feed_bytes(self.source, opener=self._opener), package_id=package_id
        )
        # A failed download clears staging. A manual retry must recreate it.
        _check_storage(((self.staging_dir, MAX_FEED_BYTES),))
        # Start from a clean staging area so a stale package can never be
        # installed in place of the one this check announced.
        for stale in self.staging_dir.glob("*"):
            if stale.is_file():
                stale.unlink()
        (self.staging_dir / f"releases.{CHANNEL}.json").write_text(
            json.dumps({"Assets": list(assets)}, ensure_ascii=True), encoding="utf-8"
        )
        # Velopack decides full versus delta from its own local packages, so
        # the offer names exactly the files it is about to read.
        info = self._manager.check_for_updates()
        if info is None:
            return None
        full = self._asset(assets, info.TargetFullRelease.FileName)
        deltas = tuple(self._asset(assets, item.FileName) for item in info.DeltasToTarget)
        files = deltas if deltas else (full,)
        return UpdateOffer(
            version=str(info.TargetFullRelease.Version),
            download_bytes=sum(int(item["Size"]) for item in files),
            is_delta=bool(deltas),
            info=info,
            files=files,
            full=full,
        )

    @staticmethod
    def _asset(assets: tuple[dict[str, Any], ...], name: str) -> dict[str, Any]:
        for asset in assets:
            if asset["FileName"] == name:
                return asset
        raise UpdateError("update feed changed during the check")

    def download(
        self, offer: UpdateOffer, progress: Callable[[int], None] | None = None
    ) -> None:
        full_size = int(offer.full["Size"])
        delta_size = offer.download_bytes if offer.is_delta else 0
        requirements = [(self.staging_dir, full_size + delta_size)]
        if self._packages_dir is not None and self._install_root is not None:
            requirements.extend([
                (self._install_root, 0),
                (self._packages_dir, 2 * full_size + delta_size),
                (Path(tempfile.gettempdir()), 2 * full_size),
            ])
        _check_storage(tuple(requirements))
        for asset in offer.files:
            download_asset(
                self.source, asset, self.staging_dir, opener=self._opener, progress=progress
            )
        try:
            self._manager.download_updates(offer.info)
            return
        except Exception:
            if not offer.is_delta:
                raise UpdateError("update could not be prepared") from None
            logger.warning("delta update failed; fetching the full package", exc_info=True)
        # Velopack falls back to the full package when a delta cannot be
        # applied; stage it and let the same call take that path.
        download_asset(
            self.source, offer.full, self.staging_dir, opener=self._opener, progress=progress
        )
        try:
            self._manager.download_updates(offer.info)
        except Exception as exc:
            raise UpdateError("update could not be prepared") from exc

    def apply_and_restart(self, offer: UpdateOffer) -> None:
        """Start the installer, which waits for this process to exit first.

        The caller must quit the application promptly afterwards: Velopack
        waits up to 60 seconds, then replaces the program folder and restarts.
        """

        requirements = [(self.staging_dir.parent, 0)]
        if self._packages_dir is not None and self._install_root is not None:
            # Recheck after download and any patrol/task deferral. Use actual
            # uncompressed sizes for extraction, rather than delta size.
            try:
                with zipfile.ZipFile(self._packages_dir / offer.full["FileName"]) as package:
                    unpacked = sum(item.file_size for item in package.infolist())
            except (OSError, zipfile.BadZipFile) as exc:
                raise UpdateError("prepared update package is unreadable") from exc
            requirements.extend([
                (self._install_root, unpacked),
                (self._packages_dir, 0),
                (Path(tempfile.gettempdir()), unpacked),
            ])
            if self._current_dir is not None:
                requirements.append((self._current_dir, 0))
        _check_storage(tuple(requirements))
        # The native installer runs after we exit, so its result cannot be
        # caught here. On failure it restarts the old app too. Persist the
        # intended transition before handing over, then inspect it at startup.
        temporary = self._attempt_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps({"source_version": self.current_version,
                            "target_version": offer.version}),
                encoding="utf-8",
            )
            os.replace(temporary, self._attempt_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise UpdateError("update attempt could not be recorded") from exc
        try:
            self._manager.wait_exit_then_apply_updates(offer.info, silent=False, restart=True)
        except Exception:
            self._attempt_path.unlink(missing_ok=True)
            raise

    def discard_staging(self) -> None:
        shutil.rmtree(self.staging_dir, ignore_errors=True)
