"""Pure chronological technical indicators (oldest observation first)."""

from __future__ import annotations


def rsi14(closes: list[float]) -> float | None:
    if len(closes) < 15:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))][-14:]
    gains = sum(max(change, 0) for change in changes) / 14
    losses = sum(max(-change, 0) for change in changes) / 14
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    seed = sum(values[:period]) / period
    output = [seed]
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        output.append((value - output[-1]) * multiplier + output[-1])
    return output


def macd_12_26_9(closes: list[float]) -> dict[str, float | bool] | None:
    if len(closes) < 34:
        return None
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    aligned_fast = fast[len(fast) - len(slow):]
    line = [fast_value - slow_value for fast_value, slow_value in zip(aligned_fast, slow)]
    signal = ema(line, 9)
    if not signal:
        return None
    aligned_line = line[len(line) - len(signal):]
    histogram = aligned_line[-1] - signal[-1]
    previous_histogram = aligned_line[-2] - signal[-2] if len(signal) > 1 else histogram
    return {
        "macd": aligned_line[-1], "signal": signal[-1], "histogram": histogram,
        "golden_cross": aligned_line[-1] > signal[-1] and aligned_line[-2] <= signal[-2],
        "histogram_rising": histogram > previous_histogram,
    }
