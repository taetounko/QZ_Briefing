# -*- coding: utf-8 -*-

import json
from datetime import date, datetime
from pathlib import Path

from qz_briefing.briefing.holdings import (
    HoldingsCollector, bottom_state, load_holdings, moving_average, review_state,
    trend_state,
)
from qz_briefing.briefing.models import BriefingContext, BriefingType
from qz_briefing.briefing.pipeline import compare_holdings
from qz_briefing.briefing.renderer import render_markdown
from qz_briefing.briefing import BriefingStorage, DailyBriefingPipeline


def write_config(path: Path, holdings: list[dict]) -> None:
    path.write_text(json.dumps({"schema_version": 1, "holdings": holdings}, ensure_ascii=False), encoding="utf-8")


VALID = {"code": "005930", "name": "삼성전자", "quantity": 10, "average_price": 100, "target_price": 150, "stop_price": 80, "maximum_additional_budget": None, "memo": ""}


def test_config_missing_valid_corrupt_and_unknown_fields(tmp_path: Path) -> None:
    missing = load_holdings(tmp_path / "missing.json")
    assert missing["holdings"] == [] and not missing["errors"]
    path = tmp_path / "holdings.json"; write_config(path, [{**VALID, "extra": 1}])
    loaded = load_holdings(path)
    assert loaded["holdings"][0]["code"] == "005930" and loaded["warnings"]
    path.write_text("{broken", encoding="utf-8")
    assert load_holdings(path)["errors"]


def test_duplicate_invalid_quantity_and_average_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"
    write_config(path, [VALID, VALID, {**VALID, "code": "000001", "quantity": 0}, {**VALID, "code": "000002", "average_price": -1}])
    result = load_holdings(path)
    assert len(result["holdings"]) == 1
    assert len(result["errors"]) == 3


class StockSource:
    def __init__(self, fail_code=None): self.fail_code = fail_code
    def get_stock_basic_info(self, code):
        if code == self.fail_code: raise RuntimeError("basic failed")
        return {"종목코드": code, "종목명": "삼성전자", "현재가": "+120"}


class DailySource:
    def __init__(self, fail=False, fail_code=None): self.fail, self.fail_code = fail, fail_code
    def daily(self, code, target_date):
        if self.fail or code == self.fail_code: raise RuntimeError("daily failed")
        chronological = [{"일자": str(i), "현재가": str(61 + i), "시가": str(60 + i), "고가": str(63 + i), "저가": str(59 + i), "거래량": "2000" if i == 59 else "1000"} for i in range(60)]
        return list(reversed(chronological))


def context(kind=BriefingType.INTRADAY_10AM):
    now = datetime(2026, 7, 22, 10)
    return BriefingContext(kind, date(2026, 7, 22), now, now, "open", "weekday")


