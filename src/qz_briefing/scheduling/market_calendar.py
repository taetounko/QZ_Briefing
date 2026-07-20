"""Offline, conservative KRX trading-day decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path


DEFAULT_CALENDAR_PATH = (
    Path(__file__).resolve().parents[1] / "calendars" / "krx_holidays.json"
)


@dataclass(frozen=True)
class CalendarYear:
    complete: bool
    holidays: frozenset[date]
    source: str


class MarketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TradingDayResult:
    date: date
    status: MarketStatus
    reason: str
    warning: str | None = None

    @property
    def is_trading_day(self) -> bool | None:
        """Return a boolean only when the calendar decision is conclusive."""
        if self.status is MarketStatus.OPEN:
            return True
        if self.status is MarketStatus.CLOSED:
            return False
        return None


class MarketCalendar:
    """Decide trading days without network access or optimistic assumptions."""

    def __init__(self, years: dict[int, CalendarYear]) -> None:
        self._years = dict(years)

    def evaluate(self, target_date: date) -> TradingDayResult:
        if target_date.weekday() >= 5:
            return TradingDayResult(target_date, MarketStatus.CLOSED, "weekend")

        calendar_year = self._years.get(target_date.year)
        if calendar_year is None:
            return TradingDayResult(
                target_date,
                MarketStatus.UNKNOWN,
                "unknown_calendar",
                f"KRX calendar data is missing for {target_date.year}",
            )

        if target_date in calendar_year.holidays:
            return TradingDayResult(
                target_date, MarketStatus.CLOSED, "market_holiday"
            )

        if not calendar_year.complete:
            return TradingDayResult(
                target_date,
                MarketStatus.UNKNOWN,
                "unknown_calendar",
                f"KRX calendar data is incomplete for {target_date.year}",
            )

        return TradingDayResult(target_date, MarketStatus.OPEN, "weekday")


def load_market_calendar(path: Path = DEFAULT_CALENDAR_PATH) -> MarketCalendar:
    """Load and validate the maintained year-based JSON calendar."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    years: dict[int, CalendarYear] = {}
    for raw_year, raw_entry in raw.get("years", {}).items():
        year = int(raw_year)
        holidays = frozenset(date.fromisoformat(value) for value in raw_entry["holidays"])
        if any(holiday.year != year for holiday in holidays):
            raise ValueError(f"Calendar year {year} contains a date from another year")
        years[year] = CalendarYear(
            complete=bool(raw_entry["complete"]),
            holidays=holidays,
            source=str(raw_entry.get("source", "")),
        )
    return MarketCalendar(years)
