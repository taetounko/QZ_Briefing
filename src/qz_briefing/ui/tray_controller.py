# -*- coding: utf-8 -*-
"""System-tray controls that never force-terminate the process."""

from __future__ import annotations

from collections.abc import Callable

from PyQt5.QtWidgets import QAction, QMenu, QSystemTrayIcon


class TrayController:
    def __init__(self, parent, *, show_window: Callable[[], None], refresh: Callable[[], None], open_folder: Callable[[], None], shutdown: Callable[[], None]) -> None:
        self.icon = QSystemTrayIcon(parent)
        menu = QMenu(parent)
        for label, callback in (
            ("QZ Briefing 열기", show_window),
            ("최신 결과 새로고침", refresh),
            ("브리핑 폴더 열기", open_folder),
            ("종료", shutdown),
        ):
            action = QAction(label, parent); action.triggered.connect(callback); menu.addAction(action)
        self.icon.setContextMenu(menu)
        self.icon.setToolTip("QZ Briefing")
        self.icon.activated.connect(lambda reason: show_window() if reason == QSystemTrayIcon.DoubleClick else None)
        if QSystemTrayIcon.isSystemTrayAvailable(): self.icon.show()

    def notify_background(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.icon.showMessage("QZ Briefing", "창을 닫아도 트레이에서 브리핑과 예약 작업이 계속 실행됩니다.")

    def stop(self) -> None:
        self.icon.hide()
