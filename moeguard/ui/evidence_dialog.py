"""本地证据管理窗口：查看、删除单条或全部证据。

视觉层（feat/ui-modernization）：
- 空状态提示（列表为空时仍可直接打开证据文件夹）。
- 普通操作（打开）与危险操作（删除）分层：危险按钮使用红色样式并靠右隔离。
- 不增加缩略图（避免隐私、I/O 与内存负担）；不改变永久删除与二次确认语义。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from moeguard.storage.db import Database
from moeguard.storage.evidence_store import EvidenceStore
from moeguard.ui import theme


class EvidenceDialog(QDialog):
    """只管理本机证据，不上传或预览其中的人脸内容。"""

    def __init__(
        self,
        evidence_store: EvidenceStore,
        database: Database,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("evidenceDialog")
        self.setWindowTitle("本地证据管理")
        self.setMinimumWidth(560)
        self.setStyleSheet(theme.dialog_qss("evidenceDialog"))
        self._store = evidence_store
        self._db = database

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title = QLabel("本机证据")
        title.setProperty("role", "title")
        layout.addWidget(title)

        hint = QLabel("证据仅保存在本机，不会上传。删除后无法恢复。")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._events = QListWidget()
        self._events.setSelectionMode(QListWidget.SingleSelection)
        self._events.setAlternatingRowColors(True)
        self._events.currentItemChanged.connect(self._update_selection_actions)
        layout.addWidget(self._events, 1)

        # 空状态：轻量文案占位，列表为空时显示（不遮挡列表交互）
        self._empty_hint = QLabel("暂无证据事件\n发生陌生人或运动事件后会出现在这里。")
        self._empty_hint.setProperty("role", "hint")
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setWordWrap(True)
        layout.addWidget(self._empty_hint)

        # 操作行：左侧普通查看操作，右侧危险删除操作
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.open_folder_button = QPushButton("打开证据文件夹")
        self.open_button = QPushButton("打开选中证据目录")
        self.delete_selected_button = QPushButton("删除选中证据…")
        self.delete_all_button = QPushButton("删除全部证据…")
        self.delete_all_button.setObjectName("dangerButton")

        self.open_folder_button.setStyleSheet(theme.button_qss("normal"))
        self.open_button.setStyleSheet(theme.button_qss("normal"))
        self.delete_selected_button.setStyleSheet(theme.button_qss("normal"))
        self.delete_all_button.setStyleSheet(theme.button_qss("danger"))

        actions.addWidget(self.open_folder_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.delete_selected_button)
        actions.addStretch()
        actions.addWidget(self.delete_all_button)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.open_folder_button.clicked.connect(self._open_evidence_folder)
        self.open_button.clicked.connect(self._open_selected)
        self.delete_selected_button.clicked.connect(self._delete_selected)
        self.delete_all_button.clicked.connect(self._delete_all)
        self._refresh()

    def _refresh(self) -> None:
        self._events.clear()
        for event in self._db.incidents_since(0):
            timestamp = datetime.fromtimestamp(event["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            kind = event["kind"]
            summary = event["summary"] or "无摘要"
            label = f"{timestamp}   〔{kind}〕  {summary}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, dict(event))
            self._events.addItem(item)
        is_empty = self._events.count() == 0
        self._empty_hint.setVisible(is_empty)
        self._events.setVisible(not is_empty)
        self.delete_all_button.setEnabled(not is_empty)
        has_selection = self._events.currentItem() is not None
        self.open_button.setEnabled(has_selection)
        self.delete_selected_button.setEnabled(has_selection)

    def _update_selection_actions(self, current, _previous) -> None:
        enabled = current is not None
        self.open_button.setEnabled(enabled)
        self.delete_selected_button.setEnabled(enabled)

    def _selected_event(self) -> dict | None:
        item = self._events.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def _open_selected(self) -> None:
        event = self._selected_event()
        if event is None or not event.get("evidence"):
            return
        self._open_directory(Path(event["evidence"]))

    def _open_evidence_folder(self) -> None:
        """即使没有任何事件，用户也可查看证据根目录。"""
        self._open_directory(self._store.base_dir)

    @staticmethod
    def _open_directory(path: Path) -> None:
        if path.is_dir() and os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif path.is_dir():
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _delete_selected(self) -> None:
        event = self._selected_event()
        if event is None:
            return
        result = QMessageBox.warning(
            self,
            "删除选中证据？",
            "将永久删除这条事件的截图、视频和本地索引，无法恢复。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Yes:
            return
        evidence = event.get("evidence")
        try:
            if evidence:
                self._store.delete_event(Path(evidence))
        except Exception as exc:
            QMessageBox.critical(
                self,
                "证据未删除",
                f"本地媒体删除失败，事件索引已保留。请关闭占用文件后重试。\n\n{exc}",
            )
            self._refresh()
            return
        try:
            self._db.delete_incident(int(event["id"]))
        except Exception as exc:
            QMessageBox.critical(
                self,
                "索引清理失败",
                f"本地媒体已删除，但事件索引未能清理；下次维护会重试。\n\n{exc}",
            )
        self._refresh()

    def _delete_all(self) -> None:
        result = QMessageBox.warning(
            self,
            "删除全部证据？",
            "将永久删除全部截图、视频和本地事件索引，无法恢复。"
            "主人注册和自动值守设置将保留。",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if result != QMessageBox.Yes:
            return
        try:
            self._store.delete_all()
        except Exception as exc:
            reconcile_error = ""
            try:
                self._db.delete_incidents_with_missing_evidence()
            except Exception as db_exc:
                reconcile_error = f"\n索引同步也失败：{db_exc}"
            QMessageBox.critical(
                self,
                "证据未全部删除",
                "仍有本地媒体无法删除；对应索引会保留以便重试。"
                f"\n\n{exc}{reconcile_error}",
            )
            self._refresh()
            return
        try:
            self._db.delete_all_incidents()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "索引清理失败",
                f"媒体已删除，但事件索引未能清理；下次维护会重试。\n\n{exc}",
            )
        self._refresh()
