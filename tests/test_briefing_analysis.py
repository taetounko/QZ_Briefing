# -*- coding: utf-8 -*-
"""Pure rule and Korean renderer tests."""

import copy

from qz_briefing.briefing.analysis import analyze_briefing
from qz_briefing.briefing.renderer import render_markdown


def payload(sign: int = 1) -> dict[str, object]:
    return {
        "briefing_type": "intraday_10am", "trading_date": "2026-07-21",
        "completed_at": "2026-07-21T10:00:00", "status": "completed",
        "market_calendar": {"status": "open", "reason": "weekday"},
        "warnings": [], "errors": [],
        "collectors": {
            "kiwoom_market_indices": {"status": "success", "data": {"indices": [
                {"market": "KOSPI", "change_rate": 1.2 * sign},
                {"market": "KOSDAQ", "change_rate": 0.8 * sign},
                {"market": "KOSPI200", "change_rate": 1.4 * sign},
            ]}},
            "kiwoom_core_market": {"status": "success", "data": {"securities": [
                {"code": "005930", "change_rate": 2.1 * sign},
                {"code": "000660", "change_rate": 1.7 * sign},
            ]}},
            "kiwoom_investor_flows": {"status": "success", "data": {"markets": [{
                "market": "KOSPI", "investors": [
                    {"investor": "individual", "net_buy": -1000 * sign},
                    {"investor": "foreigner", "net_buy": 2500 * sign},
                    {"investor": "institution", "net_buy": 1200 * sign},
                ],
            }]}},
            "kiwoom_derivatives_flows": {"status": "success", "data": {
                "kospi200_futures": {"open_interest": 345678, "investors": {
                    "foreign": {"net_buy": 800 * sign},
                    "individual": {"net_buy": -300 * sign},
                    "institution": {"net_buy": None},
                }},
                "program_trading": {"total": {"net_buy": 1800 * sign}},
            }},
        },
    }


def analyze(data: dict[str, object]) -> dict[str, object]:
    result = analyze_briefing(data)
    data["analysis"] = result
    return result


def test_strong_bullish_combination_and_signals() -> None:
    result = analyze(payload(1))
    assert result["market_state"] == "strong_bullish"
    assert result["score"] == 9
    assert "대형주 동반 강세" in result["signals"]
    assert "외국인 현물·선물 동시 순매수" in result["signals"]


def test_strong_bearish_combination_and_signals() -> None:
    result = analyze(payload(-1))
    assert result["market_state"] == "strong_bearish"
    assert result["score"] == -9
    assert "대형주 동반 약세" in result["signals"]
    assert "외국인 현물·선물 동시 순매도" in result["signals"]


def test_mixed_combination_is_neutral() -> None:
    data = payload()
    data["collectors"]["kiwoom_market_indices"]["data"]["indices"][1]["change_rate"] = -0.8
    data["collectors"]["kiwoom_core_market"]["data"]["securities"][1]["change_rate"] = -1.7
    data["collectors"]["kiwoom_investor_flows"]["data"]["markets"][0]["investors"][2]["net_buy"] = -1200
    data["collectors"]["kiwoom_derivatives_flows"]["data"]["program_trading"]["total"]["net_buy"] = -1800
    result = analyze(data)
    assert result["market_state"] == "neutral"


def test_insufficient_data_is_not_misclassified_as_bearish() -> None:
    data = payload()
    data["collectors"] = {}
    result = analyze(data)
    assert result["market_state"] == "insufficient_data"
    assert "데이터 부족 경고" in result["signals"]


def test_index_flow_divergence_and_program_alignment() -> None:
    data = payload()
    indices = data["collectors"]["kiwoom_market_indices"]["data"]["indices"]
    indices[0]["change_rate"] = -1.0
    result = analyze(data)
    assert "수급은 강하지만 지수가 약한 다이버전스" in result["signals"]
    assert "프로그램 매매와 외국인 수급 방향 일치" in result["signals"]


def test_pre_market_comparison_preserves_previous_and_finds_changes() -> None:
    previous = payload()
    previous_analysis = analyze_briefing(previous)
    previous["analysis"] = previous_analysis
    current = copy.deepcopy(previous)
    current["collectors"]["kiwoom_market_indices"]["data"]["indices"][0]["change_rate"] = -0.5
    result = analyze_briefing(current, previous)
    comparison = result["comparison_with_pre_market"]
    assert comparison["available"] is True
    assert comparison["changes"]["KOSPI"] == {
        "pre_market": 1.2, "current": -0.5, "change": -1.7
    }
    assert previous["collectors"]["kiwoom_market_indices"]["data"]["indices"][0]["change_rate"] == 1.2


def test_missing_pre_market_and_derivatives_add_warnings() -> None:
    data = payload()
    del data["collectors"]["kiwoom_derivatives_flows"]
    result = analyze_briefing(data, None)
    assert result["comparison_with_pre_market"]["available"] is False
    assert any("최근월물" in warning for warning in result["warnings"])


def test_markdown_is_korean_and_formats_numbers_with_units() -> None:
    data = payload()
    data["analysis"] = analyze_briefing(data)
    markdown = render_markdown(data)
    assert "# QZ 한국 시장 브리핑" in markdown
    assert "한눈에 보는 시장 판단" in markdown
    assert "+2,500 official scale unspecified" in markdown
    assert "+1,800 백만원" in markdown
    assert "현재 해석:" in markdown
    assert "주의:" in markdown
