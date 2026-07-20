"""Daily briefing execution and durable result storage."""

from .collectors import (
    KiwoomCoreMarketCollector,
    KiwoomStockBasicDataSource,
    PlaceholderCollector,
    normalize_decimal,
    normalize_integer,
    normalize_price,
)
from .models import BriefingRunResult, BriefingType
from .pipeline import DailyBriefingPipeline
from .storage import BriefingStorage

__all__ = [
    "BriefingRunResult",
    "BriefingStorage",
    "BriefingType",
    "DailyBriefingPipeline",
    "KiwoomCoreMarketCollector",
    "KiwoomStockBasicDataSource",
    "PlaceholderCollector",
    "normalize_price",
    "normalize_decimal",
    "normalize_integer",
]
