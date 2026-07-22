# -*- coding: utf-8 -*-
"""Main QZ Briefing dashboard window; reads files only and never calls Kiwoom."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .dashboard_view_model import DashboardViewModel
from .formatters import money, number, percent, status_label
from .tray_controller import TrayController


HOLDING_COLUMNS = ("종목코드", "종목명", "마스킹 계좌", "수량", "평단", "현재가", "투자금액", "평가금액", "평가손익", "수익률", "추세", "바닥 확인", "포지션 검토", "경고")
LEADERSHIP_COLUMNS = ("시장", "종목코드", "종목명", "현재가", "등락률", "거래대금", "RSI", "MACD", "추세", "선정 이유", "주의사항")
WATCH_COLUMNS = ("분류", "종목 또는 지표", "현재 상태", "확인 조건", "위험 조건")


class DashboardMainWindow(QMainWindow):
    briefing_completed = pyqtSignal(str)

    def __init__(
        self, root: Path, *, connection_state: Callable[[], object],
        trading_day_status: str, shutdown: Callable[[], None],
        open_folder: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        super().__init__()
        self._root, self._clock, self._connection_state = Path(root), clock, connection_state
        self._open_folder = open_folder or (lambda: os.startfile(str(self._root)))
        self._trading_day_status, self._background_notice_shown = trading_day_status, False
        self._view_model = DashboardViewModel(root, clock=clock)
        self._runtime_messages: list[str] = []
        self._file_messages: list[str] = []
        self.setWindowTitle("QZ Briefing 대시보드"); self.resize(1400, 850)
        self._status_labels = {name: QLabel() for name in ("connection", "calendar", "clock", "next", "last", "shutdown")}
        self._tabs = QTabWidget(); self._result_views = {}
        self._summary = QTextBrowser(); self._holdings = self._table(HOLDING_COLUMNS)
        self._holdings_summary = QLabel()
        self._leadership = self._table(LEADERSHIP_COLUMNS); self._watchlist = self._table(WATCH_COLUMNS)
        self._messages = QTextBrowser()
        self._build_ui()
        self.tray = TrayController(self, show_window=self.show_dashboard, refresh=self.refresh, open_folder=self._open_folder, shutdown=shutdown)
        self.briefing_completed.connect(lambda _: self.refresh())
        self._timer = QTimer(self); self._timer.timeout.connect(self._update_status); self._timer.start(1000)
        self.refresh()

    @staticmethod
    def _table(columns) -> QTableWidget:
        table = QTableWidget(0, len(columns)); table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers); table.setSelectionBehavior(QAbstractItemView.SelectRows)
        return table

    def _build_ui(self) -> None:
        central = QWidget(); layout = QVBoxLayout(central); status = QHBoxLayout()
        labels = (("connection", "키움"), ("calendar", "거래일"), ("clock", "현재"), ("next", "다음"), ("last", "마지막"), ("shutdown", "종료"))
        for key, title in labels: status.addWidget(QLabel(f"{title}:")); status.addWidget(self._status_labels[key])
        layout.addLayout(status)
        self._tabs.addTab(self._summary, "오늘 요약")
        for key, title in (("pre_market", "장전 브리핑"), ("intraday_10am", "오전 10시 브리핑"), ("market_close", "장마감 브리핑")):
            view = QTextBrowser(); self._result_views[key] = view; self._tabs.addTab(view, title)
        holdings_tab = QWidget(); holdings_layout = QVBoxLayout(holdings_tab)
        holdings_layout.addWidget(self._holdings_summary); holdings_layout.addWidget(self._holdings)
        self._tabs.addTab(holdings_tab, "보유종목"); self._tabs.addTab(self._leadership, "주도주·반등 후보")
        self._tabs.addTab(self._watchlist, "다음 거래일 관찰목록"); self._tabs.addTab(self._messages, "오류·경고")
        layout.addWidget(self._tabs)
        buttons = QHBoxLayout()
        for label, callback in (("새로고침", self.refresh), ("브리핑 폴더 열기", self._open_folder), ("창 숨기기", self.hide)):
            button = QPushButton(label); button.clicked.connect(callback); buttons.addWidget(button)
        layout.addLayout(buttons); self.setCentralWidget(central)

    @property
    def tab_count(self) -> int: return self._tabs.count()

    def handle_briefing_completed(self, briefing_name: str) -> None:
        if not self._timer.isActive(): return
        self.briefing_completed.emit(briefing_name)

    def refresh(self) -> None:
        model = self._view_model.load_today(); summary = model["summary"]
        self._summary.setPlainText("\n".join(f"{key}: {value}" for key, value in summary.items()))
        for key, view in self._result_views.items():
            wrapper = model["results"][key]; payload = wrapper.get("json")
            if not isinstance(payload, dict):
                view.setPlainText(f"아직 생성된 브리핑이 없습니다 (예정: {wrapper['next_time']})\n{wrapper.get('error') or ''}")
            else:
                text = f"생성시각: {payload.get('completed_at') or payload.get('metadata', {}).get('generated_at', '-')}\n\n"
                text += json.dumps({"analysis": payload.get("analysis"), "market_close_analysis": payload.get("market_close_analysis"), "warnings": payload.get("warnings", []), "errors": payload.get("errors", [])}, ensure_ascii=False, indent=2)
                text += "\n\n" + str(wrapper.get("markdown") or "")
                if key == "market_close":
                    validation = model["results"]["market_close_validation"]
                    if isinstance(validation.get("json"), dict): text += "\n\n[수동 validation 결과 별도 존재]\n" + str(validation.get("markdown") or "")
                view.setPlainText(text)
        self._populate_holdings(model["holdings"]); self._populate_leadership(model["leadership"]); self._populate_watchlist(model["watchlist"])
        self._file_messages = list(model["messages"])
        self._messages.setPlainText("\n".join(self._file_messages + self._runtime_messages) or "오류·경고 없음")
        latest = model.get("latest", {}); self._status_labels["last"].setText(str(latest.get("briefing_type", "없음")) if isinstance(latest, dict) else "없음")
        self._update_status()

    def _populate_holdings(self, data: dict[str, object]) -> None:
        rows = data.get("rows", []); self._holdings.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (row.get("code"), row.get("name"), row.get("account"), number(row.get("quantity")), money(row.get("average_price")), money(row.get("current_price")), money(row.get("investment_amount")), money(row.get("valuation_amount")), money(row.get("profit_loss")), percent(row.get("profit_rate")), status_label(row.get("trend")), status_label(row.get("bottom_confirmation")), status_label(row.get("review_status")), "; ".join(row.get("warnings", [])))
            for column, value in enumerate(values): self._holdings.setItem(row_index, column, QTableWidgetItem(str(value or "-")))
        portfolio = data.get("portfolio", {})
        summary = f"계좌 {data.get('account_count', 0)} / 종목 {data.get('holding_count', 0)} / 총 투자금액 {money(portfolio.get('investment_amount'))} / 총 평가금액 {money(portfolio.get('valuation_amount'))} / 총 평가손익 {money(portfolio.get('profit_loss'))} / 전체 수익률 {percent(portfolio.get('profit_rate'))} / 출처 {data.get('source')}"
        self._holdings_summary.setText(summary); self._holdings.setToolTip(summary)

    def _populate_leadership(self, rows) -> None:
        self._leadership.setRowCount(len(rows))
        for index, row in enumerate(rows):
            macd = row.get("macd") if isinstance(row.get("macd"), dict) else {}
            values = (row.get("market"), row.get("code"), row.get("name"), money(row.get("current_price")), percent(row.get("change_rate")), money(row.get("trading_value")), row.get("rsi14", "-"), macd.get("histogram", "-"), status_label(row.get("trend")), ", ".join(row.get("reasons", [])), ", ".join(row.get("warnings", [])))
            for column, value in enumerate(values): self._leadership.setItem(index, column, QTableWidgetItem(str(value or "-")))

    def _populate_watchlist(self, rows) -> None:
        self._watchlist.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (row.get("category"), row.get("name") or row.get("code"), status_label(row.get("current_state")), row.get("confirmation_condition"), row.get("risk_condition"))
            for column, value in enumerate(values): self._watchlist.setItem(index, column, QTableWidgetItem(str(value or "-")))

    def _update_status(self) -> None:
        now = self._clock(); connection = self._connection_state()
        self._status_labels["connection"].setText(status_label(getattr(connection, "name", connection)))
        self._status_labels["calendar"].setText(self._trading_day_status); self._status_labels["clock"].setText(now.strftime("%H:%M:%S"))
        schedule = [(8, 0, "장전 감시"), (9, 0, "장전 브리핑"), (10, 0, "10시 브리핑"), (15, 40, "장마감 브리핑")]
        self._status_labels["next"].setText(next((f"{hour:02d}:{minute:02d} {name}" for hour, minute, name in schedule if (now.hour, now.minute) < (hour, minute)), "오늘 일정 완료"))
        self._status_labels["shutdown"].setText("20:00")

    def show_dashboard(self) -> None: self.showNormal(); self.raise_(); self.activateWindow()

    def handle_connection_state(self, state: object) -> None:
        if not self._timer.isActive(): return
        name = getattr(state, "name", str(state)); timestamp = self._clock().isoformat(timespec="seconds")
        messages = {
            "RECHECKING": "키움 연결 상태 불일치를 재확인합니다",
            "RECONNECT_WAIT": "키움 연결이 끊어져 재연결을 시도합니다",
            "RECONNECTING": "키움 연결 재시도 중입니다",
            "CONNECTED": "키움 연결이 복구되었습니다",
            "FAILED": "자동 복구에 실패했습니다. 프로그램 상태를 확인하세요",
        }
        if name in messages:
            message = f"{timestamp} {messages[name]}"; self._runtime_messages.append(message)
            self.tray.icon.showMessage("QZ Briefing", messages[name])
        self._update_status(); self._messages.setPlainText("\n".join(self._file_messages + self._runtime_messages))

    def closeEvent(self, event) -> None:
        event.ignore(); self.hide()
        if not self._background_notice_shown:
            self._background_notice_shown = True; self.tray.notify_background()

    def stop(self) -> None:
        self._timer.stop(); self.tray.stop()
