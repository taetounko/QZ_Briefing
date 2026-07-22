"""Serializable briefing domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


SCHEMA_VERSION = 1


class BriefingType(str, Enum):
    PRE_MARKET = "pre_market"
    INTRADAY_10AM = "intraday_10am"
    MARKET_CLOSE = "market_close"


@dataclass
class BriefingContext:
    briefing_type: BriefingType
    trading_date: date
    requested_at: datetime
    started_at: datetime
    market_calendar_status: str
    market_calendar_reason: str
    market_calendar_warning: str | None = None
    completed_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BriefingRunResult:
    status: str
    briefing_type: BriefingType
    trading_date: date
    json_path: str | None = None
    markdown_path: str | None = None
