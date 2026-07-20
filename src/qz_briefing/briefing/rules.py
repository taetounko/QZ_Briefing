# -*- coding: utf-8 -*-
"""Pure, explicit scoring and signal rules for collected briefing data."""

from __future__ import annotations

from typing import Any

def collector_data(result: dict[str, object], name: str) -> dict[str, Any]:
    collectors = result.get("collectors")
    if not isinstance(collectors, dict):
        return {}
    wrapper = collectors.get(name)
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("data"), dict):
        return {}
    return wrapper["data"]


def index_rates(result: dict[str, object]) -> dict[str, float | None]:
    rows = collector_data(result, "kiwoom_market_indices").get("indices", [])
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("market")): number(row.get("change_rate"))
        for row in rows if isinstance(row, dict)
    }


def stock_rates(result: dict[str, object]) -> dict[str, float | None]:
    rows = collector_data(result, "kiwoom_core_market").get("securities", [])
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("code")): number(row.get("change_rate"))
        for row in rows if isinstance(row, dict)
    }


def spot_flows(result: dict[str, object], market: str = "KOSPI") -> dict[str, int | None]:
    rows = collector_data(result, "kiwoom_investor_flows").get("markets", [])
    if not isinstance(rows, list):
        return {}
    selected = next(
        (row for row in rows if isinstance(row, dict) and row.get("market") == market),
        None,
    )
    if not isinstance(selected, dict) or not isinstance(selected.get("investors"), list):
        return {}
    return {
        str(row.get("investor")): integer(row.get("net_buy"))
        for row in selected["investors"] if isinstance(row, dict)
    }


def derivatives_values(result: dict[str, object]) -> dict[str, int | None]:
    data = collector_data(result, "kiwoom_derivatives_flows")
    futures = data.get("kospi200_futures")
    program = data.get("program_trading")
    values: dict[str, int | None] = {}
    if isinstance(futures, dict):
        investors = futures.get("investors")
        if isinstance(investors, dict):
            for key in ("foreign", "individual", "institution"):
                item = investors.get(key)
                values[f"futures_{key}"] = (
                    integer(item.get("net_buy")) if isinstance(item, dict) else None
                )
        values["open_interest"] = integer(futures.get("open_interest"))
    if isinstance(program, dict) and isinstance(program.get("total"), dict):
        values["program_total"] = integer(program["total"].get("net_buy"))
    return values


def score_market(result: dict[str, object]) -> tuple[str, int, str, list[str]]:
    components: list[tuple[str, float | int | None]] = []
    indices = index_rates(result)
    stocks = stock_rates(result)
    spot = spot_flows(result)
    derivatives = derivatives_values(result)
    for key in ("KOSPI", "KOSDAQ", "KOSPI200"):
        components.append((f"{key} 등락률", indices.get(key)))
    components.extend((
        ("삼성전자 등락률", stocks.get("005930")),
        ("SK하이닉스 등락률", stocks.get("000660")),
        ("외국인 현물 순매수", spot.get("foreigner")),
        ("기관 현물 순매수", spot.get("institution")),
        ("프로그램 전체 순매수", derivatives.get("program_total")),
        ("외국인 선물 순매수", derivatives.get("futures_foreign")),
    ))
    available = [(name, value) for name, value in components if value is not None]
    score = sum(direction(value) for _, value in available)
    reasons = [f"{name} {'플러스' if direction(value) > 0 else '마이너스' if direction(value) < 0 else '보합'}" for name, value in available]
    if len(available) < 3:
        return "insufficient_data", score, "low", reasons
    if score >= 6:
        state = "strong_bullish"
    elif score >= 2:
        state = "bullish"
    elif score <= -6:
        state = "strong_bearish"
    elif score <= -2:
        state = "bearish"
    else:
        state = "neutral"
    errors = result.get("errors")
    confidence = "high" if len(available) >= 8 and not errors else "medium"
    if len(available) < 6 or errors:
        confidence = "low" if len(available) < 5 else "medium"
    return state, score, confidence, reasons


def calculate_signals(result: dict[str, object]) -> list[str]:
    signals: list[str] = []
    indices, stocks = index_rates(result), stock_rates(result)
    spot, derivatives = spot_flows(result), derivatives_values(result)
    samsung, hynix = stocks.get("005930"), stocks.get("000660")
    if both_direction(samsung, hynix, 1):
        signals.append("대형주 동반 강세")
    elif both_direction(samsung, hynix, -1):
        signals.append("대형주 동반 약세")
    kospi = indices.get("KOSPI")
    known_stocks = [value for value in (samsung, hynix) if value is not None]
    if kospi is not None and known_stocks and all(direction(v) == direction(kospi) for v in known_stocks):
        signals.append("지수와 대형주 방향 일치")
    foreign_spot, foreign_futures = spot.get("foreigner"), derivatives.get("futures_foreign")
    if both_direction(foreign_spot, foreign_futures, 1):
        signals.append("외국인 현물·선물 동시 순매수")
    elif both_direction(foreign_spot, foreign_futures, -1):
        signals.append("외국인 현물·선물 동시 순매도")
    program = derivatives.get("program_total")
    if foreign_spot is not None and program is not None and direction(foreign_spot) == direction(program):
        signals.append("프로그램 매매와 외국인 수급 방향 일치")
    kosdaq = indices.get("KOSDAQ")
    if kospi is not None and kosdaq is not None and direction(kospi) * direction(kosdaq) == -1:
        signals.append("코스피와 코스닥 방향 차이")
    flow_values = [value for value in (foreign_spot, spot.get("institution"), program) if value is not None]
    flow_score = sum(direction(value) for value in flow_values)
    if kospi is not None and direction(kospi) < 0 and flow_score >= 2:
        signals.append("수급은 강하지만 지수가 약한 다이버전스")
    if kospi is not None and direction(kospi) > 0 and flow_score <= -2:
        signals.append("지수는 강하지만 수급이 약한 다이버전스")
    if len([v for v in (*indices.values(), *stocks.values(), foreign_spot, foreign_futures, program) if v is not None]) < 5:
        signals.append("데이터 부족 경고")
    return signals


def number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def integer(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def direction(value: float | int) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def both_direction(first: object, second: object, expected: int) -> bool:
    return isinstance(first, (int, float)) and isinstance(second, (int, float)) and direction(first) == direction(second) == expected
