import json
import os
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

from qz_briefing.__main__ import parse_cli_arguments, run
from qz_briefing.scheduling import MarketStatus, TradingDayResult
from qz_briefing.ui.main_window import DashboardMainWindow
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
    text=window._recommendations.toPlainText()
    assert window._refresh_timer.interval()==30000 and text.count("가상종목") == 3
    assert all(token not in text for token in ("None","null","unknown",str(tmp_path)))
    window.stop()
