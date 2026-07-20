# -*- coding: utf-8 -*-

from datetime import date, datetime

from qz_briefing.briefing.leadership import (
    KiwoomLeadershipCollector, KiwoomLeadershipDataSource, merge_rankings,
    score_leader, score_rebound,
)
from qz_briefing.briefing.models import BriefingContext, BriefingType
from qz_briefing.briefing.pipeline import compare_leadership
from qz_briefing.briefing.renderer import render_markdown
from qz_briefing.briefing.technical_indicators import macd_12_26_9, rsi14


class Queue:
    def __init__(self): self.requests = []
    def request_rows(self, request): self.requests.append(request); return []


def test_official_candidate_and_daily_requests() -> None:
    queue = Queue(); source = KiwoomLeadershipDataSource(queue)
    source.ranking("OPT10027", "001"); source.ranking("OPT10030", "101"); source.ranking("OPT10032", "001"); source.daily("005930", "2026-07-21")
    assert [request.tr_code for request in queue.requests] == ["OPT10027", "OPT10030", "OPT10032", "OPT10081"]
    assert queue.requests[0].inputs["시장구분"] == "001"
    assert queue.requests[0].inputs["종목조건"] == "16"
    assert queue.requests[1].inputs["시장구분"] == "101"
    assert queue.requests[2].inputs == {"시장구분": "001", "관리종목포함": "0", "거래소구분": ""}
    assert queue.requests[3].inputs == {"종목코드": "005930", "기준일자": "20260721", "수정주가구분": "1"}


def test_duplicate_codes_merge_and_preserve_source_ranks() -> None:
    merged = merge_rankings([[{"종목코드": "A", "종목명": "가"}], [{"종목코드": "A", "현재가": "100"}], [{"종목코드": "B"}]])
    assert set(merged) == {"A", "B"}
    assert merged["A"]["source_ranks"] == {0: 1, 1: 1}


def test_leader_score_uses_value_sustainability_and_relative_strength() -> None:
    strong = score_leader({"code": "A", "change_rate": 8.0, "current_price": 108, "open": 100, "high": 110, "trading_value_rank": 1}, 2.0)
    weak_value = score_leader({"code": "B", "change_rate": 15.0, "current_price": 100, "open": 99, "high": 120, "trading_value_rank": None}, 2.0)
    assert strong["score"] > weak_value["score"]
    assert "거래대금 1위" in strong["reasons"]
    assert "고가 대비 큰 폭 이탈" in weak_value["warnings"]


def test_stable_tie_sort_policy_is_score_rank_then_code() -> None:
    rows = [{"code": "B", "score": 5.0, "trading_value_rank": 2}, {"code": "A", "score": 5.0, "trading_value_rank": 2}]
    rows.sort(key=lambda row: (-row["score"], row["trading_value_rank"], row["code"]))
    assert [row["code"] for row in rows] == ["A", "B"]


def history(count: int = 40, rising: bool = True):
    closes = [80 + i * 0.2 for i in range(count - 2)] + [86, 88] if rising else [80 + i for i in range(count)]
    return [{"close": value, "low": value - 2, "volume": 2000 if i == count - 1 else 1000} for i, value in enumerate(closes)]


def test_rebound_requires_upturn_and_avoids_chasing() -> None:
    candidate = {"code": "A", "change_rate": 3.0, "history": history()}
    result = score_rebound(candidate, -1.0)
    assert result is not None and result["score"] >= 5
    chasing = {"code": "B", "change_rate": 20.0, "history": history()}
    assert score_rebound(chasing, 1.0)["score"] < result["score"]


def test_rsi14_macd_and_insufficient_history() -> None:
    closes = [100 + i * 0.4 + (-1 if i % 3 == 0 else 0) for i in range(50)]
    assert rsi14(closes) is not None
    assert macd_12_26_9(closes) is not None
    assert rsi14(closes[:14]) is None
    assert macd_12_26_9(closes[:33]) is None


class Source:
    def __init__(self): self.calls = []
    def ranking(self, tr_code, market_code):
        self.calls.append((tr_code, market_code))
        if market_code == "001": raise RuntimeError("kospi failed")
        return [{"종목코드": "KQ", "종목명": "코스닥주", "현재가": "100", "등락률": "5", "현재거래량": "1000", "거래대금": "10000", "현재순위": "1"}]
    def daily(self, code, target_date):
        return list(reversed([{"일자": str(i), "현재가": str(80 + i), "시가": str(79 + i), "고가": str(81 + i), "저가": str(78 + i), "거래량": "1000"} for i in range(40)]))


def test_one_market_failure_does_not_stop_other_and_does_not_fill_ten() -> None:
    context = BriefingContext(BriefingType.INTRADAY_10AM, date(2026, 7, 21), datetime.now(), datetime.now(), "open", "weekday")
    result = KiwoomLeadershipCollector(Source()).collect(context)
    assert result["errors"]
    assert len(result["kosdaq"]) < 10
    assert any("10개 미만" in warning for warning in result["warnings"])


def test_pre_market_new_maintained_dropped() -> None:
    comparison = compare_leadership({"kospi": [{"code": "A"}, {"code": "B"}], "kosdaq": []}, {"kospi": [{"code": "B"}, {"code": "C"}], "kosdaq": []})
    assert comparison == {"new": ["A"], "maintained": ["B"], "dropped": ["C"]}


def test_markdown_renders_korean_leadership_without_buy_instruction() -> None:
    result = {"briefing_type": "intraday_10am", "trading_date": "2026-07-21", "status": "completed", "warnings": [], "errors": [], "collectors": {}, "analysis": {"summary": "중립", "market_state": "neutral", "confidence": "medium", "indicator_comments": {}, "signals": [], "warnings": [], "comparison_with_pre_market": {}}, "leadership": {"kospi": [{"code": "A", "name": "테스트", "score": 8.2, "current_price": 10000, "change_rate": 5.1, "trading_value": 123456, "reasons": ["거래대금 1위"], "warnings": []}], "kosdaq": [], "rebound_candidates": []}}
    text = render_markdown(result)
    assert "코스피 주도주 TOP 10" in text and "123,456" in text
    assert "매수 지시가 아닙니다" in text
