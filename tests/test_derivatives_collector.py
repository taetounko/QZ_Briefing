# -*- coding: utf-8 -*-
"""Derivatives collector tests; no OCX or external data is used."""

import json
from datetime import date, datetime
from pathlib import Path

from qz_briefing.briefing import (
    BriefingStorage,
    BriefingType,
    DailyBriefingPipeline,
    FuturesContractResolution,
    KiwoomDerivativesDataSource,
    KiwoomDerivativesFlowCollector,
    UnavailableFuturesContractResolver,
)
from qz_briefing.briefing.models import BriefingContext

NOW = datetime(2026, 7, 21, 10, 0)
DAY = date(2026, 7, 21)


def context() -> BriefingContext:
    return BriefingContext(
        briefing_type=BriefingType.INTRADAY_10AM, trading_date=DAY,
        requested_at=NOW, started_at=NOW,
        market_calendar_status="open", market_calendar_reason="weekday",
    )


class FakeQueue:
    def __init__(self, rows: list[dict[str, str]] | None = None) -> None:
        self.requests: list[object] = []
        self.rows = rows or []

    def request(self, request: object) -> dict[str, str]:
        self.requests.append(request)
        return {}

    def request_rows(self, request: object) -> list[dict[str, str]]:
        self.requests.append(request)
        return self.rows


class Resolved:
    def resolve(self) -> FuturesContractResolution:
        return FuturesContractResolution(
            status="resolved", code="FUTURE_FROM_FAKE",
            method="fake_official_list",
        )


class FakeSource:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def get_futures_quote(self, code: str) -> dict[str, str]:
        self.calls.append("quote")
        if self.fail == "quote":
            raise RuntimeError("quote unavailable")
        return {
            "종목명": "코스피200 선물 최근월물", "현재가": " -401.25 ",
            "전일대비": "-2.50", "등락율": "-0.62", "시가": "-403.00",
            "고가": "405.50", "저가": "-399.10", "거래량": "1,234",
            "누적거래대금": "12,345,678", "미결제약정": "345,678",
        }

    def get_futures_investor(
        self, code: str, trading_date: str, investor_code: str
    ) -> dict[str, str]:
        self.calls.append("investor_" + investor_code)
        if self.fail == "investor":
            raise RuntimeError("investor unavailable")
        value = {"09": "-1,234", "08": "+567"}[investor_code]
        return {"종목코드": code, "투자자별순매수수량": value}

    def get_program_trading(self, trading_date: str) -> dict[str, str]:
        self.calls.append("program")
        if self.fail == "program":
            raise RuntimeError("program unavailable")
        return {
            "차익거래매도": "1,000", "차익거래매수": "1,500",
            "차익거래순매수": "+500", "비차익거래매도": "2,000",
            "비차익거래매수": "1,200", "비차익거래순매수": "-800",
            "전체매도": "3,000", "전체매수": "2,700", "전체순매수": "-300",
        }


def test_production_resolver_does_not_hardcode_a_dated_contract() -> None:
    result = UnavailableFuturesContractResolver().resolve()
    assert result.status == "unavailable"
    assert result.code is None
    assert result.warning


def test_official_futures_quote_and_investor_requests() -> None:
    queue = FakeQueue(rows=[{"종목코드": "FUT", "투자자별순매수수량": "-1"}])
    source = KiwoomDerivativesDataSource(queue)  # type: ignore[arg-type]
    source.get_futures_quote("FUT")
    source.get_futures_investor("FUT", "2026-07-21", "09")
    quote, investor = queue.requests
    assert quote.tr_code == "OPT50001"  # type: ignore[attr-defined]
    assert quote.inputs == {"종목코드": "FUT"}  # type: ignore[attr-defined]
    assert investor.tr_code == "OPT50038"  # type: ignore[attr-defined]
    assert investor.inputs == {  # type: ignore[attr-defined]
        "일자구분": "1", "일자": "20260721", "투자자구분": "09",
        "수량금액구분": "1",
    }


def test_official_kospi_program_request_inputs() -> None:
    queue = FakeQueue(rows=[{}])
    KiwoomDerivativesDataSource(queue).get_program_trading("2026-07-21")  # type: ignore[arg-type]
    request = queue.requests[0]
    assert request.tr_code == "OPT90005"  # type: ignore[attr-defined]
    assert request.inputs == {  # type: ignore[attr-defined]
        "날짜": "20260721", "시간구분": "1", "금액수량구분": "1",
        "시장구분": "P00101", "분틱구분": "0", "거래소구분": "",
    }


