"""Shared Qt lifetime for the Windows test process.

Qt supports one QApplication per process.  Several historical test modules
created an application only in a local variable, allowing Python to destroy
and recreate the native singleton between modules.  That can finish all
assertions and still crash python.exe during Windows interpreter teardown.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_SESSION_QT_APP: QApplication | None = None


def pytest_sessionstart(session: object) -> None:
    """Create and retain exactly one native QApplication for the test session."""
    del session
    global _SESSION_QT_APP
    _SESSION_QT_APP = QApplication.instance() or QApplication([])


def pytest_sessionfinish(session: object, exitstatus: int) -> None:
    """Destroy leaked Qt windows while the retained QApplication is alive."""
    del session, exitstatus
    if _SESSION_QT_APP is None:
        return
    # Bypass business close guards at process teardown.  Merely calling close
    # can leave an unsaved workbench alive; leaving native widgets to Python's
    # interpreter teardown is unsafe on the Windows Qt platform plugin.
    for widget in _SESSION_QT_APP.topLevelWidgets():
        widget.hide()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    _SESSION_QT_APP.processEvents()
