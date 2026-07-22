# -*- coding: utf-8 -*-

from qz_briefing.briefing.decision_guidance import holding_decision, market_decision, priority
from qz_briefing.briefing.analysis import analyze_briefing
from qz_briefing.briefing.renderer import render_markdown


def market(sign=1, *, futures_sign=None):
    futures_sign = sign if futures_sign is None else futures_sign
    return {"briefing_type": "intraday_10am", "trading_date": "2026-07-22", "status": "completed", "warnings": [], "errors": [], "collectors": {
        "kiwoom_market_indices": {"data": {"indices": [{"market": name, "change_rate": value * sign} for name, value in (("KOSPI", 1.5), ("KOSDAQ", 1.1), ("KOSPI200", 1.7))]}},
        "kiwoom_core_market": {"data": {"securities": [{"code": "005930", "change_rate": 2 * sign}, {"code": "000660", "change_rate": 1.8 * sign}]}},
        "kiwoom_investor_flows": {"data": {"markets": [{"market": "KOSPI", "investors": [{"investor": "foreigner", "net_buy": 1000 * sign}, {"investor": "institution", "net_buy": 500 * sign}]}]}},
        "kiwoom_derivatives_flows": {"data": {"kospi200_futures": {"investors": {"foreign": {"net_buy": 500 * futures_sign}}}, "program_trading": {"total": {"net_buy": 700 * sign}}}},
    }}


def holding(**changes):
    base = {"trend": "strong_downtrend", "bottom_confirmation": "not_confirmed", "review_status": "averaging_down_high_risk", "current_price": 40, "profit_rate": -60, "moving_averages": {"ma5": 50, "ma20": 60, "ma60": 80}, "high_low": {"low20": 38, "high20": 70}, "warnings": [], "next_session_observation": "다음 저점 확인"}
    base.update(changes); return base


def test_strong_market_and_confidence_are_explainable():
    decision = market_decision(market())
    assert decision["state"] == "strong_uptrend"
    assert 60 <= decision["confidence"] <= 100
    assert decision["evidence"] and decision["confirmation_conditions"] and decision["invalidation_conditions"]


def test_market_flow_conflict_lowers_confidence_and_is_reported():
    aligned = market_decision(market())
    conflict = market_decision(market(futures_sign=-1))
    assert conflict["conflicts"]
    assert conflict["confidence"] < aligned["confidence"]


def test_missing_values_are_not_treated_as_zero_and_market_not_open():
    assert market_decision({"status": "completed", "warnings": [], "errors": [], "collectors": {}})["state"] == "insufficient_data"
    assert market_decision({"status": "no_market_open"})["state"] == "market_not_open"


def test_large_loss_in_downtrend_is_never_averaging_candidate():
    decision = holding_decision(holding())
    assert decision["action_level"] == "do_not_add"
    assert "손실률" in decision["summary"]
    assert priority(decision) == 3


def test_large_loss_with_missing_technical_data_stays_insufficient():
    decision = holding_decision(holding(trend="insufficient_data", review_status="insufficient_data"))
    assert decision["action_level"] == "insufficient_data"


def test_bottom_candidate_is_conditional_not_a_buy_signal():
    decision = holding_decision(holding(trend="sideways", bottom_confirmation="confirmed", review_status="averaging_down_candidate", current_price=65))
    assert decision["action_level"] == "conditional_add_review"
    assert "후보는 매수 신호가 아닙니다" in decision["summary"]


def test_exit_and_price_conditions_are_logical():
    decision = holding_decision(holding(bottom_confirmation="failed", review_status="exit_review"))
    assert decision["action_level"] == "exit_condition_check"
    assert all(value > 0 for value in decision["price_conditions"].values())
    assert decision["price_conditions"]["invalidation_price"] <= 40


def test_market_stock_conflict_is_explicit():
    decision = holding_decision(holding(trend="uptrend", review_status="no_action"), {"state": "strong_decline"})
    assert decision["conflicts"]


def test_analysis_json_and_markdown_use_same_decision():
    result = market(); result["analysis"] = analyze_briefing(result)
    markdown = render_markdown(result)
    assert "decision" in result["analysis"]
    assert "## 오늘의 결론" in markdown
    assert result["analysis"]["decision"]["headline"] in markdown
    assert "무조건 매수" not in markdown and "무조건 매도" not in markdown
