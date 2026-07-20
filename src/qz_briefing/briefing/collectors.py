# -*- coding: utf-8 -*-
"""Collector contract and offline placeholder implementation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from qz_briefing.kiwoom.tr_requests import KiwoomTrRequestQueue, TrRequest

from .models import BriefingContext


class BriefingCollector(Protocol):
    name: str

    def collect(self, context: BriefingContext) -> object: ...


class StockBasicDataSource(Protocol):
    def get_stock_basic_info(self, code: str) -> dict[str, str]: ...


CORE_SECURITIES = (
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
)


def normalize_price(raw_value: str) -> int:
    """Normalize signed/comma-separated Kiwoom price text to a positive integer."""
    compact = str(raw_value).strip().replace(",", "")
    if not compact:
        raise ValueError("Price value is empty")
    return abs(int(compact))


def normalize_integer(raw_value: str, *, absolute: bool = False) -> int | None:
    compact = str(raw_value).strip().replace(",", "")
    if not compact:
        return None
    value = int(compact)
    return abs(value) if absolute else value


def normalize_decimal(raw_value: str, *, absolute: bool = False) -> float | None:
    compact = str(raw_value).strip().replace(",", "")
    if not compact:
        return None
    value = float(compact)
    return abs(value) if absolute else value


STOCK_BASIC_FIELDS = (
    "종목코드",
    "종목명",
    "현재가",
    "전일대비",
    "등락율",
    "시가",
    "고가",
    "저가",
    "거래량",
    "기준가",
)

MARKET_INDEX_FIELDS = (
    "현재가",
    "전일대비",
    "등락률",
    "시가",
    "고가",
    "저가",
    "거래량",
    "거래대금",
)

MARKET_INDEX_TARGETS = (
    ("KOSPI", "0", "001", "코스피"),
    ("KOSDAQ", "1", "101", "코스닥"),
    ("KOSPI200", "2", "201", "코스피200"),
)

INVESTOR_FLOW_FIELDS = (
    "업종코드",
    "업종명",
    "개인순매수",
    "외국인순매수",
    "기관계순매수",
)

INVESTOR_FLOW_TARGETS = (
    ("KOSPI", "0", "001"),
    ("KOSDAQ", "1", "101"),
)

INVESTOR_FIELDS = (
    ("individual", "개인", "개인순매수"),
    ("foreigner", "외국인", "외국인순매수"),
    ("institution", "기관계", "기관계순매수"),
)


class KiwoomStockBasicDataSource:
    """Issue the locally documented OPT10001 read-only request."""

    def __init__(self, tr_queue: KiwoomTrRequestQueue) -> None:
        self._tr_queue = tr_queue

    def get_stock_basic_info(self, code: str) -> dict[str, str]:
        return self._tr_queue.request(
            TrRequest(
                request_name=f"qz_stock_basic_{code}",
                tr_code="OPT10001",
                inputs={"종목코드": code},
                output_fields=STOCK_BASIC_FIELDS,
            )
        )


class MarketIndexDataSource(Protocol):
    def get_market_index(self, market_code: str, industry_code: str) -> dict[str, str]: ...


class KiwoomMarketIndexDataSource:
    """Issue the locally documented OPT20001 read-only request."""

    def __init__(self, tr_queue: KiwoomTrRequestQueue) -> None:
        self._tr_queue = tr_queue

    def get_market_index(
        self, market_code: str, industry_code: str
    ) -> dict[str, str]:
        return self._tr_queue.request(
            TrRequest(
                request_name=f"qz_market_index_{industry_code}",
                tr_code="OPT20001",
                inputs={"시장구분": market_code, "업종코드": industry_code},
                output_fields=MARKET_INDEX_FIELDS,
            )
        )


class InvestorFlowDataSource(Protocol):
    def get_market_investor_flows(
        self, market_code: str, trading_date: str
    ) -> list[dict[str, str]]: ...


class KiwoomInvestorFlowDataSource:
    """Issue the locally documented OPT10051 read-only repeated request."""

    def __init__(self, tr_queue: KiwoomTrRequestQueue) -> None:
        self._tr_queue = tr_queue

    def get_market_investor_flows(
        self, market_code: str, trading_date: str
    ) -> list[dict[str, str]]:
        return self._tr_queue.request_rows(
            TrRequest(
                request_name=f"qz_investor_flows_{market_code}",
                tr_code="OPT10051",
                inputs={
                    "시장구분": market_code,
                    "금액수량구분": "0",
                    "기준일자": trading_date.replace("-", ""),
                    "거래소구분": "",
                },
                output_fields=INVESTOR_FLOW_FIELDS,
                repeat=True,
            )
        )


class KiwoomCoreMarketCollector:
    """Collect read-only master data for two core Korean securities."""

    name = "kiwoom_core_market"

    def __init__(
        self,
        data_source: StockBasicDataSource,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._data_source = data_source
        self._clock = clock

    def collect(self, context: BriefingContext) -> dict[str, object]:
        del context
        collected_at = self._clock().isoformat()
        securities: list[dict[str, object]] = []
        warnings: list[str] = []
        for code, expected_name in CORE_SECURITIES:
            item: dict[str, object] = {
                "code": code,
                "name": None,
                "expected_name": expected_name,
                "current_price": None,
                "change": None,
                "change_rate": None,
                "open": None,
                "high": None,
                "low": None,
                "volume": None,
                "reference_price": None,
                "raw": {},
                "collected_at": collected_at,
                "warnings": [],
            }
            item_warnings: list[str] = item["warnings"]  # type: ignore[assignment]
            try:
                raw = self._data_source.get_stock_basic_info(code)
                item["raw"] = dict(raw)
                item["code"] = raw.get("종목코드", code).strip() or code
                item["name"] = raw.get("종목명", "").strip() or None
                normalizers = {
                    "current_price": (
                        "현재가",
                        lambda value: normalize_integer(value, absolute=True),
                    ),
                    "change": ("전일대비", normalize_integer),
                    "change_rate": ("등락율", normalize_decimal),
                    "open": (
                        "시가",
                        lambda value: normalize_integer(value, absolute=True),
                    ),
                    "high": (
                        "고가",
                        lambda value: normalize_integer(value, absolute=True),
                    ),
                    "low": (
                        "저가",
                        lambda value: normalize_integer(value, absolute=True),
                    ),
                    "volume": (
                        "거래량",
                        lambda value: normalize_integer(value, absolute=True),
                    ),
                    "reference_price": (
                        "기준가",
                        lambda value: normalize_integer(value, absolute=True),
                    ),
                }
                for output_name, (field, normalizer) in normalizers.items():
                    try:
                        item[output_name] = normalizer(raw.get(field, ""))
                    except (TypeError, ValueError) as exc:
                        warning = (
                            f"{code} invalid {field}: {type(exc).__name__}: {exc}"
                        )
                        item_warnings.append(warning)
                        warnings.append(warning)
            except Exception as exc:
                warning = f"{code} collection failed: {type(exc).__name__}: {exc}"
                item_warnings.append(warning)
                warnings.append(warning)
            securities.append(item)
        return {
            "collector": self.name,
            "collected_at": collected_at,
            "securities": securities,
            "warnings": warnings,
            "errors": warnings,
        }


class KiwoomMarketIndexCollector:
    """Collect KOSPI and KOSDAQ index data with per-market isolation."""

    name = "kiwoom_market_indices"

    def __init__(
        self,
        data_source: MarketIndexDataSource,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._data_source = data_source
        self._clock = clock

    def collect(self, context: BriefingContext) -> dict[str, object]:
        del context
        collected_at = self._clock().isoformat()
        indices: list[dict[str, object]] = []
        warnings: list[str] = []
        errors: list[str] = []
        for market, market_code, industry_code, name in MARKET_INDEX_TARGETS:
            item: dict[str, object] = {
                "market": market,
                "code": industry_code,
                "name": name,
                "current": None,
                "change": None,
                "change_rate": None,
                "open": None,
                "high": None,
                "low": None,
                "volume": None,
                "trading_value": None,
                "collected_at": collected_at,
                "raw": {},
                "warnings": [],
            }
            item_warnings: list[str] = item["warnings"]  # type: ignore[assignment]
            try:
                raw = self._data_source.get_market_index(market_code, industry_code)
                item["raw"] = dict(raw)
                normalizers = {
                    "current": (
                        "현재가",
                        lambda value: normalize_decimal(value, absolute=True),
                    ),
                    "change": ("전일대비", normalize_decimal),
                    "change_rate": ("등락률", normalize_decimal),
                    "open": (
                        "시가",
                        lambda value: normalize_decimal(value, absolute=True),
                    ),
                    "high": (
                        "고가",
                        lambda value: normalize_decimal(value, absolute=True),
                    ),
                    "low": (
                        "저가",
                        lambda value: normalize_decimal(value, absolute=True),
                    ),
                    "volume": (
                        "거래량",
                        lambda value: normalize_integer(value, absolute=True),
                    ),
                    "trading_value": (
                        "거래대금",
                        lambda value: normalize_integer(value, absolute=True),
                    ),
                }
                for output_name, (field, normalizer) in normalizers.items():
                    try:
                        item[output_name] = normalizer(raw.get(field, ""))
                    except (TypeError, ValueError) as exc:
                        warning = (
                            f"{market} invalid {field}: {type(exc).__name__}: {exc}"
                        )
                        item_warnings.append(warning)
                        warnings.append(warning)
            except Exception as exc:
                error = f"{market} collection failed: {type(exc).__name__}: {exc}"
                item_warnings.append(error)
                warnings.append(error)
                errors.append(error)
            indices.append(item)
        return {
            "collector": self.name,
            "collected_at": collected_at,
            "indices": indices,
            "warnings": warnings,
            "errors": errors,
        }


class KiwoomInvestorFlowCollector:
    """Collect KOSPI/KOSDAQ investor net amounts with market isolation."""

    name = "kiwoom_investor_flows"

    def __init__(
        self,
        data_source: InvestorFlowDataSource,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._data_source = data_source
        self._clock = clock

    def collect(self, context: BriefingContext) -> dict[str, object]:
        collected_at = self._clock().isoformat()
        markets: list[dict[str, object]] = []
        warnings: list[str] = []
        errors: list[str] = []
        for market, market_code, industry_code in INVESTOR_FLOW_TARGETS:
            market_result: dict[str, object] = {
                "market": market,
                "industry_code": industry_code,
                "collected_at": collected_at,
                "investors": [],
                "raw": {},
                "warnings": [],
            }
            market_warnings: list[str] = market_result["warnings"]  # type: ignore[assignment]
            try:
                rows = self._data_source.get_market_investor_flows(
                    market_code, context.trading_date.isoformat()
                )
                row = next(
                    (item for item in rows if item.get("업종코드", "").strip() == industry_code),
                    None,
                )
                if row is None:
                    raise ValueError(f"official aggregate row {industry_code} is missing")
                market_result["raw"] = dict(row)
                investors: list[dict[str, object]] = []
                for investor_code, investor_name, field in INVESTOR_FIELDS:
                    investor_warnings = [
                        "OPT10051 does not provide separate sell or buy amounts"
                    ]
                    try:
                        net_buy = normalize_integer(row.get(field, ""))
                    except (TypeError, ValueError) as exc:
                        net_buy = None
                        investor_warnings.append(
                            f"invalid {field}: {type(exc).__name__}: {exc}"
                        )
                    investors.append(
                        {
                            "investor": investor_code,
                            "investor_name": investor_name,
                            "sell": None,
                            "buy": None,
                            "net_buy": net_buy,
                            "unit": "amount (official scale unspecified)",
                            "raw": {field: row.get(field, "")},
                            "warnings": investor_warnings,
                        }
                    )
                market_result["investors"] = investors
                market_warnings.append(
                    "OPT10051 local documentation does not specify the amount scale"
                )
                warnings.extend(market_warnings)
            except Exception as exc:
                error = f"{market} investor flow failed: {type(exc).__name__}: {exc}"
                market_warnings.append(error)
                warnings.append(error)
                errors.append(error)
            markets.append(market_result)
        return {
            "collector": self.name,
            "collected_at": collected_at,
            "markets": markets,
            "warnings": warnings,
            "errors": errors,
        }


class PlaceholderCollector:
    """Return a static marker until a real data source is connected."""

    name = "placeholder"

    def __init__(self, clock: Callable[[], datetime] = datetime.now) -> None:
        self._clock = clock

    def collect(self, context: BriefingContext) -> dict[str, str]:
        return {
            "collector": self.name,
            "status": "placeholder",
            "collected_at": self._clock().isoformat(),
            "message": (
                f"No market-data collector is connected for "
                f"{context.briefing_type.value}."
            ),
        }
