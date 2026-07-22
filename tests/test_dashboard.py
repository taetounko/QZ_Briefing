# -*- coding: utf-8 -*-

import json
import os
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QApplication

from qz_briefing.briefing import BriefingStorage, BriefingType, DailyBriefingPipeline
from qz_briefing.ui.dashboard_view_model import DashboardViewModel
from qz_briefing.ui.formatters import money, percent, status_label
from qz_briefing.ui.main_window import DashboardMainWindow


DAY = date(2026, 7, 22)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def payload(kind="market_close"):
    return {
        "schema_version": 1, "briefing_type": kind, "trading_date": DAY.isoformat(),
        "completed_at": "2026-07-22T15:40:00", "status": "completed",
        "analysis": {"summary": "상승 우위", "market_state": "bullish"},
        "market_close_analysis": {"market_conclusion": "상승장", "risk_summary": "수급 반전 주의", "next_session_summary": "외국인 수급 확인"},
        "collectors": {
            "kiwoom_market_indices": {"data": {"indices": [{"market": "KOSPI", "change_rate": 1.2}]}},
            "kiwoom_core_market": {"data": {"securities": [{"code": "005930", "change_rate": 2.3}]}},
        },
        "holdings_analysis": {
            "source": "kiwoom_accounts", "accounts": [{"account_id": "******6910"}],
            "portfolio": {"investment_amount": 1000000, "valuation_amount": 1100000, "profit_loss": 100000, "profit_rate": 10},
            "holdings": [{"code": "005930", "name": "삼성전자", "account_ids": ["1234567890"], "quantity": 10, "average_price": 100000, "current_price": 110000, "investment_amount": 1000000, "valuation_amount": 1100000, "profit_loss": 100000, "profit_rate": 10, "trend": "uptrend", "bottom_confirmation": "confirmed", "review_status": "no_action", "warnings": []}],
        },
        "leadership": {"kospi": [{"code": "005930", "name": "삼성전자", "current_price": 110000, "change_rate": 2.3, "trading_value": 10000000, "rsi14": 55, "reasons": ["거래대금"], "warnings": []}], "kosdaq": [], "rebound_candidates": []},
        "next_session_watchlist": [{"category": "holding_risk", "code": "005930", "name": "삼성전자", "current_state": "uptrend", "confirmation_condition": "20일선 유지", "risk_condition": "저점 이탈"}],
        "warnings": ["확인 경고"], "errors": [],
    }


def write_result(root: Path, name="market_close", data=None):
    directory = root / "2026" / "07" / "22"; directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(data or payload(), ensure_ascii=False), encoding="utf-8")
    (directory / f"{name}.md").write_text("# 저장된 마크다운", encoding="utf-8")


def test_view_model_parses_summary_holdings_leadership_watchlist_and_masks_account(tmp_path: Path) -> None:
    write_result(tmp_path)
    model = DashboardViewModel(tmp_path, clock=lambda: datetime(2026, 7, 22, 16)).load_today()
    assert model["summary"]["conclusion"] == "상승장"
    assert model["summary"]["KOSPI"] == 1.2
    assert model["holdings"]["account_count"] == 1 and model["holdings"]["holding_count"] == 1
    assert model["holdings"]["rows"][0]["account"] == "******7890"
    assert "1234567890" not in json.dumps(model["holdings"], ensure_ascii=False)
    assert model["leadership"][0]["market"] == "KOSPI"
    assert model["watchlist"][0]["category"] == "holding_risk"
    assert any("확인 경고" in item for item in model["messages"])


def test_validation_is_parsed_separately_and_never_replaces_regular_close_slot(tmp_path: Path) -> None:
    regular = payload(); validation = payload(); validation["metadata"] = {"execution_mode": "manual_validation", "generated_at": "2026-07-22T16:00:00"}
    write_result(tmp_path, "market_close", regular); write_result(tmp_path, "market_close_validation", validation)
    results = DashboardViewModel(tmp_path, clock=lambda: datetime(2026, 7, 22, 16)).load_today()["results"]
    assert results["market_close"]["json"] is not results["market_close_validation"]["json"]
    assert results["market_close"]["json"].get("metadata") is None
    assert results["market_close_validation"]["json"]["metadata"]["execution_mode"] == "manual_validation"


def test_missing_corrupt_and_legacy_json_are_safe(tmp_path: Path) -> None:
    directory = tmp_path / "2026" / "07" / "22"; directory.mkdir(parents=True)
    (directory / "pre_market.json").write_text("{broken", encoding="utf-8")
    (directory / "intraday_10am.json").write_text('{"briefing_type":"intraday_10am"}', encoding="utf-8")
    model = DashboardViewModel(tmp_path, clock=lambda: datetime(2026, 7, 22, 11)).load_today()
    assert model["results"]["pre_market"]["error"]
    assert model["results"]["intraday_10am"]["json"]["briefing_type"] == "intraday_10am"
    assert model["holdings"]["rows"] == []


def test_ui_formatters_translate_without_changing_source_identifier() -> None:
    source = "strong_downtrend"
    assert status_label(source) == "강한 하락추세" and source == "strong_downtrend"
    assert money(1234567) == "1,234,567"
    assert percent(-12.345) == "-12.35%"
    assert money(None) == "-" and percent(None) == "-"


def test_main_window_has_eight_tabs_refreshes_files_and_close_hides(app, tmp_path: Path) -> None:
    write_result(tmp_path)
    shutdown = []
    window = DashboardMainWindow(tmp_path, connection_state=lambda: "CONNECTED", trading_day_status="open", shutdown=lambda: shutdown.append(True), open_folder=lambda: None, clock=lambda: datetime(2026, 7, 22, 16))
    assert window.tab_count == 8
    assert all(window._status_labels[key] is not None for key in ("connection", "calendar", "clock", "next", "last", "shutdown"))
    assert window._holdings.rowCount() == 1 and window._leadership.rowCount() == 1 and window._watchlist.rowCount() == 1
    assert "총 투자금액 1,000,000" in window._holdings_summary.text()
    assert "1234567890" not in window._holdings.item(0, 2).text()
    event = QCloseEvent(); window.show(); window.closeEvent(event)
    assert not event.isAccepted() and window.isHidden()
    window.tray.icon.contextMenu().actions()[-1].trigger()
    assert shutdown == [True]
    window.stop()


def test_pipeline_completion_listener_refreshes_without_tr_calls(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path); observed = []
    pipeline = DailyBriefingPipeline(storage, [], clock=lambda: datetime(2026, 7, 22, 8))
    pipeline.add_completion_listener(lambda name, path: observed.append((name, path)))
    pipeline.run(BriefingType.PRE_MARKET, DAY, market_calendar_status="open", market_calendar_reason="weekday")
    assert observed and observed[0][0] == "pre_market"
    assert Path(observed[0][1]).name == "pre_market.json"
