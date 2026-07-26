import json
import os
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication, QFrame, QLabel, QScrollArea, QTableWidget

from qz_briefing.__main__ import parse_cli_arguments, run
from qz_briefing.scheduling import MarketStatus, TradingDayResult
from qz_briefing.ui.main_window import DashboardMainWindow, REQUIRED_READONLY_TABS
from qz_briefing.ui.readonly_loader import NO_SAVED_RESULT, ReadOnlyDashboardLoader


class Signal:
    def connect(self, callback): self.callback = callback


class App:
    def __init__(self): self.aboutToQuit = Signal(); self.quit_calls = 0; self.last_window = None
    def setQuitOnLastWindowClosed(self, value): self.last_window = value
    def exec_(self): return 0
    def quit(self): self.quit_calls += 1


class Window:
    def __init__(self, captured, **kwargs): captured.update(kwargs); self.shown = False
    def show(self): self.shown = True


def widget_text(widget):
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


def table_text(table):
    return "\n".join(table.item(row,column).text() for row in range(table.rowCount()) for column in range(table.columnCount()) if table.item(row,column))


def market(status, reason="weekday"):
    return TradingDayResult(date(2026, 7, 26), status, reason)


@pytest.mark.parametrize(("now", "status", "reason", "label"), [
    (datetime(2026, 7, 26, 12), MarketStatus.CLOSED, "weekend", "주말"),
    (datetime(2026, 7, 27, 20, 30), MarketStatus.OPEN, "weekday", "장 종료"),
    (datetime(2026, 7, 27, 8, 30), MarketStatus.OPEN, "weekday", "개장 전"),
    (datetime(2026, 7, 27, 10), MarketStatus.OPEN, "weekday", "장중"),
    (datetime(2026, 8, 15, 12), MarketStatus.CLOSED, "market_holiday", "확정 휴장일"),
])
def test_dashboard_starts_without_runtime_for_every_market_state(now, status, reason, label):
    app = App(); captured = {}; forbidden = lambda *a, **k: (_ for _ in ()).throw(AssertionError("external runtime call"))
    result = run(
        ["--dashboard"], application_factory=lambda _: app,
        dashboard_factory=lambda **kwargs: Window(captured, **kwargs),
        adapter_factory=forbidden, notification_service_factory=forbidden,
        tr_queue_factory=forbidden, market_day_checker=lambda _: market(status, reason),
        lock_factory=forbidden, clock=lambda: now,
    )
    assert result == 0 and captured["read_only"] and captured["standalone"]
    assert captured["trading_day_status"] == label
    assert "미연결" in captured["connection_state"]()


