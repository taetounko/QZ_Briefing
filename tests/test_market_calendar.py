"""Offline KRX trading calendar policy tests."""

from datetime import date

from qz_briefing.scheduling.market_calendar import (
    CalendarYear,
    MarketCalendar,
    MarketStatus,
)


def complete_calendar(*holidays: date) -> MarketCalendar:
    return MarketCalendar(
        {
            2026: CalendarYear(
                complete=True,
                holidays=frozenset(holidays),
                source="test fixture",
            )
        }
    )


def test_complete_calendar_weekday_is_trading_day() -> None:
    result = complete_calendar().evaluate(date(2026, 7, 20))
    assert result.date == date(2026, 7, 20)
    assert result.status is MarketStatus.OPEN
    assert result.is_trading_day
    assert result.reason == "weekday"


def test_saturday_is_not_trading_day() -> None:
    result = complete_calendar().evaluate(date(2026, 7, 18))
    assert result.status is MarketStatus.CLOSED
    assert not result.is_trading_day
    assert result.reason == "weekend"


def test_sunday_is_not_trading_day() -> None:
    result = complete_calendar().evaluate(date(2026, 7, 19))
    assert result.status is MarketStatus.CLOSED
    assert not result.is_trading_day
    assert result.reason == "weekend"


def test_registered_market_holiday_is_not_trading_day() -> None:
    holiday = date(2026, 7, 20)
    result = complete_calendar(holiday).evaluate(holiday)
    assert result.status is MarketStatus.CLOSED
    assert not result.is_trading_day
    assert result.reason == "market_holiday"


def test_incomplete_calendar_returns_warning_instead_of_assuming_trading_day() -> None:
    calendar = MarketCalendar(
        {
            2026: CalendarYear(
                complete=False,
                holidays=frozenset(),
                source="incomplete test fixture",
            )
        }
    )
    result = calendar.evaluate(date(2026, 7, 20))
    assert result.status is MarketStatus.UNKNOWN
    assert result.is_trading_day is None
    assert result.reason == "unknown_calendar"
    assert result.warning == "KRX calendar data is incomplete for 2026"


def test_missing_calendar_year_returns_warning() -> None:
    result = MarketCalendar({}).evaluate(date(2027, 1, 4))
    assert result.status is MarketStatus.UNKNOWN
    assert result.is_trading_day is None
    assert result.reason == "unknown_calendar"
    assert result.warning == "KRX calendar data is missing for 2027"
