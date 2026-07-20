"""Collector contract and offline placeholder implementation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from .models import BriefingContext


class BriefingCollector(Protocol):
    name: str

    def collect(self, context: BriefingContext) -> object: ...


class KiwoomMasterDataSource(Protocol):
    def get_master_code_name(self, code: str) -> str: ...

    def get_master_last_price(self, code: str) -> str: ...


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


class KiwoomCoreMarketCollector:
    """Collect read-only master data for two core Korean securities."""

    name = "kiwoom_core_market"

    def __init__(
        self,
        data_source: KiwoomMasterDataSource,
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
                "reference_price_raw": None,
                "reference_price": None,
                "collected_at": collected_at,
                "warnings": [],
            }
            item_warnings: list[str] = item["warnings"]  # type: ignore[assignment]
            try:
                item["name"] = self._data_source.get_master_code_name(code)
                raw_price = self._data_source.get_master_last_price(code)
                item["reference_price_raw"] = raw_price
                item["reference_price"] = normalize_price(raw_price)
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