def write_recommendation(root: Path, digest="abc", *, broken_report=False):
    directory = root / "reports" / "2026-07-24"
    version = directory / "versions" / digest; version.mkdir(parents=True)
    report = {"generated_at":"2026-07-24T15:40:00","data_as_of":"2026-07-24T15:30:00","strong_count":1,"review_count":0,"strong":[{"code":"000001","name":"가상종목","market":"KOSPI","grade":"완전 강추","total_score":80,"confidence":.9,"weekly_close":100,"weekly_ma5":90,"weekly_distance_rate":11.1,"reasons":["근거"],"missing":[],"risks":[],"invalidation_conditions":[]}],"review":[]}
    (version / "daily_recommendations.json").write_text("{broken" if broken_report else json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (version / "daily_recommendations.md").write_text("# 추천", encoding="utf-8")
    (version / "metadata.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (directory / "latest.json").write_text(json.dumps({"version":f"versions/{digest}"}), encoding="utf-8")
    return report


def test_option_parsing_and_readonly_loader_has_no_result(tmp_path):
    assert parse_cli_arguments(["--dashboard"]).dashboard
    loaded = ReadOnlyDashboardLoader(tmp_path/"briefings", tmp_path/"recommendations").latest_recommendation()
    assert loaded["report"] is None and loaded["warning"] == NO_SAVED_RESULT


def test_loader_reads_operational_recommendation_and_never_validation(tmp_path):
    expected = write_recommendation(tmp_path/"data"/"recommendations")
    write_recommendation(tmp_path/"data"/"validation"/"recommendations", "validation")
    loader = ReadOnlyDashboardLoader(tmp_path/"data"/"briefings", tmp_path/"data"/"recommendations")
    assert loader.latest_recommendation()["report"] == expected


def test_corrupt_latest_falls_back_to_valid_version_and_ignores_temp(tmp_path):
    root = tmp_path/"recommendations"; expected = write_recommendation(root)
    directory = root/"reports"/"2026-07-24"
    (directory/"latest.json").write_text("{broken", encoding="utf-8")
    (directory/".latest.json.tmp").write_text("{}", encoding="utf-8")
    assert ReadOnlyDashboardLoader(tmp_path/"briefings", root).latest_recommendation()["report"] == expected


def test_corrupt_version_is_skipped_for_older_valid_report(tmp_path):
    root = tmp_path/"recommendations"; write_recommendation(root, "broken", broken_report=True)
    older = root/"reports"/"2026-07-23"/"versions"/"good"; older.mkdir(parents=True)
    report={"strong":[],"review":[],"generated_at":"2026-07-23T15:40:00"}
    for name in ("daily_recommendations.json","metadata.json"):
        (older/name).write_text(json.dumps(report), encoding="utf-8")
    (older/"daily_recommendations.md").write_text("ok",encoding="utf-8")
    assert ReadOnlyDashboardLoader(tmp_path/"briefings", root).latest_recommendation()["report"] == report


def test_latest_prior_briefing_is_loaded_and_file_change_refreshes(tmp_path):
    directory=tmp_path/"briefings"/"2026"/"07"/"24"; directory.mkdir(parents=True)
    path=directory/"market_close.json"; path.write_text(json.dumps({"briefing_type":"market_close","completed_at":"2026-07-24T15:40:00"}),encoding="utf-8")
    loader=ReadOnlyDashboardLoader(tmp_path/"briefings",tmp_path/"recommendations")
    assert loader.latest_briefing("market_close",date(2026,7,26))["json"]["completed_at"].endswith("15:40:00")
    path.write_text(json.dumps({"briefing_type":"market_close","completed_at":"2026-07-24T15:41:00"}),encoding="utf-8")
    assert loader.latest_briefing("market_close",date(2026,7,26))["json"]["completed_at"].endswith("15:41:00")


def test_readonly_window_never_displays_validation_markdown(tmp_path):
    directory=tmp_path/"briefings"/"2026"/"07"/"26"; directory.mkdir(parents=True)
    regular={"briefing_type":"market_close","completed_at":"2026-07-26T15:40:00"}
    validation={"briefing_type":"market_close","completed_at":"2026-07-26T16:00:00"}
    (directory/"market_close.json").write_text(json.dumps(regular),encoding="utf-8")
    (directory/"market_close.md").write_text("운영 장마감",encoding="utf-8")
    (directory/"market_close_validation.json").write_text(json.dumps(validation),encoding="utf-8")
    (directory/"market_close_validation.md").write_text("VALIDATION_ONLY_SECRET",encoding="utf-8")
    app=QApplication.instance() or QApplication([])
    window=DashboardMainWindow(tmp_path/"briefings",recommendation_root=tmp_path/"recommendations",connection_state=lambda:"DISCONNECTED",trading_day_status="주말",shutdown=lambda:None,open_folder=lambda:None,read_only=True,standalone=True,clock=lambda:datetime(2026,7,26,16))
    assert "운영 장마감" in window._result_views["market_close"].toPlainText()
    assert "VALIDATION_ONLY_SECRET" not in window._result_views["market_close"].toPlainText()
    window.stop()


def test_window_refresh_interval_cards_limit_and_safe_text(tmp_path):
    app=QApplication.instance() or QApplication([]); report=write_recommendation(tmp_path/"recommendations")
    report["strong"] = report["strong"] * 5; report["strong_count"] = 5
    version=tmp_path/"recommendations"/"reports"/"2026-07-24"/"versions"/"abc"
    for name in ("daily_recommendations.json","metadata.json"):
        (version/name).write_text(json.dumps(report,ensure_ascii=False),encoding="utf-8")
    window=DashboardMainWindow(tmp_path/"briefings",recommendation_root=tmp_path/"recommendations",connection_state=lambda:"DISCONNECTED",trading_day_status="주말",shutdown=lambda:None,open_folder=lambda:None,read_only=True,standalone=True,clock=lambda:datetime(2026,7,26,12))
    text=widget_text(window._recommendations)
    assert window._refresh_timer.interval()==30000 and text.count("가상종목") == 3 and len(window._recommendation_cards)==3
    assert all(isinstance(card,QFrame) for card in window._recommendation_cards) and "border:3px solid #b71c1c" in window._recommendation_cards[0].styleSheet()
    assert all(token not in text for token in ("None","null","unknown",str(tmp_path)))
    window.stop()


def test_readonly_user_cards_hide_internal_fields_and_failed_runtime(tmp_path):
    directory=tmp_path/"briefings"/"2026"/"07"/"24"; directory.mkdir(parents=True)
    payload={"briefing_type":"market_close","completed_at":"2026-07-24T15:40:00","analysis":{"summary":"상승 우세","decision":{"confidence":"높음","action_guidance":"관찰 우선"}},"market_close_analysis":{"risk_summary":"과열 주의","next_session_summary":"추격매수 주의"},"warnings":[],"errors":[]}
    (directory/"market_close.json").write_text(json.dumps(payload,ensure_ascii=False),encoding="utf-8")
    (directory/"market_close.md").write_text("상세 시장 내용\n"*100,encoding="utf-8")
    runtime=tmp_path/"runtime"; runtime.mkdir(); (runtime/"heartbeat.json").write_text(json.dumps({"connection_state":"FAILED","runtime_state":"waiting_for_login","telegram_enabled":True}),encoding="utf-8")
    app=QApplication.instance() or QApplication([])
    window=DashboardMainWindow(tmp_path/"briefings",recommendation_root=tmp_path/"recommendations",connection_state=lambda:"FAILED",trading_day_status="주말",shutdown=lambda:None,open_folder=lambda:None,read_only=True,standalone=True,next_trading_day=lambda _:date(2026,7,27),clock=lambda:datetime(2026,7,26,12))
    summary=window._summary.toPlainText(); operations=window._messages.toPlainText()
    assert "시장 판단" in summary and "시장 수급" in summary and "주요 위험" in summary and "오늘의 대응" in summary
    assert all(key not in summary+operations for key in ("conclusion","decision_confidence","market_risk","connection_state","waiting_for_login","FAILED"))
    assert "키움 연결\n연결하지 않음" in operations and "Telegram\n전송하지 않음" in operations
    assert window._status_labels["connection"].text()=="읽기 전용 — 키움 연결하지 않음"
    assert window._status_labels["next"].text()=="2026-07-27 09:00 장전 브리핑"
    assert not hasattr(window.tray,"icon")
    assert window.minimumWidth()>=960 and window._result_views["market_close"].verticalScrollBar() is not None
    window.stop()


def test_after_close_schedule_and_missing_recommendation_notice(tmp_path, capfd):
    app=QApplication.instance() or QApplication([])
    window=DashboardMainWindow(tmp_path/"briefings",recommendation_root=tmp_path/"recommendations",connection_state=lambda:"DISCONNECTED",trading_day_status="장 종료",shutdown=lambda:None,open_folder=lambda:None,read_only=True,standalone=True,next_trading_day=lambda _:date(2026,7,28),clock=lambda:datetime(2026,7,27,20,30))
    assert window._status_labels["next"].text()=="2026-07-28 09:00 장전 브리핑"
    assert "저장된 운영 추천 결과가 없습니다" in widget_text(window._recommendations)
    assert "다음 장마감 추천 생성 후" in widget_text(window._recommendations)
    window.stop()
    assert "QSystemTrayIcon::setVisible: No Icon set" not in capfd.readouterr().err


def premarket_payload():
    return {
        "briefing_type":"pre_market", "completed_at":"2026-07-24T09:00:00",
        "analysis":{"summary":"장전 중립", "confidence":"보통", "decision":{"headline":"관찰 우선", "confidence":"보통", "action_guidance":"개장 후 수급 확인"}},
        "leadership":{
            "kospi":[
                {"code":"005930","name":"코스피가상1","market":"KOSPI","current_price":70000,"change_rate":1.2,"trading_value":1000000,"score":9,"reasons":["거래대금 상위"],"warnings":[]},
                {"code":"005930","name":"중복종목","market":"KOSPI"},
                {"code":"999999","name":"잘못혼합","market":"KOSDAQ"},
            ],
            "kosdaq":[{"code":"035720","name":"코스닥가상1","market":"KOSDAQ","current_price":50000,"change_rate":2.1,"reasons":["상대강도"],"warnings":["변동성 주의"]}],
        },
        "holdings_analysis":{"source":"kiwoom_accounts","basis":"latest_close","accounts":[{"account_id":"1234567890"}],"portfolio":{},"holdings":[
            {"code":"000001","name":"보유가상1","account_ids":["1234567890"],"quantity":10,"average_price":1000,"current_price":1100,"profit_loss":1000,"profit_rate":10,"trend":"uptrend","moving_averages":{"ma5":1080,"ma20":1050,"ma60":1000},"review_status":"add_on_strength_candidate","decision":{"summary":"보유 관찰","positive_conditions":["20일선 유지"],"risk_conditions":["저점 이탈"]},"warnings":[]},
            {"code":"000002","name":"보유가상2","account_ids":["87654321"],"quantity":2,"average_price":2000,"current_price":1900,"profit_loss":-200,"profit_rate":-5,"trend":"sideways","moving_averages":{},"review_status":"no_action","decision":{},"warnings":[]},
        ]}, "warnings":[], "errors":[],
    }


def readonly_window_with_premarket(tmp_path):
    directory=tmp_path/"briefings"/"2026"/"07"/"24"; directory.mkdir(parents=True)
    payload=premarket_payload()
    (directory/"pre_market.json").write_text(json.dumps(payload,ensure_ascii=False),encoding="utf-8")
    (directory/"pre_market.md").write_text("## 코스피 주도주\nMARKDOWN_SHOULD_NOT_BE_PARSED",encoding="utf-8")
    app=QApplication.instance() or QApplication([])
    window=DashboardMainWindow(tmp_path/"briefings",recommendation_root=tmp_path/"recommendations",connection_state=lambda:"DISCONNECTED",trading_day_status="주말",shutdown=lambda:None,open_folder=lambda:None,read_only=True,standalone=True,next_trading_day=lambda _:date(2026,7,27),clock=lambda:datetime(2026,7,26,12))
    window._test_app=app
    return window


def test_required_readonly_tab_contract_and_order(tmp_path):
    window=readonly_window_with_premarket(tmp_path)
    actual=tuple(window._tabs.tabText(index) for index in range(window._tabs.count()))
    assert actual == REQUIRED_READONLY_TABS
    window.stop()


def test_structured_premarket_sections_are_independent_and_market_safe(tmp_path):
    window=readonly_window_with_premarket(tmp_path)
    pre=window._result_views["pre_market"].toPlainText(); kospi=table_text(window._leader_views["kospi"]); kosdaq=table_text(window._leader_views["kosdaq"]); holdings=widget_text(window._readonly_holdings)
    assert "장전 중립" in pre and "독립 탭" in pre
    assert all(value not in pre for value in ("코스피가상1","코스닥가상1","보유가상1","MARKDOWN_SHOULD_NOT_BE_PARSED"))
    assert kospi.count("코스피가상1")==1 and "코스닥가상1" not in kospi and "잘못혼합" not in kospi
    assert "코스닥가상1" in kosdaq and "코스피가상1" not in kosdaq
    assert "보유가상1" in holdings and "보유가상2" in holdings and "마지막 저장 자료" in holdings
    assert all(secret not in holdings for secret in ("1234567890","87654321","account_ids","kiwoom_accounts"))
    assert all(token not in pre+kospi+kosdaq+holdings for token in ("None","null","unknown","score","review_status"))
    window.stop()


def test_required_tabs_survive_missing_or_corrupt_premarket(tmp_path):
    directory=tmp_path/"briefings"/"2026"/"07"/"26"; directory.mkdir(parents=True)
    (directory/"pre_market.json").write_text("{broken",encoding="utf-8")
    app=QApplication.instance() or QApplication([])
    window=DashboardMainWindow(tmp_path/"briefings",recommendation_root=tmp_path/"recommendations",connection_state=lambda:"DISCONNECTED",trading_day_status="주말",shutdown=lambda:None,open_folder=lambda:None,read_only=True,standalone=True,next_trading_day=lambda _:date(2026,7,27),clock=lambda:datetime(2026,7,26,12))
    assert tuple(window._tabs.tabText(i) for i in range(window._tabs.count())) == REQUIRED_READONLY_TABS
    assert "저장된 코스피 주도주 결과가 없습니다" in window._leader_notices["kospi"].text()
    assert "저장된 코스닥 주도주 결과가 없습니다" in window._leader_notices["kosdaq"].text()
    assert "저장된 보유종목 분석 결과가 없습니다" in widget_text(window._readonly_holdings)
    window.stop()


def test_stock_tabs_use_real_tables_and_cards(tmp_path):
    window=readonly_window_with_premarket(tmp_path)
    assert isinstance(window._leader_views["kospi"],QTableWidget) and isinstance(window._leader_views["kosdaq"],QTableWidget)
    assert tuple(window._leader_views["kospi"].horizontalHeaderItem(i).text() for i in range(window._leader_views["kospi"].columnCount())) == ("순위","종목명","종목코드","등락률","거래대금","외국인","기관","핵심 근거","위험","업종/테마","기술적 위치")
    assert window._leader_views["kospi"].alternatingRowColors() and "alternate-background-color:#f7f7f7" in window._leader_views["kospi"].styleSheet()
    assert window._leader_views["kospi"].item(0,3).foreground().color().name()=="#c62828"
    assert isinstance(window._readonly_holdings,QScrollArea) and len(window._holding_cards)==2 and all(isinstance(card,QFrame) for card in window._holding_cards)
    assert "background:#fff4f4" in window._holding_cards[0].styleSheet() and "background:#f1f6ff" in window._holding_cards[1].styleSheet()
    assert isinstance(window._recommendations,QScrollArea)
    window.stop()