def test_profit_distances_technical_and_portfolio_aggregate(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"; write_config(path, [VALID])
    result = HoldingsCollector(path, StockSource(), DailySource(), leadership_codes=lambda: {"005930"}).collect(context())
    item = result["holdings"][0]
    assert item["investment_amount"] == 1000
    assert item["valuation_amount"] == 1200
    assert item["profit_loss"] == 200 and item["profit_rate"] == 20
    assert item["target_distance"] == 25 and round(item["stop_distance"], 2) == -33.33
    assert item["moving_averages"]["ma5"] is not None
    assert item["rsi14"] is not None and item["macd"] is not None
    assert item["volume_multiple"] == 2
    assert result["portfolio"]["profit_loss"] == 200
    assert item["fees_and_taxes_included"] is False


def test_daily_failure_keeps_profit_calculation(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"; write_config(path, [VALID])
    item = HoldingsCollector(path, StockSource(), DailySource(fail=True)).collect(context())["holdings"][0]
    assert item["profit_loss"] == 200
    assert item["trend"] == "insufficient_data" and item["warnings"]


def test_one_holding_failure_does_not_stop_next(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"; write_config(path, [VALID, {**VALID, "code": "000660", "name": "SK하이닉스"}])
    result = HoldingsCollector(path, StockSource(fail_code="005930"), DailySource()).collect(context())
    assert len(result["holdings"]) == 1 and result["errors"]


def test_one_daily_failure_does_not_stop_next_technical_analysis(tmp_path: Path) -> None:
    path = tmp_path / "holdings.json"
    write_config(path, [VALID, {**VALID, "code": "000660", "name": "SK하이닉스"}])
    result = HoldingsCollector(path, StockSource(), DailySource(fail_code="005930")).collect(context())
    by_code = {item["code"]: item for item in result["holdings"]}
    assert by_code["005930"]["trend"] == "insufficient_data"
    assert by_code["000660"]["trend"] != "insufficient_data"
    assert by_code["000660"]["bottom_confirmation"] != "insufficient_data"
    assert by_code["000660"]["review_status"] != "insufficient_data"


def test_trend_and_bottom_states() -> None:
    assert trend_state(130, 120, 110, 100) == "strong_uptrend"
    assert trend_state(70, 80, 90, 100) == "strong_downtrend"
    assert trend_state(100, None, 90, 80) == "insufficient_data"
    lows = [90.0] * 20; closes = [90.0] * 17 + [92, 95, 100]
    assert bottom_state(100, lows, closes, 95) == "confirmed"
    assert bottom_state(91, lows, [90.0] * 19 + [91], 95) == "attempting_bottom"


def test_review_rules_prevent_simple_cheap_averaging_and_cover_risk_states() -> None:
    base = dict(profit_rate=-20, bottom="not_confirmed", above_ma20=False, volume_multiple=1, leadership=False, stop_breached=False)
    assert review_state(trend="strong_downtrend", **base) == "averaging_down_high_risk"
    assert review_state(trend="downtrend", **base) == "wait"
    candidate = {**base, "bottom": "confirmed", "above_ma20": True}
    assert review_state(trend="sideways", **candidate) == "averaging_down_candidate"
    strength = dict(profit_rate=10, trend="uptrend", bottom="not_confirmed", above_ma20=True, volume_multiple=1.5, leadership=True, stop_breached=False)
    assert review_state(**strength) == "add_on_strength_candidate"
    assert review_state(**{**strength, "stop_breached": True}) == "exit_review"


def test_moving_average_and_holdings_pre_market_comparison() -> None:
    assert moving_average([1, 2, 3, 4, 5], 5) == 3
    current = {"holdings": [{"code": "A", "trend": "uptrend", "bottom_confirmation": "confirmed", "review_status": "no_action"}]}
    previous = {"holdings": [{"code": "A", "trend": "sideways", "bottom_confirmation": "attempting_bottom", "review_status": "wait"}]}
    changes = compare_holdings(current, previous)
    assert changes[0]["trend"] == {"pre_market": "sideways", "current": "uptrend"}


def test_holdings_markdown_is_korean_and_explicitly_not_an_order() -> None:
    data = {"basis": "latest_close", "portfolio": {"investment_amount": 1000, "valuation_amount": 1200, "profit_loss": 200, "profit_rate": 20}, "holdings": [{"code": "005930", "name": "삼성전자", "quantity": 10, "average_price": 100, "current_price": 120, "profit_loss": 200, "profit_rate": 20, "trend": "uptrend", "moving_averages": {"ma5": 115, "ma20": 110, "ma60": 100}, "bottom_confirmation": "confirmed", "review_status": "add_on_strength_candidate"}]}
    result = {"briefing_type": "pre_market", "trading_date": "2026-07-22", "status": "completed", "warnings": [], "errors": [], "collectors": {}, "analysis": {"summary": "중립", "market_state": "neutral", "confidence": "low", "indicator_comments": {}, "signals": [], "warnings": [], "comparison_with_pre_market": {}}, "holdings_analysis": data}
    text = render_markdown(result)
    assert "보유종목 종합" in text and "총 투자금" in text
    assert "최근 종가" in text and "확정적인 매수·매도 지시가 아닙니다" in text


def test_pipeline_saves_holdings_analysis_json_and_markdown(tmp_path: Path) -> None:
    config = tmp_path / "holdings.json"; write_config(config, [VALID])
    storage = BriefingStorage(tmp_path / "briefings")
    collector = HoldingsCollector(config, StockSource(), DailySource())
    result = DailyBriefingPipeline(storage, [collector], clock=lambda: datetime(2026, 7, 22, 8)).run(
        BriefingType.PRE_MARKET, date(2026, 7, 22),
        market_calendar_status="open", market_calendar_reason="weekday",
    )
    saved = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
    markdown = Path(result.markdown_path).read_text(encoding="utf-8")
    assert saved["holdings_analysis"]["holdings"][0]["code"] == "005930"
    assert saved["collectors"]["holdings_analysis"]["data"]["holdings"][0]["code"] == "005930"
    assert "보유종목 종합" in markdown
