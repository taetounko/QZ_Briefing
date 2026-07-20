# -*- coding: utf-8 -*-
"""KOSPI/KOSDAQ OPT20001 collector tests using fake data only."""

import json
from datetime import date, datetime
from pathlib import Path

from qz_briefing.__main__ import create_briefing_pipeline
from qz_briefing.briefing import (
    BriefingStorage,
    BriefingType,
    DailyBriefingPipeline,
    KiwoomMarketIndexCollector,
    KiwoomMarketIndexDataSource,
    KiwoomStockBasicDataSource,
)
from qz_briefing.briefing.models import BriefingContext


NOW = datetime(2026, 7, 21, 10, 0)
TRADING_DATE = date(2026, 7, 21)


class FakeRequestQueue:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def request(self, request: object) -> dict[str, str]:
        self.requests.append(request)
        return {}


class FakeMarketIndexDataSource:
    def __init__(self, *, fail_market_code: str | None = None) -> None:
        self.fail_market_code = fail_market_code
        self.calls: list[tuple[str, str]] = []

    def get_market_index(
        self, market_code: str, industry_code: str
    ) -> dict[str, str]:
        self.calls.append((market_code, industry_code))
        if market_code == self.fail_market_code:
            raise RuntimeError("index unavailable")
        if market_code == "0":
            return {
                "현재가": " -6516.27 ",
                "전일대비": "-304.33",
                "등락률": "-4.46",
                "시가": "-6643.58",
                "고가": "2,870.50",
                "저가": "2,840.10",
                "거래량": "1,234,567",
                "거래대금": "12,345,678",
            }
        return {
            "현재가": "850.25",
            "전일대비": "+5.75",
            "등락률": "+0.68",
            "시가": "",
            "고가": "855.00",
            "저가": "845.50",
            "거래량": "987,654",
            "거래대금": "8,765,432",
        }


def context() -> BriefingContext:
    return BriefingContext(
        briefing_type=BriefingType.INTRADAY_10AM,
        trading_date=TRADING_DATE,
        requested_at=NOW,
        started_at=NOW,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )


def test_opt20001_uses_official_inputs_for_kospi_and_kosdaq_in_order() -> None:
    queue = FakeRequestQueue()
    source = KiwoomMarketIndexDataSource(queue)  # type: ignore[arg-type]
    source.get_market_index("0", "001")
    source.get_market_index("1", "101")
    source.get_market_index("2", "201")
    first, second, third = queue.requests
    assert first.tr_code == second.tr_code == third.tr_code == "OPT20001"  # type: ignore[attr-defined]
    assert first.inputs == {"시장구분": "0", "업종코드": "001"}  # type: ignore[attr-defined]
    assert second.inputs == {"시장구분": "1", "업종코드": "101"}  # type: ignore[attr-defined]
    assert third.inputs == {"시장구분": "2", "업종코드": "201"}  # type: ignore[attr-defined]
    assert first.request_name == "qz_market_index_001"  # type: ignore[attr-defined]
    assert second.request_name == "qz_market_index_101"  # type: ignore[attr-defined]
    assert third.request_name == "qz_market_index_201"  # type: ignore[attr-defined]
    assert first.output_fields == (  # type: ignore[attr-defined]
        "현재가",
        "전일대비",
        "등락률",
        "시가",
        "고가",
        "저가",
        "거래량",
        "거래대금",
    )


def test_index_values_preserve_decimals_signs_and_integer_totals() -> None:
    collector = KiwoomMarketIndexCollector(
        FakeMarketIndexDataSource(), clock=lambda: NOW
    )
    result = collector.collect(context())
    kospi, kosdaq, kospi200 = result["indices"]
    assert kospi["current"] == 6516.27
    assert kospi["open"] == 6643.58
    assert kospi["change"] == -304.33
    assert kospi["change_rate"] == -4.46
    assert kospi["raw"]["현재가"] == " -6516.27 "
    assert kospi["raw"]["시가"] == "-6643.58"
    assert kosdaq["change"] == 5.75
    assert kosdaq["change_rate"] == 0.68
    assert kospi["volume"] == 1_234_567
    assert kospi["trading_value"] == 12_345_678
    assert kosdaq["open"] is None
    assert kospi200["name"] == "코스피200"


def test_kospi_failure_does_not_stop_kosdaq() -> None:
    source = FakeMarketIndexDataSource(fail_market_code="0")
    result = KiwoomMarketIndexCollector(source, clock=lambda: NOW).collect(context())
    kospi, kosdaq, kospi200 = result["indices"]
    assert source.calls == [("0", "001"), ("1", "101"), ("2", "201")]
    assert kospi["current"] is None
    assert kospi["warnings"]
    assert kosdaq["name"] == "코스닥"
    assert kosdaq["current"] == 850.25
    assert kospi200["name"] == "코스피200"
    assert result["errors"]


def test_partial_index_failure_marks_pipeline_completed_with_errors(
    tmp_path: Path,
) -> None:
    collector = KiwoomMarketIndexCollector(
        FakeMarketIndexDataSource(fail_market_code="0"), clock=lambda: NOW
    )
    pipeline = DailyBriefingPipeline(
        BriefingStorage(tmp_path), [collector], clock=lambda: NOW
    )
    result = pipeline.run(
        BriefingType.PRE_MARKET,
        TRADING_DATE,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )
    assert result.status == "completed_with_errors"


def test_korean_market_names_and_fields_are_saved_in_json(tmp_path: Path) -> None:
    collector = KiwoomMarketIndexCollector(
        FakeMarketIndexDataSource(), clock=lambda: NOW
    )
    pipeline = DailyBriefingPipeline(
        BriefingStorage(tmp_path), [collector], clock=lambda: NOW
    )
    result = pipeline.run(
        BriefingType.PRE_MARKET,
        TRADING_DATE,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )
    text = Path(result.json_path or "").read_text(encoding="utf-8")
    saved = json.loads(text)
    indices = saved["collectors"]["kiwoom_market_indices"]["data"]["indices"]
    assert [item["name"] for item in indices] == ["코스피", "코스닥", "코스피200"]
    assert "등락률" in indices[0]["raw"]
    assert "코스피" in text and "코스닥" in text


def test_default_pipeline_registers_collectors_in_order_with_same_queue() -> None:
    queue = FakeRequestQueue()
    pipeline = create_briefing_pipeline(lambda: NOW, queue)  # type: ignore[arg-type]
    collectors = pipeline._collectors  # type: ignore[attr-defined]
    assert [collector.name for collector in collectors] == [
        "kiwoom_core_market",
        "kiwoom_market_indices",
        "kiwoom_investor_flows",
        "kiwoom_derivatives_flows",
    ]
    core_source = collectors[0]._data_source  # type: ignore[attr-defined]
    index_source = collectors[1]._data_source  # type: ignore[attr-defined]
    flow_source = collectors[2]._data_source  # type: ignore[attr-defined]
    derivatives_source = collectors[3]._data_source  # type: ignore[attr-defined]
    assert isinstance(core_source, KiwoomStockBasicDataSource)
    assert isinstance(index_source, KiwoomMarketIndexDataSource)
    assert core_source._tr_queue is queue  # type: ignore[attr-defined]
    assert index_source._tr_queue is queue  # type: ignore[attr-defined]
    assert flow_source._tr_queue is queue  # type: ignore[attr-defined]
    assert derivatives_source._tr_queue is queue  # type: ignore[attr-defined]
