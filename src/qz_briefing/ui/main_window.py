# -*- coding: utf-8 -*-
"""Main QZ Briefing dashboard window; reads files only and never calls Kiwoom."""

from __future__ import annotations

import os
from html import escape
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QPushButton, QScrollArea, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .dashboard_view_model import DashboardViewModel
from .formatters import money, number, percent, status_label
from .tray_controller import DisabledTrayController, TrayController


HOLDING_COLUMNS = ("우선순위", "종목코드", "종목명", "마스킹 계좌", "수량", "평단", "현재가", "투자금액", "평가금액", "평가손익", "수익률", "추세", "바닥 확인", "포지션 검토", "판단 신뢰도", "행동 수준", "핵심 이유", "확인 조건", "위험 조건", "경고")
LEADERSHIP_COLUMNS = ("시장", "종목코드", "종목명", "현재가", "등락률", "거래대금", "RSI", "MACD", "추세", "선정 이유", "주의사항")
WATCH_COLUMNS = ("분류", "종목 또는 지표", "현재 상태", "확인 조건", "위험 조건")
REQUIRED_READONLY_TABS = (
    "오늘 요약", "장전 브리핑", "코스피 주도주", "코스닥 주도주", "보유종목",
    "오전 10시 브리핑", "장마감 브리핑", "일일 추천", "운영 상태",
)
LEADER_COLUMNS = ("순위", "종목명", "종목코드", "등락률", "거래대금", "외국인", "기관", "핵심 근거", "위험", "업종/테마", "기술적 위치")


