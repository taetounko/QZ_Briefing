"""Read-only core security collector tests using fake adapters."""

from datetime import date, datetime
from pathlib import Path

from qz_briefing.briefing import (
    KiwoomCoreMarketCollector,
    BriefingStorage,
    DailyBriefingPipeline,
    normalize_decimal,
    normalize_integer,
    normalize_price,
)
from qz_briefing.briefing.models import BriefingContext, BriefingType


NOW = datetime(2026, 7, 21, 10, 0)


class FakeStockBasicDataSource:
    def __init__(self, *, fail_code: str | None = None) -> None:
        self.fail_code = fail_code
        self.calls: list[str] = []

    def get_stock_basic_info(self, code: str) -> dict[str, str]:
        self.calls.append(code)
        if code == self.fail_code:
            raise RuntimeError("stock basic info unavailable")
        values = {
            "005930": ("삼성전자", "+72,500", "+1,200", "+1.68"),
            "000660": ("SK하이닉스", "-185,000", "-2,500", "-1.33"),
        }
        name, current, change, rate = values[code]
        return {
            "종목코드": code,
            "종목명": name,
            "현재가": current,
            "전일대비": change,
            "등락율": rate,
            "시가": current,
            "고가": current,
            "저가": current,
            "거래량": "1,234,567",
            "기준가": current,
        }


def context() -> BriefingContext:
    return BriefingContext(
        briefing_type=BriefingType.INTRADAY_10AM,
        trading_date=date(2026, 7, 21),
        requested_at=NOW,
        started_at=NOW,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )


def test_normalize_price_removes_sign_and_commas() -> None:
    assert normalize_price("+72,500") == 72_500
    assert normalize_price("-185,000") == 185_000
    assert normalize_price(" 001230 ") == 1_230
    assert normalize_integer("") is None
    assert normalize_decimal("-1.33") == -1.33


def test_collector_returns_samsung_and_sk_hynix_master_data() -> None:
    source = FakeStockBasicDataSource()
    result = KiwoomCoreMarketCollector(source, clock=lambda: NOW).collect(context())
    securities = result["securities"]
    assert [item["code"] for item in securities] == ["005930", "000660"]
    assert [item["name"] for item in securities] == ["삼성전자", "SK하이닉스"]
    assert [item["reference_price"] for item in securities] == [72_500, 185_000]
    assert securities[0]["raw"]["현재가"] == "+72,500"
    assert securities[0]["change"] == 1_200
    assert securities[1]["change_rate"] == -1.33
    assert result["warnings"] == []


def test_one_security_failure_does_not_stop_the_other() -> None:
    source = FakeStockBasicDataSource(fail_code="005930")
    result = KiwoomCoreMarketCollector(source, clock=lambda: NOW).collect(context())
    securities = result["securities"]
    assert securities[0]["reference_price"] is None
    assert securities[0]["warnings"]
    assert securities[1]["name"] == "SK하이닉스"
    assert securities[1]["reference_price"] == 185_000
    assert "000660" in source.calls


def test_one_security_failure_marks_pipeline_completed_with_errors(
    tmp_path: Path,
) -> None:
    collector = KiwoomCoreMarketCollector(
        FakeStockBasicDataSource(fail_code="005930"), clock=lambda: NOW
    )
    pipeline = DailyBriefingPipeline(
        BriefingStorage(tmp_path), [collector], clock=lambda: NOW
    )
    result = pipeline.run(
        BriefingType.INTRADAY_10AM,
        date(2026, 7, 21),
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )
    assert result.status == "completed_with_errors"