def test_futures_prices_oi_and_investor_signs_are_normalized() -> None:
    result = KiwoomDerivativesFlowCollector(
        Resolved(), FakeSource(), clock=lambda: NOW
    ).collect(context())
    futures = result["kospi200_futures"]
    assert futures["current"] == 401.25
    assert futures["open"] == 403.0
    assert futures["change"] == -2.5
    assert futures["change_rate"] == -0.62
    assert futures["volume"] == 1234
    assert futures["trading_value"] == 12_345_678
    assert futures["open_interest"] == 345_678
    assert futures["raw"]["현재가"] == " -401.25 "
    assert futures["investors"]["foreign"]["net_buy"] == -1234
    assert futures["investors"]["individual"]["net_buy"] == 567
    assert futures["investors"]["institution"]["net_buy"] is None


def test_program_arbitrage_non_arbitrage_and_total_are_separate() -> None:
    result = KiwoomDerivativesFlowCollector(
        UnavailableFuturesContractResolver(), FakeSource(), clock=lambda: NOW
    ).collect(context())
    program = result["program_trading"]
    assert (program["arbitrage"]["sell"], program["arbitrage"]["buy"], program["arbitrage"]["net_buy"]) == (1000, 1500, 500)
    assert (program["non_arbitrage"]["sell"], program["non_arbitrage"]["buy"], program["non_arbitrage"]["net_buy"]) == (2000, 1200, -800)
    assert (program["total"]["sell"], program["total"]["buy"], program["total"]["net_buy"]) == (3000, 2700, -300)


def test_futures_failure_still_collects_program_trading() -> None:
    source = FakeSource(fail="quote")
    result = KiwoomDerivativesFlowCollector(
        Resolved(), source, clock=lambda: NOW
    ).collect(context())
    assert "program" in source.calls
    assert result["program_trading"]["total"]["net_buy"] == -300
    assert result["errors"]


def test_futures_investor_failure_still_collects_program_trading() -> None:
    source = FakeSource(fail="investor")
    result = KiwoomDerivativesFlowCollector(
        Resolved(), source, clock=lambda: NOW
    ).collect(context())
    assert result["program_trading"]["total"]["net_buy"] == -300
    assert len(result["errors"]) == 2


def test_empty_and_invalid_futures_values_become_none_with_warning() -> None:
    class InvalidSource(FakeSource):
        def get_futures_quote(self, code: str) -> dict[str, str]:
            raw = super().get_futures_quote(code)
            raw["현재가"] = "not-a-number"
            raw["미결제약정"] = ""
            return raw

    result = KiwoomDerivativesFlowCollector(
        Resolved(), InvalidSource(), clock=lambda: NOW
    ).collect(context())
    futures = result["kospi200_futures"]
    assert futures["current"] is None
    assert futures["open_interest"] is None
    assert any("invalid current" in warning for warning in futures["warnings"])


def test_program_failure_keeps_futures_and_marks_pipeline_error(tmp_path: Path) -> None:
    collector = KiwoomDerivativesFlowCollector(
        Resolved(), FakeSource(fail="program"), clock=lambda: NOW
    )
    result = DailyBriefingPipeline(
        BriefingStorage(tmp_path), [collector], clock=lambda: NOW
    ).run(
        BriefingType.PRE_MARKET, DAY,
        market_calendar_status="open", market_calendar_reason="weekday",
    )
    assert result.status == "completed_with_errors"


def test_korean_derivatives_json_is_utf8(tmp_path: Path) -> None:
    pipeline = DailyBriefingPipeline(
        BriefingStorage(tmp_path),
        [KiwoomDerivativesFlowCollector(Resolved(), FakeSource(), clock=lambda: NOW)],
        clock=lambda: NOW,
    )
    result = pipeline.run(
        BriefingType.PRE_MARKET, DAY,
        market_calendar_status="open", market_calendar_reason="weekday",
    )
    text = Path(result.json_path or "").read_text(encoding="utf-8")
    json.loads(text)
    assert all(value in text for value in ("코스피200 선물 최근월물", "외국인", "개인", "기관"))
