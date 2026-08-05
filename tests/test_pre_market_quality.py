# -*- coding: utf-8 -*-

from qz_briefing.briefing.pipeline import build_leadership_output
from qz_briefing.briefing.quality import evaluate_pre_market_quality, unavailable_analysis


def wrapper(data):
    return {"status": "success", "error": None, "data": data}


def payload(*, zero=False, leadership_at="2026-08-05T09:00:06", flow_unit="백만원"):
    rate = 0 if zero else 1.0
    market_row = lambda market: {"market": market, "open": 0 if zero else 100, "high": 0 if zero else 101, "low": 0 if zero else 99, "volume": 0 if zero else 1000, "change_rate": rate}
    stock_row = lambda code: {"code": code, "open": 0 if zero else 100, "high": 0 if zero else 101, "low": 0 if zero else 99, "volume": 0 if zero else 1000, "change_rate": rate}
    return {"collectors": {
        "kiwoom_market_indices": wrapper({"collected_at":"2026-08-05T09:00:02", "indices":[market_row("KOSPI"), market_row("KOSDAQ")]}),
        "kiwoom_core_market": wrapper({"collected_at":"2026-08-05T09:00:03", "securities":[stock_row("005930"), stock_row("000660")]}),
        "kiwoom_investor_flows": wrapper({"collected_at":"2026-08-05T09:00:04", "unit":flow_unit, "markets":[{"market":"KOSPI", "investors":[{"investor":"foreigner","net_buy":10},{"investor":"institution","net_buy":-3}]}]}),
        "kiwoom_derivatives_flows": wrapper({"collected_at":"2026-08-05T09:00:05", "program_trading":{"total":{"net_buy":7}}}),
        "kiwoom_market_leadership": wrapper({"collected_at":leadership_at, "kospi":[], "kosdaq":[]}),
    }}


def test_zero_opening_snapshot_is_unavailable_and_has_no_score_or_direction():
    quality = evaluate_pre_market_quality(payload(zero=True))
    analysis = unavailable_analysis(quality)
    assert quality["DATA_QUALITY"] == "unavailable"
    assert quality["status"] == "incomplete"
    assert analysis["score"] is None and analysis["signals"] == []
    assert "판단을 보류" in analysis["summary"]
    assert all(term not in analysis["summary"] for term in ("하락 우위", "중립·혼조"))


def test_mixed_0900_and_0901_collection_is_blocked():
    quality = evaluate_pre_market_quality(payload(leadership_at="2026-08-05T09:01:15"))
    assert quality["time_consistent"] is False
    assert quality["BRIEFING_QUALITY"] == "unavailable"


def test_complete_and_partial_quality_are_distinct():
    assert evaluate_pre_market_quality(payload())["BRIEFING_QUALITY"] == "complete"
    value = payload(); value["collectors"]["kiwoom_derivatives_flows"]["data"]["program_trading"] = {}
    quality = evaluate_pre_market_quality(value)
    assert quality["status"] == "degraded" and quality["BRIEFING_QUALITY"] == "partial"


def test_zero_flows_without_official_unit_are_not_neutral():
    value = payload(flow_unit=None)
    investors = value["collectors"]["kiwoom_investor_flows"]["data"]["markets"][0]["investors"]
    for row in investors: row["net_buy"] = 0
    quality = evaluate_pre_market_quality(value)
    assert quality["spot_flow_usable"] is False
    assert quality["market_score_allowed"] is False
    assert quality["BRIEFING_QUALITY"] == "partial"


def test_leader_has_collection_context_and_missing_value_rank_lowers_confidence():
    source={"collected_at":"2026-08-05T09:01:15", "kospi":[{"code":"A","name":"가상","change_rate":5,"current_price":100,"open":95,"high":101,"trading_value":1234,"trading_value_rank":None}], "kosdaq":[]}
    row=build_leadership_output(source,{"KOSPI":1})["kospi"][0]
    assert row["collected_at"] == "2026-08-05T09:01:15"
    assert row["seconds_after_open"] == 75 and row["early_session_volatility_risk"]
    assert row["confidence"] == "low"