class DashboardMainWindow(QMainWindow):
    briefing_completed = pyqtSignal(str)

    def __init__(
        self, root: Path, *, connection_state: Callable[[], object],
        trading_day_status: str, shutdown: Callable[[], None],
        open_folder: Callable[[], None] | None = None,
        recommendation_root: Path | None = None,
        read_only: bool = False,
        standalone: bool = False,
        next_trading_day: Callable[[date], date | None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        super().__init__()
        self._root, self._clock, self._connection_state = Path(root), clock, connection_state
        self._open_folder = open_folder or (lambda: os.startfile(str(self._root)))
        self._trading_day_status, self._background_notice_shown = trading_day_status, False
        self._read_only, self._standalone, self._shutdown = read_only, standalone, shutdown
        self._next_trading_day = next_trading_day or self._weekday_after
        self._view_model = DashboardViewModel(root, recommendation_root=recommendation_root, clock=clock)
        self._runtime_messages: list[str] = []
        self._file_messages: list[str] = []
        self.setWindowTitle("QZ 브리핑"); self.resize(1280, 820); self.setMinimumSize(960, 640)
        self._status_labels = {name: QLabel() for name in ("connection", "calendar", "clock", "next", "last", "shutdown", "data_as_of", "pre_market", "intraday", "market_close", "recommendation")}
        self._tabs = QTabWidget(); self._result_views = {}
        self._summary = QTextBrowser(); self._holdings = self._table(HOLDING_COLUMNS)
        self._holdings_summary = QLabel()
        self._holding_detail = QTextBrowser()
        self._leadership = self._table(LEADERSHIP_COLUMNS); self._watchlist = self._table(WATCH_COLUMNS)
        self._messages = QTextBrowser()
        self._recommendations, self._recommendation_layout = self._card_area()
        self._leader_views = {"kospi": self._leader_table(), "kosdaq": self._leader_table()}
        self._leader_notices = {key: QLabel() for key in self._leader_views}
        self._leader_tabs = {key: self._leader_container(key) for key in self._leader_views}
        self._readonly_holdings, self._holding_layout = self._card_area()
        self._holding_cards: list[QFrame] = []; self._recommendation_cards: list[QFrame] = []
        self._build_ui()
        self.tray = DisabledTrayController() if self._read_only else TrayController(self, show_window=self.show_dashboard, refresh=self.refresh, open_folder=self._open_folder, shutdown=shutdown)
        self.briefing_completed.connect(lambda _: self.refresh())
        self._timer = QTimer(self); self._timer.timeout.connect(self._update_status); self._timer.start(1000)
        self._refresh_timer = QTimer(self); self._refresh_timer.timeout.connect(self.refresh); self._refresh_timer.start(30000)
        self.refresh()

    @staticmethod
    def _table(columns) -> QTableWidget:
        table = QTableWidget(0, len(columns)); table.setHorizontalHeaderLabels(columns)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers); table.setSelectionBehavior(QAbstractItemView.SelectRows)
        return table

    @staticmethod
    def _card_area() -> tuple[QScrollArea, QVBoxLayout]:
        area=QScrollArea(); area.setWidgetResizable(True); content=QWidget(); layout=QVBoxLayout(content)
        layout.setContentsMargins(16,16,16,16); layout.setSpacing(12); layout.setAlignment(Qt.AlignTop)
        area.setWidget(content); return area,layout

    @staticmethod
    def _leader_table() -> QTableWidget:
        table=QTableWidget(0,len(LEADER_COLUMNS)); table.setHorizontalHeaderLabels(LEADER_COLUMNS)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers); table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True); table.setWordWrap(True); table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        for column in (7,8,9,10): table.horizontalHeader().setSectionResizeMode(column,QHeaderView.Stretch)
        table.setStyleSheet("QTableWidget{alternate-background-color:#f7f7f7;background:#ffffff;gridline-color:#dddddd;} QHeaderView::section{background:#e2e5e9;font-weight:700;padding:7px;border:1px solid #c7ccd1;} QTableWidget::item{padding:6px;border-bottom:1px solid #e5e5e5;}")
        return table

    def _leader_container(self,key:str)->QWidget:
        widget=QWidget(); layout=QVBoxLayout(widget); notice=self._leader_notices[key]
        notice.setWordWrap(True); notice.setAlignment(Qt.AlignCenter); notice.setStyleSheet("background:#fff8db;border:1px solid #e6d27a;border-radius:10px;padding:28px;font-weight:600;")
        layout.addWidget(notice); layout.addWidget(self._leader_views[key]); return widget

    def _build_ui(self) -> None:
        central = QWidget(); layout = QVBoxLayout(central); layout.setContentsMargins(18, 14, 18, 14); layout.setSpacing(12)
        central.setStyleSheet("QWidget { color: #24292f; } QTabWidget::pane { border: 1px solid #d8dee4; } QTabBar::tab { padding: 8px 16px; }")
        header = QWidget(); header.setMaximumHeight(170); header_layout = QVBoxLayout(header); header_layout.setContentsMargins(0, 0, 0, 0); header_layout.setSpacing(8)
        title_row = QHBoxLayout(); title = QLabel("QZ 브리핑"); title.setStyleSheet("font-size: 24px; font-weight: 700;")
        title_row.addWidget(title); title_row.addStretch()
        if self._read_only:
            badge = QLabel("읽기 전용")
            badge.setStyleSheet("font-weight: 700; color: #7a4d00; background: #fff3cd; border: 1px solid #e6c96b; border-radius: 8px; padding: 6px 12px;")
            title_row.addWidget(badge)
        market_badge = QLabel(self._trading_day_status); market_badge.setStyleSheet("font-weight: 700; background: #e8f1fb; border-radius: 8px; padding: 6px 12px;")
        title_row.addWidget(market_badge); header_layout.addLayout(title_row)
        for labels in (
            (("clock", "현재 시각"), ("calendar", "시장 상태"), ("data_as_of", "마지막 데이터"), ("connection", "키움 상태"), ("next", "다음 예정")),
            (("pre_market", "장전 브리핑"), ("intraday", "10시 브리핑"), ("market_close", "장마감 브리핑"), ("recommendation", "최신 추천")),
        ):
            row = QHBoxLayout(); row.setSpacing(18)
            for key, caption in labels:
                box = QVBoxLayout(); heading = QLabel(caption); heading.setStyleSheet("color: #666; font-size: 11px;")
                value = self._status_labels[key]; value.setWordWrap(True); value.setStyleSheet("font-weight: 600;")
                box.addWidget(heading); box.addWidget(value); row.addLayout(box, 1)
            header_layout.addLayout(row)
        layout.addWidget(header)
        self._tabs.addTab(self._summary, "오늘 요약")
        pre_market_view = QTextBrowser(); self._result_views["pre_market"] = pre_market_view; self._tabs.addTab(pre_market_view, "장전 브리핑")
        if self._read_only:
            self._tabs.addTab(self._leader_tabs["kospi"], "코스피 주도주")
            self._tabs.addTab(self._leader_tabs["kosdaq"], "코스닥 주도주")
            self._tabs.addTab(self._readonly_holdings, "보유종목")
        intraday_view = QTextBrowser(); self._result_views["intraday_10am"] = intraday_view; self._tabs.addTab(intraday_view, "오전 10시 브리핑")
        close_view = QTextBrowser(); self._result_views["market_close"] = close_view; self._tabs.addTab(close_view, "장마감 브리핑")
        holdings_tab = QWidget(); holdings_layout = QVBoxLayout(holdings_tab)
        holdings_layout.addWidget(self._holdings_summary); holdings_layout.addWidget(self._holdings)
        holdings_layout.addWidget(self._holding_detail)
        self._holdings.itemSelectionChanged.connect(self._show_holding_detail)
        self._tabs.addTab(self._recommendations, "일일 추천")
        if not self._read_only:
            self._tabs.addTab(holdings_tab, "보유종목"); self._tabs.addTab(self._leadership, "주도주·반등 후보")
            self._tabs.addTab(self._watchlist, "다음 거래일 관찰목록")
        self._tabs.addTab(self._messages, "운영 상태")
        layout.addWidget(self._tabs)
        buttons = QHBoxLayout()
        button_actions = [("새로고침", self.refresh)]
        if not self._read_only: button_actions.extend([("브리핑 폴더 열기", self._open_folder), ("창 숨기기", self.hide)])
        for label, callback in button_actions:
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
        self._summary.setHtml(self._summary_html(summary))
        for key, view in self._result_views.items():
            wrapper = model["results"][key]; payload = wrapper.get("json")
            if not isinstance(payload, dict):
                view.setHtml(self._notice_html("저장된 최근 브리핑 또는 추천 결과가 없습니다.", wrapper.get("error")))
            else:
                view.setHtml(self._briefing_html(key, payload, str(wrapper.get("markdown") or "")))
        if not self._read_only:
            self._populate_holdings(model["holdings"]); self._populate_leadership(model["leadership"]); self._populate_watchlist(model["watchlist"])
        else:
            self._render_leaders("kospi", model.get("kospi_leaders", []))
            self._render_leaders("kosdaq", model.get("kosdaq_leaders", []))
            self._render_readonly_holdings(model.get("holdings_feedback", {}))
        self._file_messages = list(model["messages"])
        self._messages.setHtml(self._operations_html(runtime))
        self._render_recommendations(model.get("recommendations", {}))
        results = model["results"]; recommendation = model.get("recommendations", {}).get("report") or {}
        for key, result_key in (("pre_market", "pre_market"), ("intraday", "intraday_10am"), ("market_close", "market_close")):
            payload = results[result_key].get("json") or {}; self._status_labels[key].setText(self._display_time(payload.get("completed_at") or payload.get("metadata", {}).get("generated_at")))
        self._status_labels["recommendation"].setText(self._display_time(recommendation.get("generated_at")))
        latest_times = [recommendation.get("data_as_of")] + [
            (results[name].get("json") or {}).get("completed_at") for name in ("pre_market", "intraday_10am", "market_close")
        ]
        self._status_labels["data_as_of"].setText(self._display_time(max((str(x) for x in latest_times if x), default="")))
        self._update_status()

    def _render_leaders(self, market_key: str, rows: list[dict[str, object]]) -> None:
        korean = "코스피" if market_key == "kospi" else "코스닥"
        table = self._leader_views[market_key]; notice=self._leader_notices[market_key]
        if not rows:
            table.setRowCount(0); table.hide(); notice.setText(f"저장된 {korean} 주도주 결과가 없습니다.\n다음 정상 장전 브리핑 생성 후 표시됩니다."); notice.show()
            return
        notice.hide(); table.show(); table.setRowCount(len(rows[:10]))
        for rank, item in enumerate(rows[:10], 1):
            reasons = "; ".join(str(x) for x in item.get("reasons", [])) or "자료 부족"
            risks = "; ".join(str(x) for x in item.get("warnings", [])) or "특별 경고 없음"
            flow = item.get("investor_flow") if isinstance(item.get("investor_flow"), dict) else {}
            foreign = flow.get("foreign") if flow else item.get("foreign_net_buy")
            institution = flow.get("institution") if flow else item.get("institution_net_buy")
            technical = status_label(item.get("trend") or item.get("technical_position")) if item.get("trend") or item.get("technical_position") else "자료 부족"
            theme = item.get("sector") or item.get("theme") or "자료 부족"
            values=(rank,item.get("name") or "자료 부족",item.get("code") or "자료 부족",item.get("change_rate"),item.get("trading_value"),foreign,institution,reasons,risks,theme,technical)
            for column,value in enumerate(values):
                text="자료 부족" if value is None or value=="" else str(value); cell=QTableWidgetItem(text)
                if column in (0,3,4,5,6): cell.setTextAlignment(Qt.AlignRight|Qt.AlignVCenter)
                if column==1:
                    font=cell.font(); font.setBold(True); cell.setFont(font)
                if column in (3,5,6) and isinstance(value,(int,float)):
                    color="#c62828" if value>0 else "#1565c0" if value<0 else "#555555"; cell.setForeground(QColor(color))
                    cell.setBackground(QColor("#fff0f0" if value>0 else "#eef5ff" if value<0 else "#f2f2f2"))
                if column==8 and risks!="특별 경고 없음": cell.setBackground(QColor("#fff0df")); cell.setForeground(QColor("#a34b00"))
                if text=="자료 부족": cell.setBackground(QColor("#fff8db"))
                table.setItem(rank-1,column,cell)
        table.resizeRowsToContents()

    def _render_readonly_holdings(self, data: dict[str, object]) -> None:
        rows = data.get("rows", []) if isinstance(data, dict) else []
        self._clear_card_layout(self._holding_layout,self._holding_cards)
        if not rows:
            self._holding_layout.addWidget(self._notice_card("저장된 보유종목 분석 결과가 없습니다.\n다음 정상 운영 시 보유종목 분석이 완료되면 표시됩니다.\n\n읽기 전용 화면에서는 계좌에 새로 접속하지 않습니다."))
            return
        for item in rows:
            decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
            averages = item.get("moving_averages") if isinstance(item.get("moving_averages"), dict) else {}
            flow = item.get("investor_flow") if isinstance(item.get("investor_flow"), dict) else {}
            risks = "; ".join(str(x) for x in decision.get("risk_conditions", [])) or "; ".join(str(x) for x in item.get("warnings", [])) or "특별 경고 없음"
            conditions = "; ".join(str(x) for x in decision.get("positive_conditions", [])) or "자료 부족"
            trend = status_label(item.get("trend")) if item.get("trend") else "자료 부족"
            review = status_label(item.get("review_status") or decision.get("action_level")) if item.get("review_status") or decision.get("action_level") else "자료 부족"
            profit=item.get("profit_loss"); rate=item.get("profit_rate"); positive=isinstance(rate,(int,float)) and rate>0; negative=isinstance(rate,(int,float)) and rate<0
            state="수익권" if positive else "손실권" if negative else "추가 확인 필요"; accent="#c62828" if positive else "#1565c0" if negative else "#666666"; background="#fff4f4" if positive else "#f1f6ff" if negative else "#f7f7f7"
            frame=QFrame(); frame.setObjectName("holdingCard"); frame.setStyleSheet(f"QFrame#holdingCard{{background:{background};border:1px solid #d7d7d7;border-radius:10px;}} QLabel{{border:none;background:transparent;}}")
            grid=QGridLayout(frame); grid.setContentsMargins(16,14,16,14); grid.setHorizontalSpacing(28); grid.setVerticalSpacing(7)
            title=QLabel(f'{item.get("name") or "종목명 자료 부족"}  /  {item.get("code") or "코드 자료 부족"}'); title.setStyleSheet("font-size:17px;font-weight:700;")
            pnl=QLabel(f"수익률 {rate if rate is not None else '자료 부족'}  ·  평가손익 {profit if profit is not None else '자료 부족'}"); pnl.setAlignment(Qt.AlignRight); pnl.setStyleSheet(f"font-weight:700;color:{accent};")
            badge=QLabel(state); badge.setStyleSheet(f"font-weight:700;color:{accent};padding:4px 8px;border:1px solid {accent};border-radius:7px;")
            saved=QLabel(f"마지막 저장 자료 · {self._display_time(data.get('as_of'))}"); saved.setStyleSheet("color:#666;")
            left=self._card_label(f"보유수량  {self._value(item.get('quantity'))}\n평균매입가  {self._value(item.get('average_price'))}\n마지막 저장 현재가  {self._value(item.get('current_price'))}\n평가손익  {self._value(profit)}\n수익률  {self._value(rate)}")
            right=self._card_label(f"기술적 위치  {trend}\n주봉/일봉 추세  {trend}\n외국인  {self._value(flow.get('foreign'))} · 기관  {self._value(flow.get('institution'))}\n주요 위험  {risks}\n보유 의견  {decision.get('summary') or '자료 부족'}")
            footer=self._card_label(f"물타기·불타기 판단  {review}\n축소·손절·관망 의견  {review}\n무효화 조건  {risks}\n확인 조건  {conditions}")
            grid.addWidget(title,0,0); grid.addWidget(pnl,0,1); grid.addWidget(badge,1,0); grid.addWidget(saved,1,1); grid.addWidget(left,2,0); grid.addWidget(right,2,1); grid.addWidget(footer,3,0,1,2)
            self._holding_layout.addWidget(frame); self._holding_cards.append(frame)

    @staticmethod
    def _value(value: object) -> str: return "자료 부족" if value is None or value=="" else str(value)

    @staticmethod
    def _card_label(text: str) -> QLabel:
        label=QLabel(text); label.setWordWrap(True); label.setTextInteractionFlags(Qt.TextSelectableByMouse); return label

    @staticmethod
    def _notice_card(text: str) -> QFrame:
        frame=QFrame(); frame.setObjectName("noticeCard"); frame.setStyleSheet("QFrame#noticeCard{background:#fff8db;border:1px solid #e6d27a;border-radius:10px;}")
        layout=QVBoxLayout(frame); label=QLabel(text); label.setAlignment(Qt.AlignCenter); label.setWordWrap(True); label.setStyleSheet("padding:28px;font-weight:600;"); layout.addWidget(label); return frame

    @staticmethod
    def _clear_card_layout(layout: QVBoxLayout, cards: list[QFrame]) -> None:
        while layout.count():
            item=layout.takeAt(0); widget=item.widget()
            if widget is not None: widget.deleteLater()
        cards.clear()

    @staticmethod
    def _card(title: str, body: str) -> str:
        return f'<div style="display:inline-block; vertical-align:top; width:45%; margin:8px; padding:14px; border:1px solid #d8dee4; border-radius:10px; background:#ffffff;"><h3 style="margin-top:0;">{escape(title)}</h3>{body}</div>'

    def _summary_html(self, summary: dict[str, object]) -> str:
        conclusion = summary.get("conclusion") or "시장 판단 자료가 없습니다."
        confidence = summary.get("decision_confidence") or "자료 부족"
        flow = f"외국인 {summary.get('외국인') or '-'}<br>기관 {summary.get('기관') or '-'}<br>프로그램 {summary.get('프로그램') or '-'}"
        risks = summary.get("invalidation_conditions") or []
        risk_text = "<br>".join(f"• {escape(str(x))}" for x in risks[:3]) or escape(str(summary.get("risk") or "특별 경고 없음"))
        response = summary.get("action_guidance") or summary.get("guidance") or "저장된 대응 자료가 없습니다."
        cards = [
            self._card("시장 판단", f"<b>{escape(str(conclusion))}</b><br><br>판단 신뢰도: {escape(str(confidence))}"),
            self._card("시장 수급", flow), self._card("주요 위험", risk_text),
            self._card("오늘의 대응", escape(str(response))),
        ]
        return self._page("오늘 요약", "".join(cards))

    def _briefing_html(self, key: str, payload: dict[str, object], markdown: str) -> str:
        titles = {"pre_market":"장전 브리핑", "intraday_10am":"오전 10시 브리핑", "market_close":"장마감 브리핑"}
        analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
        close = payload.get("market_close_analysis") if isinstance(payload.get("market_close_analysis"), dict) else {}
        generated = payload.get("completed_at") or payload.get("metadata", {}).get("generated_at") or "자료 없음"
        summary = analysis.get("summary") or close.get("market_conclusion") or "저장된 시장 요약이 없습니다."
        risk = close.get("risk_summary") or "; ".join(str(x) for x in payload.get("warnings", [])[:3]) or "특별 경고 없음"
        response = close.get("next_session_summary") or analysis.get("action_guidance") or "추가 대응 자료가 없습니다."
        watch = payload.get("next_session_watchlist") if isinstance(payload.get("next_session_watchlist"), list) else []
        stocks = "<br>".join(f"• {escape(str(item.get('name') or item.get('code') or '-'))}" for item in watch[:10] if isinstance(item, dict)) or "표시할 종목 목록이 없습니다."
        if key == "pre_market":
            projected = self._view_model.summary(payload)
            decision = analysis.get("decision") if isinstance(analysis.get("decision"), dict) else {}
            previous = payload.get("previous_market_close") if isinstance(payload.get("previous_market_close"), dict) else {}
            previous_analysis = previous.get("market_close_analysis") if isinstance(previous.get("market_close_analysis"), dict) else {}
            prior = previous_analysis.get("market_conclusion") or "전일 장마감 자료 부족"
            flows = f'외국인 {escape(str(projected.get("외국인") or "자료 부족"))}<br>기관 {escape(str(projected.get("기관") or "자료 부족"))}<br>프로그램 {escape(str(projected.get("프로그램") or "자료 부족"))}'
            confidence = decision.get("confidence") or analysis.get("confidence") or "자료 부족"
            body = f"<p><b>생성 시각</b> {escape(str(generated))}</p>" + self._card("전일 국내시장", escape(str(prior))) + self._card("장전 시장 방향", escape(str(summary))) + self._card("핵심 수급", flows) + self._card("주요 위험", escape(str(risk))) + self._card("오늘의 대응", escape(str(response))) + self._card("판단 신뢰도·기준 시각", f'{escape(str(confidence))}<br>{escape(str(generated))}') + '<p>코스피·코스닥 주도주와 보유종목 상세는 각각의 독립 탭에서 확인할 수 있습니다.</p>'
        else:
            body = f"<p><b>생성 시각</b> {escape(str(generated))}</p>" + self._card("시장 요약", escape(str(summary))) + self._card("주요 위험", escape(str(risk))) + self._card("대응", escape(str(response))) + self._card("종목 목록", stocks)
        if markdown and key != "pre_market":
            body += f'<details><summary>상세 내용</summary><pre style="white-space:pre-wrap;">{escape(self._safe_text(markdown))}</pre></details>'
        return self._page(titles[key], body)

    def _operations_html(self, runtime: dict[str, object]) -> str:
        if self._read_only:
            values = (("데이터 수집", "저장 결과 조회 중"), ("키움 연결", "연결하지 않음"), ("Telegram", "전송하지 않음"))
        else:
            values = (("데이터 수집", self._human_status(runtime.get("health"))), ("키움 연결", self._human_status(runtime.get("connection_state"))), ("Telegram", "사용" if runtime.get("telegram_enabled") else "사용 안 함"))
        last_task = self._human_status(runtime.get("last_completed_briefing")) if runtime.get("last_completed_briefing") else self._display_time(runtime.get("last_heartbeat_at"))
        values += (("마지막 정상 작업", last_task), ("다음 예정 작업", self._human_status(runtime.get("next_scheduled_task"))), ("대기 중 알림", str(runtime.get("telegram_pending_count") or 0)), ("최근 오류", "확인 필요" if runtime.get("telegram_last_error") or self._file_messages else "없음"))
        return self._page("운영 상태", "".join(self._card(title, escape(str(value))) for title, value in values))

    @staticmethod
    def _page(title: str, body: str) -> str:
        return f'<html><body style="font-family:Malgun Gothic; font-size:14px; color:#24292f; margin:18px;"><h2>{escape(title)}</h2>{body}</body></html>'

    def _notice_html(self, message: str, detail: object = None) -> str:
        suffix = f"<p>{escape(str(detail))}</p>" if detail else ""
        return self._page("안내", f'<div style="padding:30px; text-align:center; border:1px solid #d8dee4; border-radius:10px;">{escape(message)}{suffix}</div>')

    def _render_recommendations(self, wrapper: dict[str, object]) -> None:
        self._clear_card_layout(self._recommendation_layout,self._recommendation_cards)
        report = wrapper.get("report") if isinstance(wrapper, dict) else None
        if not isinstance(report, dict):
            self._recommendation_layout.addWidget(self._notice_card("저장된 운영 추천 결과가 없습니다.\n다음 장마감 추천 생성 후 이 화면에 표시됩니다."))
            return
        groups=(("최우선 후보","strong",True),("추가 검토 후보","review",False)); total=sum(len(report.get(key,[])[:3]) for _,key,_ in groups if isinstance(report.get(key),list))
        if total==0:
            self._recommendation_layout.addWidget(self._notice_card("현재 기준을 충족한 추천 후보가 없습니다."))
            return
        summary=QLabel(f"최우선 후보 {report.get('strong_count',0)}개 · 추가 검토 {report.get('review_count',0)}개  |  기준 시각 {self._display_time(report.get('data_as_of'))}"); summary.setStyleSheet("font-weight:700;padding:8px;"); self._recommendation_layout.addWidget(summary)
        for section,key,strong in groups:
            rows=report.get(key) if isinstance(report.get(key),list) else []
            if not rows: continue
            heading=QLabel(section); heading.setStyleSheet("font-size:18px;font-weight:700;padding:10px 2px 4px;"); self._recommendation_layout.addWidget(heading)
            for item in rows[:3]:
                if not isinstance(item,dict): continue
                missing="; ".join(str(x) for x in item.get("missing",[])) or "없음"; risks="; ".join(str(x) for x in item.get("risks",[])) or "없음"; reasons="\n".join(f"• {x}" for x in item.get("reasons",[])[:3]) or "자료 부족"; invalid="; ".join(str(x) for x in item.get("invalidation_conditions",[])) or "자료 부족"
                frame=QFrame(); frame.setObjectName("recommendationCard"); border="#b71c1c" if strong else "#9aa0a6"; width="3px" if strong else "1px"
                frame.setStyleSheet(f"QFrame#recommendationCard{{background:#ffffff;border:{width} solid {border};border-radius:10px;}} QLabel{{border:none;background:transparent;}}")
                grid=QGridLayout(frame); grid.setContentsMargins(16,14,16,14); grid.setHorizontalSpacing(24); grid.setVerticalSpacing(7)
                title=QLabel(f'{item.get("name") or "종목명 자료 부족"}  /  {item.get("code") or "코드 자료 부족"}'); title.setStyleSheet("font-size:17px;font-weight:700;")
                grade=QLabel(f'{item.get("market") or "자료 부족"} · {item.get("grade") or section}'); grade.setStyleSheet("font-weight:700;color:#555;")
                score=QLabel(f'{self._value(item.get("total_score"))}점'); score.setAlignment(Qt.AlignRight); score.setStyleSheet("font-size:24px;font-weight:800;color:#b71c1c;" if strong else "font-size:24px;font-weight:800;color:#555;")
                core=self._card_label(f"데이터 신뢰도  {self._value(item.get('confidence'))}\n주봉 종가  {self._value(item.get('weekly_close'))}\nMA5  {self._value(item.get('weekly_ma5'))}\n이격률  {self._value(item.get('weekly_distance_rate'))}")
                detail=self._card_label(f"핵심 근거\n{reasons}\n\n추격매수 금지  {'예' if item.get('chase_buying_prohibited') else '아니오'}\n무효화 조건  {invalid}")
                missing_label=self._card_label(f"부족 자료  {missing}"); missing_label.setStyleSheet("background:#fff8db;color:#725c00;padding:5px;border-radius:5px;" if missing!="없음" else "color:#555;")
                risk_label=self._card_label(f"주요 위험  {risks}"); risk_label.setStyleSheet("background:#fff0df;color:#a34b00;padding:5px;border-radius:5px;" if risks!="없음" else "color:#555;")
                grid.addWidget(title,0,0); grid.addWidget(score,0,1); grid.addWidget(grade,1,0,1,2); grid.addWidget(core,2,0); grid.addWidget(detail,2,1); grid.addWidget(missing_label,3,0); grid.addWidget(risk_label,3,1)
                self._recommendation_layout.addWidget(frame); self._recommendation_cards.append(frame)

    def _safe_text(self, text: str) -> str:
        project_root = str(self._root.parent.parent)
        return text.replace(project_root, "[내부 경로 숨김]").replace("None", "-").replace("null", "자료 부족").replace("unknown", "자료 부족")

    @staticmethod
    def _human_status(value: object) -> str:
        mapping = {"waiting_for_login":"운영 프로그램 로그인 대기", "FAILED":"현재 연결 안 됨", "CONNECTED":"연결됨", "healthy":"정상", "waiting":"대기 중", "pre_market":"장전 브리핑", "intraday_10am":"오전 10시 브리핑", "market_close":"장마감 브리핑", "preopen_monitoring":"장전 시장 감시"}
        raw = str(value or "")
        return mapping.get(raw, raw if raw else "자료 없음")

    @staticmethod
    def _display_time(value: object) -> str:
        if not value: return "기록 없음"
        raw = str(value)
        try: return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
        except ValueError: return raw if len(raw) < 40 else "기록 있음"

    @staticmethod
    def _weekday_after(target: date) -> date:
        candidate = target + timedelta(days=1)
        while candidate.weekday() >= 5: candidate += timedelta(days=1)
        return candidate

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
        connection_text = "읽기 전용 — 키움 연결하지 않음" if self._read_only else self._human_status(getattr(connection, "name", connection))
        self._status_labels["connection"].setText(connection_text)
        self._status_labels["calendar"].setText(self._trading_day_status); self._status_labels["clock"].setText(now.strftime("%H:%M:%S"))
        closed = self._trading_day_status in {"주말", "확정 휴장일", "장 종료"} or now.time() >= datetime.strptime("15:40", "%H:%M").time()
        if not closed:
            schedule = [(9, 0, "장전 브리핑"), (10, 0, "10시 브리핑"), (15, 40, "장마감 브리핑")]
            upcoming = next((f"{now.date().isoformat()} {hour:02d}:{minute:02d} {name}" for hour, minute, name in schedule if (now.hour, now.minute) < (hour, minute)), None)
        else: upcoming = None
        if upcoming is None:
            next_day = self._next_trading_day(now.date())
            upcoming = f"{next_day.isoformat()} 09:00 장전 브리핑" if next_day else "다음 거래일 확인 필요"
        self._status_labels["next"].setText(upcoming)
        self._status_labels["shutdown"].setText("해당 없음" if self._read_only else "20:00")

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
            if not self._read_only:
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
