# -*- coding: utf-8 -*-
"""OPT10051 market investor-flow collector tests using fake data only."""

import json
from datetime import date, datetime
from pathlib import Path

from qz_briefing.briefing import (
    BriefingStorage,
    BriefingType,
    DailyBriefingPipeline,
    KiwoomInvestorFlowCollector,
    KiwoomInvestorFlowDataSource,
)
from qz_briefing.briefing.models import BriefingContext

NOW = datetime(2026, 7, 21, 10, 0)
TRADING_DATE = date(2026, 7, 21)


def context() -> BriefingContext:
    return BriefingContext(
        briefing_type=BriefingType.INTRADAY_10AM,
        trading_date=TRADING_DATE,
        requested_at=NOW,
        started_at=NOW,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )


class FakeQueue:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def request_rows(self, request: object) -> list[dict[str, str]]:
        self.requests.append(request)
        return []


class FakeFlowSource:
    def __init__(self, fail_market: str | None = None) -> None:
        self.fail_market = fail_market
        self.calls: list[tuple[str, str]] = []

    def get_market_investor_flows(
        self, market_code: str, trading_date: str
    ) -> list[dict[str, str]]:
        self.calls.append((market_code, trading_date))
        if market_code == self.fail_market:
            raise RuntimeError("flow unavailable")
        industry = "001" if market_code == "0" else "101"
        name = "코스피" if market_code == "0" else "코스닥"
        return [{
            "업종코드": industry,
            "업종명": name,
            "개인순매수": " -1,234 ",
            "외국인순매수": "+2,345",
            "기관계순매수": "",
        }]


def test_opt10051_uses_official_market_amount_date_inputs() -> None:
    queue = FakeQueue()
    source = KiwoomInvestorFlowDataSource(queue)  # type: ignore[arg-type]
    source.get_market_investor_flows("0", "2026-07-21")
    source.get_market_investor_flows("1", "2026-07-21")
    first, second = queue.requests
    assert first.tr_code == second.tr_code == "OPT10051"  # type: ignore[attr-defined]
    assert first.inputs == {  # type: ignore[attr-defined]
        "시장구분": "0",
        "금액수량구분": "0",
        "기준일자": "20260721",
        "거래소구분": "",
    }
    assert second.inputs["시장구분"] == "1"  # type: ignore[attr-defined]
    assert first.repeat is True  # type: ignore[attr-defined]
    assert first.output_fields == (  # type: ignore[attr-defined]
        "업종코드", "업종명", "개인순매수", "외국인순매수", "기관계순매수"
    )


def test_kospi_and_kosdaq_investors_are_separate_and_normalized() -> None:
    result = KiwoomInvestorFlowCollector(
        FakeFlowSource(), clock=lambda: NOW
    ).collect(context())
    kospi, kosdaq = result["markets"]
    assert [item["investor_name"] for item in kospi["investors"]] == [
        "개인", "외국인", "기관계"
    ]
    assert kospi["investors"][0]["net_buy"] == -1234
    assert kospi["investors"][1]["net_buy"] == 2345
    assert kospi["investors"][2]["net_buy"] is None
    assert kospi["investors"][0]["sell"] is None
    assert kospi["investors"][0]["buy"] is None
    assert kospi["raw"]["개인순매수"] == " -1,234 "
    assert kosdaq["market"] == "KOSDAQ"


def test_kospi_failure_does_not_stop_kosdaq_and_marks_partial_error(
    tmp_path: Path,
) -> None:
    source = FakeFlowSource(fail_market="0")
    collector = KiwoomInvestorFlowCollector(source, clock=lambda: NOW)
    collected = collector.collect(context())
    assert source.calls == [("0", "2026-07-21"), ("1", "2026-07-21")]
    assert collected["markets"][0]["warnings"]
    assert collected["markets"][1]["investors"]
    assert collected["errors"]

    result = DailyBriefingPipeline(
        BriefingStorage(tmp_path), [collector], clock=lambda: NOW
    ).run(
        BriefingType.PRE_MARKET,
        TRADING_DATE,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )
    assert result.status == "completed_with_errors"


def test_korean_investor_names_are_saved_as_utf8_json(tmp_path: Path) -> None:
    pipeline = DailyBriefingPipeline(
        BriefingStorage(tmp_path),
        [KiwoomInvestorFlowCollector(FakeFlowSource(), clock=lambda: NOW)],
        clock=lambda: NOW,
    )
    result = pipeline.run(
        BriefingType.PRE_MARKET,
        TRADING_DATE,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )
    text = Path(result.json_path or "").read_text(encoding="utf-8")
    saved = json.loads(text)
    investors = saved["collectors"]["kiwoom_investor_flows"]["data"]["markets"][0]["investors"]
    assert [item["investor_name"] for item in investors] == ["개인", "외국인", "기관계"]
    assert all(name in text for name in ("코스피", "코스닥", "개인", "외국인", "기관계"))
