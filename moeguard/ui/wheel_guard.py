'Keep the mouse wheel from editing number boxes, drop-downs and sliders.'

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QScrollBar,
    QWidget,
)

_GUARDED = (QAbstractSpinBox, QComboBox, QAbstractSlider)
_MARK = "moeguard_wheel_guarded"


def _scroll_area_of(widget: QWidget) -> QAbstractScrollArea | None:
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


class _WheelGuard(QObject):
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Wheel:
            return False
        area = _scroll_area_of(obj) if isinstance(obj, QWidget) else None
        if area is None:
            # Nothing to scroll; leaving the event unaccepted lets Qt offer a
            # real wheel event to the parents as usual.
            event.ignore()
            return True
        # Qt only propagates *system* wheel events to parents, so forward
        # explicitly: one path for real and synthesized events alike, and
        # accepting it here stops the spontaneous propagation from scrolling
        # the page a second time.
        viewport = area.viewport()
        position = QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint()))
        forwarded = QWheelEvent(
            position,
            event.globalPosition(),
            event.pixelDelta(),
            event.angleDelta(),
            event.buttons(),
            event.modifiers(),
            event.phase(),
            event.inverted(),
        )
        QApplication.sendEvent(viewport, forwarded)
        event.accept()
        return True


def guard_wheel(root: QWidget) -> int:
    """Guard every wheel-sensitive input under ``root``; returns how many were new."""
    guard = root.findChild(_WheelGuard, "moeguardWheelGuard")
    if guard is None:
        guard = _WheelGuard(root)
        guard.setObjectName("moeguardWheelGuard")
    count = 0
    for cls in _GUARDED:
        for widget in root.findChildren(cls):
            if isinstance(widget, QScrollBar) or widget.property(_MARK):
                continue
            widget.setProperty(_MARK, True)

            widget.setFocusPolicy(Qt.StrongFocus)
            widget.installEventFilter(guard)
            count += 1
    return count
