# -*- coding: utf-8 -*-

import json
from datetime import date, datetime
from pathlib import Path

from qz_briefing.briefing import BriefingStorage, BriefingType, DailyBriefingPipeline
from qz_briefing.briefing.market_close import (
    build_next_session_watchlist, compare_market_close, evaluate_market_close,
)


DAY = date(2026, 7, 22)


def result(*, state="bullish", kospi=1.0, samsung=2.0, foreign=100, profit=1000, leaders=("A",)):
    return {
        "trading_date": DAY.isoformat(),
        "analysis": {"market_state": state},
        "collectors": {
            "kiwoom_market_indices": {"data": {"indices": [{"market": "KOSPI", "change_rate": kospi}]}},
            "kiwoom_core_market": {"data": {"securities": [{"code": "005930", "change_rate": samsung}]}},
            "kiwoom_investor_flows": {"data": {"markets": [{"market": "KOSPI", "investors": [{"investor": "foreigner", "net_buy": foreign}]}]}},
            "kiwoom_derivatives_flows": {"data": {"program_trading": {"total": {"net_buy": foreign}}}},
        },
        "leadership": {"kospi": [{"code": code, "name": code} for code in leaders], "kosdaq": [], "rebound_candidates": []},
        "holdings_analysis": {"portfolio": {"profit_loss": profit, "profit_rate": 1.0}, "holdings": [{"code": "005930", "name": "삼성전자", "trend": "uptrend", "review_status": "no_action"}]},
    }


def test_market_close_comparison_preserves_missing_values_and_changes() -> None:
    current = result(kospi=2.0, samsung=3.0, foreign=200, profit=1500, leaders=("A", "B"))
    pre = result(kospi=1.0, samsung=2.0, foreign=100, profit=1000, leaders=("A",))
    comparison = compare_market_close(current, pre, None)
    changes = comparison["pre_market"]["changes"]
    assert changes["indices"]["KOSPI"]["change"] == 1.0
    assert changes["large_caps"]["005930"]["change"] == 1.0
    assert changes["spot_flows"]["foreigner"]["change"] == 100
    assert changes["portfolio"]["profit_loss"]["change"] == 500
    assert changes["leadership"] == {"new": ["B"], "maintained": ["A"], "dropped": []}
    assert comparison["intraday_10am"]["available"] is False

    missing = result(); missing["collectors"] = {}
    missing_change = compare_market_close(missing, pre, None)["pre_market"]["changes"]
    assert missing_change["indices"]["KOSPI"]["change"] == "not_available"


def test_market_close_evaluation_and_watchlist() -> None:
    current = result(state="strong_bullish")
    comparison = compare_market_close(current, result(state="bullish"), result(state="bearish"))
    analysis = evaluate_market_close(current, comparison)
    watchlist = build_next_session_watchlist(current)
    assert analysis["market_conclusion"] == "상승장"
    assert analysis["pre_market_evaluation"] == "일부 적중"
    assert analysis["intraday_evaluation"] == "반전"
    assert any(item["category"] == "market_indicator" for item in watchlist)
    assert any(item["category"] == "holding_opportunity" for item in watchlist)


def test_market_close_pipeline_loads_same_day_results_and_saves_files(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    for briefing_type in (BriefingType.PRE_MARKET, BriefingType.INTRADAY_10AM):
        payload = result(); payload.update({"schema_version": 1, "briefing_type": briefing_type.value, "status": "completed"})
        storage.save(DAY, briefing_type, payload, "prior")
    pipeline = DailyBriefingPipeline(storage, [], clock=lambda: datetime(2026, 7, 22, 15, 40))
    run = pipeline.run(BriefingType.MARKET_CLOSE, DAY, market_calendar_status="open", market_calendar_reason="weekday")
    saved = json.loads(Path(run.json_path).read_text(encoding="utf-8"))
    markdown = Path(run.markdown_path).read_text(encoding="utf-8")
    assert Path(run.json_path).name == "market_close.json"
    assert Path(run.markdown_path).name == "market_close.md"
    assert saved["metadata"]["briefing_type"] == "market_close"
    assert saved["previous_results"] == {"pre_market_loaded": True, "intraday_10am_loaded": True, "warnings": []}
    assert "market_close_analysis" in saved and "session_comparison" in saved
    assert saved["next_session_watchlist"]
    assert "장마감 브리핑" in markdown and "다음 거래일 핵심 관찰 목록" in markdown
    assert pipeline.run(BriefingType.MARKET_CLOSE, DAY, market_calendar_status="open", market_calendar_reason="weekday").status == "skipped"


def test_market_close_continues_with_missing_or_corrupt_prior_files(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    broken, _ = storage.result_paths(DAY, BriefingType.PRE_MARKET)
    broken.parent.mkdir(parents=True); broken.write_text("{broken", encoding="utf-8")
    pipeline = DailyBriefingPipeline(storage, [], clock=lambda: datetime(2026, 7, 22, 15, 40))
    run = pipeline.run(BriefingType.MARKET_CLOSE, DAY, market_calendar_status="open", market_calendar_reason="weekday")
    saved = json.loads(Path(run.json_path).read_text(encoding="utf-8"))
    assert saved["previous_results"]["pre_market_loaded"] is False
    assert saved["previous_results"]["intraday_10am_loaded"] is False
    assert saved["warnings"]


def test_recent_market_close_finds_friday_not_calendar_yesterday_and_excludes_future(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    friday = date(2026, 7, 17); monday = date(2026, 7, 20)
    payload = {"trading_date": friday.isoformat(), "briefing_type": "market_close", "status": "completed", "market_close_analysis": {"market_conclusion": "혼조장"}}
    storage.save(friday, BriefingType.MARKET_CLOSE, payload, "friday")
    storage.save(date(2026, 7, 21), BriefingType.MARKET_CLOSE, {**payload, "trading_date": "2026-07-21"}, "future")
    loaded, warning = storage.load_recent_market_close(monday)
    assert loaded["trading_date"] == friday.isoformat() and warning is None


def test_pre_market_links_latest_close_and_marks_stale(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    old = date(2026, 7, 1)
    storage.save(old, BriefingType.MARKET_CLOSE, {"trading_date": old.isoformat(), "briefing_type": "market_close", "status": "completed", "market_close_analysis": {"market_conclusion": "약세장"}}, "old")
    pipeline = DailyBriefingPipeline(storage, [], clock=lambda: datetime(2026, 7, 22, 8))
    run = pipeline.run(BriefingType.PRE_MARKET, DAY, market_calendar_status="open", market_calendar_reason="weekday")
    saved = json.loads(Path(run.json_path).read_text(encoding="utf-8"))
    markdown = Path(run.markdown_path).read_text(encoding="utf-8")
    assert saved["previous_market_close"]["trading_date"] == old.isoformat()
    assert any("stale" in warning for warning in saved["warnings"])
    assert "전 거래일 장마감 요약" in markdown
