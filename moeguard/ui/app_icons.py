"""Application and tray icons shipped with MoeGuard."""

from __future__ import annotations

from PySide6.QtGui import QIcon

from moeguard.utils.paths import resource_path


def application_icons() -> tuple[QIcon, QIcon]:
    """Return companion and patrol icons from read-only packaged resources."""
    companion = QIcon(str(resource_path("icons", "moeguard-companion.png")))
    patrol = QIcon(str(resource_path("icons", "moeguard-patrol.png")))
    return companion, patrol
