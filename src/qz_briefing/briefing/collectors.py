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


def normalize_decimal(raw_value: str) -> float | None:
    compact = str(raw_value).strip().replace(",", "")
    if not compact:
        return None
    return float(compact)


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
