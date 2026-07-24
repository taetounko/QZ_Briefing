from __future__ import annotations

from datetime import datetime

from .models import WeeklyBar, WeeklySignal


def completed_weekly_signal(bars: tuple[WeeklyBar, ...], as_of: datetime) -> WeeklySignal | None:
    """Use only explicitly completed weekly candles available at ``as_of``."""
    completed = sorted(
        (bar for bar in bars if bar.completed and bar.ended_at <= as_of),
        key=lambda bar: bar.ended_at,
    )
    if len(completed) < 5:
        return None
    closes = [bar.close for bar in completed]
    latest = completed[-1]
    ma5 = sum(closes[-5:]) / 5
    prior_ma5 = sum(closes[-6:-1]) / 5 if len(closes) >= 6 else None
    consecutive = 0
    for index in range(len(closes) - 1, 3, -1):
        if closes[index] > sum(closes[index - 4:index + 1]) / 5:
            consecutive += 1
        else:
            break
    distance = (latest.close / ma5 - 1) * 100 if ma5 else 0
    slope = ((ma5 / prior_ma5 - 1) * 100) if prior_ma5 else None
    candle_range = max(latest.high - latest.low, 0)
    upper_wick = max(latest.high - max(latest.open, latest.close), 0)
    return WeeklySignal(
        weekly_close=latest.close, weekly_ma5=ma5,
        weekly_close_above_ma5=latest.close > ma5,
        distance_rate=distance, consecutive_weeks=consecutive,
        ma5_slope_rate=slope, upper_wick_rate=upper_wick / candle_range if candle_range else 0,
        completed_at=latest.ended_at,
    )
