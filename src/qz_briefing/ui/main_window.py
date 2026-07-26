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


HOLDING_COLUMNS = ("우선순위", "종목코드", "종목명", "마스킹 계좌", "수량", "평단", "현재가", "투자금액", "평가금액", "평가손익", "수익률", "추세", "바닥 확인", "포지션 검토", "판단 신뢰도", "행동 수준", "핵심 이유", "확인 조건", "위험 조건", "경고")
LEADERSHIP_COLUMNS = ("시장", "종목코드", "종목명", "현재가", "등락률", "거래대금", "RSI", "MACD", "추세", "선정 이유", "주의사항")
WATCH_COLUMNS = ("분류", "종목 또는 지표", "현재 상태", "확인 조건", "위험 조건")


class DashboardMainWindow(QMainWindow):
    briefing_completed = pyqtSignal(str)

    def __init__(
        self, root: Path, *, connection_state: Callable[[], object],
        trading_day_status: str, shutdown: Callable[[], None],
        open_folder: Callable[[], None] | None = None,
        recommendation_root: Path | None = None,
        read_only: bool = False,
        standalone: bool = False,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        super().__init__()
        self._root, self._clock, self._connection_state = Path(root), clock, connection_state
        self._open_folder = open_folder or (lambda: os.startfile(str(self._root)))
        self._trading_day_status, self._background_notice_shown = trading_day_status, False
        self._read_only, self._standalone, self._shutdown = read_only, standalone, shutdown
        self._view_model = DashboardViewModel(root, recommendation_root=recommendation_root, clock=clock)
        self._runtime_messages: list[str] = []
        self._file_messages: list[str] = []
        self.setWindowTitle("QZ Briefing 대시보드"); self.resize(1400, 850)
        self._status_labels = {name: QLabel() for name in ("connection", "calendar", "clock", "next", "last", "shutdown")}
        self._tabs = QTabWidget(); self._result_views = {}
        self._summary = QTextBrowser(); self._holdings = self._table(HOLDING_COLUMNS)
        self._holdings_summary = QLabel()
        self._holding_detail = QTextBrowser()
        self._leadership = self._table(LEADERSHIP_COLUMNS); self._watchlist = self._table(WATCH_COLUMNS)
        self._messages = QTextBrowser(); self._recommendations = QTextBrowser()
        self._build_ui()
        self.tray = TrayController(self, show_window=self.show_dashboard, refresh=self.refresh, open_folder=self._open_folder, shutdown=shutdown)
        self.briefing_completed.connect(lambda _: self.refresh())
        self._timer = QTimer(self); self._timer.timeout.connect(self._update_status); self._timer.start(1000)
        self._refresh_timer = QTimer(self); self._refresh_timer.timeout.connect(self.refresh); self._refresh_timer.start(30000)
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
        if self._read_only:
            badge = QLabel("READ_ONLY · 저장 결과 조회 전용")
            badge.setStyleSheet("font-weight: bold; color: #9a6700; padding: 6px;")
            layout.addWidget(badge)
        layout.addLayout(status)
        self._tabs.addTab(self._summary, "오늘 요약")
        for key, title in (("pre_market", "장전 브리핑"), ("intraday_10am", "오전 10시 브리핑"), ("market_close", "장마감 브리핑")):
            view = QTextBrowser(); self._result_views[key] = view; self._tabs.addTab(view, title)
        holdings_tab = QWidget(); holdings_layout = QVBoxLayout(holdings_tab)
        holdings_layout.addWidget(self._holdings_summary); holdings_layout.addWidget(self._holdings)
        holdings_layout.addWidget(self._holding_detail)
        self._holdings.itemSelectionChanged.connect(self._show_holding_detail)
        self._tabs.addTab(self._recommendations, "일일 추천")
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
        runtime = model.get("runtime", {})
        runtime_lines = ["", "[운영상태]"] + [
            f"{key}: {runtime.get(key, '-')}" for key in (
                "started_at", "last_heartbeat_at", "health", "connection_state",
                "active_briefing", "next_scheduled_task", "last_completed_briefing",
                "shutdown_scheduled_at", "telegram_configured", "telegram_enabled",
                "telegram_last_success_at", "telegram_last_event",
                "telegram_pending_count", "telegram_last_error", "telegram_next_attempt_at",
            )
        ]
        self._summary.setPlainText(self._safe_text("\n".join(f"{key}: {value}" for key, value in summary.items()) + "\n" + "\n".join(runtime_lines)))
        for key, view in self._result_views.items():
            wrapper = model["results"][key]; payload = wrapper.get("json")
            if not isinstance(payload, dict):
                view.setPlainText(f"아직 생성된 브리핑이 없습니다 (예정: {wrapper['next_time']})\n{wrapper.get('error') or ''}")
            else:
                text = f"생성시각: {payload.get('completed_at') or payload.get('metadata', {}).get('generated_at', '-')}\n\n"
                text += json.dumps({"analysis": payload.get("analysis"), "market_close_analysis": payload.get("market_close_analysis"), "warnings": payload.get("warnings", []), "errors": payload.get("errors", [])}, ensure_ascii=False, indent=2)
                text += "\n\n" + str(wrapper.get("markdown") or "")
                if key == "market_close" and not self._read_only:
                    validation = model["results"]["market_close_validation"]
                    if isinstance(validation.get("json"), dict): text += "\n\n[수동 validation 결과 별도 존재]\n" + str(validation.get("markdown") or "")
                view.setPlainText(self._safe_text(text))
        self._populate_holdings(model["holdings"]); self._populate_leadership(model["leadership"]); self._populate_watchlist(model["watchlist"])
        self._file_messages = list(model["messages"])
        self._messages.setPlainText(self._safe_text("\n".join(self._file_messages + self._runtime_messages) or "오류·경고 없음"))
        self._render_recommendations(model.get("recommendations", {}))
        latest = model.get("latest", {}); self._status_labels["last"].setText(str(latest.get("briefing_type", "없음")) if isinstance(latest, dict) else "없음")
        self._update_status()

    def _render_recommendations(self, wrapper: dict[str, object]) -> None:
        report = wrapper.get("report") if isinstance(wrapper, dict) else None
        if not isinstance(report, dict):
            self._recommendations.setPlainText("저장된 최근 브리핑 또는 추천 결과가 없습니다.")
            return
        rows = []
        for group in ("strong", "review"):
            for item in report.get(group, [])[:3]:
                if isinstance(item, dict): rows.append(item)
        if not rows:
            self._recommendations.setPlainText("현재 기준을 충족한 추천 후보가 없습니다.")
            return
        lines = [
            f"추천 보고서 생성 시각: {report.get('generated_at') or '-'}",
            f"데이터 기준 시각: {report.get('data_as_of') or '-'}",
            f"완전 강추 {report.get('strong_count', 0)} / 추가 검토 {report.get('review_count', 0)}", "",
        ]
        if wrapper.get("recent_failure_at"):
            lines.insert(3, f"최근 추천 생성 실패 기록: {wrapper['recent_failure_at']} (상세 내용은 표시하지 않음)")
        for item in rows:
            lines.extend([
                f"[{item.get('grade') or '-'}] {item.get('name') or '-'} ({item.get('code') or '-'}) · {item.get('market') or '-'}",
                f"종합점수 {item.get('total_score', '-')} / 신뢰도 {item.get('confidence', '-')}",
                f"완성 주봉 {item.get('weekly_close', '-')} / MA5 {item.get('weekly_ma5', '-')} / 이격률 {item.get('weekly_distance_rate', '-')}",
                "근거: " + "; ".join(str(x) for x in item.get("reasons", [])[:4]),
                "부족 자료: " + ("; ".join(str(x) for x in item.get("missing", [])) or "없음"),
                "주요 위험: " + ("; ".join(str(x) for x in item.get("risks", [])) or "없음"),
                f"추격매수 금지: {'예' if item.get('chase_buying_prohibited') else '아니오'}",
                "무효화 조건: " + ("; ".join(str(x) for x in item.get("invalidation_conditions", [])) or "자료 없음"), "",
            ])
        self._recommendations.setPlainText(self._safe_text("\n".join(lines)))

    def _safe_text(self, text: str) -> str:
        project_root = str(self._root.parent.parent)
        return text.replace(project_root, "[내부 경로 숨김]").replace("None", "-").replace("null", "자료 부족").replace("unknown", "자료 부족")

    def _populate_holdings(self, data: dict[str, object]) -> None:
        rows = data.get("rows", []); self._holdings.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            decision = row.get("decision") if isinstance(row.get("decision"), dict) else {}
            values = (row.get("priority"), row.get("code"), row.get("name"), row.get("account"), number(row.get("quantity")), money(row.get("average_price")), money(row.get("current_price")), money(row.get("investment_amount")), money(row.get("valuation_amount")), money(row.get("profit_loss")), percent(row.get("profit_rate")), status_label(row.get("trend")), status_label(row.get("bottom_confirmation")), status_label(row.get("review_status")), decision.get("confidence"), status_label(decision.get("action_level")), decision.get("summary"), "; ".join(decision.get("positive_conditions", [])), "; ".join(decision.get("risk_conditions", [])), "; ".join(row.get("warnings", [])))
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value or "-")); cell.setToolTip(str(value or "-")); self._holdings.setItem(row_index, column, cell)
        portfolio = data.get("portfolio", {})
        summary = f"계좌 {data.get('account_count', 0)} / 종목 {data.get('holding_count', 0)} / 총 투자금액 {money(portfolio.get('investment_amount'))} / 총 평가금액 {money(portfolio.get('valuation_amount'))} / 총 평가손익 {money(portfolio.get('profit_loss'))} / 전체 수익률 {percent(portfolio.get('profit_rate'))} / 출처 {data.get('source')}"
        self._holdings_summary.setText(summary); self._holdings.setToolTip(summary)

    def _show_holding_detail(self) -> None:
        row = self._holdings.currentRow()
        if row < 0:
            self._holding_detail.clear(); return
        values = [self._holdings.item(row, column).text() if self._holdings.item(row, column) else "-" for column in range(self._holdings.columnCount())]
        self._holding_detail.setPlainText("\n".join(f"{HOLDING_COLUMNS[index]}: {value}" for index, value in enumerate(values)))

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
        if self._standalone:
            event.accept(); self.stop(); self._shutdown(); return
        event.ignore(); self.hide()
        if not self._background_notice_shown:
            self._background_notice_shown = True; self.tray.notify_background()

    def stop(self) -> None:
        self._timer.stop(); self._refresh_timer.stop(); self.tray.stop()
