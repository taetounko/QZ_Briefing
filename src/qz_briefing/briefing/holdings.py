# -*- coding: utf-8 -*-
"""User-managed holdings configuration and read-only position feedback."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .collectors import StockBasicDataSource, normalize_decimal, normalize_integer
from .leadership import LeadershipDataSource, normalize_history, numeric
from .models import BriefingContext, BriefingType
from .technical_indicators import macd_12_26_9, rsi14
from .accounts import (
    KiwoomAccountHoldingsSource, consolidate, mask_account, normalize_account_row,
)

ALLOWED_FIELDS = {
    "code", "name", "quantity", "average_price", "target_price", "stop_price",
    "maximum_additional_budget", "memo",
}


def load_holdings(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"holdings": [], "warnings": ["보유종목 설정 파일이 없어 보유종목 없음으로 처리합니다."], "errors": []}
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"holdings": [], "warnings": [], "errors": [f"보유종목 설정을 읽을 수 없습니다: {type(exc).__name__}: {exc}"]}
    if not isinstance(root, dict) or root.get("schema_version") != 1 or not isinstance(root.get("holdings"), list):
        return {"holdings": [], "warnings": [], "errors": ["보유종목 설정 schema_version 또는 holdings 형식이 잘못되었습니다."]}
    valid, warnings, errors, seen = [], [], [], set()
    for index, raw in enumerate(root["holdings"]):
        if not isinstance(raw, dict):
            errors.append(f"holdings[{index}]는 object가 아닙니다."); continue
        unknown = sorted(set(raw) - ALLOWED_FIELDS)
        if unknown: warnings.append(f"holdings[{index}] 알 수 없는 필드: {', '.join(unknown)}")
        code, quantity, average = raw.get("code"), raw.get("quantity"), raw.get("average_price")
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code): errors.append(f"holdings[{index}] code는 숫자 6자리여야 합니다."); continue
        if code in seen: errors.append(f"중복 종목 코드: {code}"); continue
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0: errors.append(f"{code} quantity는 0보다 큰 정수여야 합니다."); continue
        if not isinstance(average, (int, float)) or isinstance(average, bool) or average <= 0: errors.append(f"{code} average_price는 0보다 큰 숫자여야 합니다."); continue
        seen.add(code); valid.append({field: raw.get(field) for field in ALLOWED_FIELDS})
    return {"holdings": valid, "warnings": warnings, "errors": errors}


def moving_average(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def trend_state(current: float, ma5: float | None, ma20: float | None, ma60: float | None) -> str:
    if ma60 is None or ma20 is None or ma5 is None: return "insufficient_data"
    if current > ma5 > ma20 > ma60: return "strong_uptrend"
    if current > ma20 and ma5 >= ma20: return "uptrend"
    if current < ma5 < ma20 < ma60: return "strong_downtrend"
    if current < ma20 and ma5 <= ma20: return "downtrend"
    return "sideways"


def bottom_state(current: float, lows: list[float], closes: list[float], ma20: float | None) -> str:
    if len(lows) < 20 or len(closes) < 3 or ma20 is None: return "insufficient_data"
    recent_low = min(lows[-20:])
    if current < recent_low: return "failed"
    rebound = (current / recent_low - 1) * 100
    if closes[-1] > closes[-2] > closes[-3] and current >= ma20 and rebound >= 5: return "confirmed"
    if closes[-1] > closes[-2] and rebound >= 3: return "partially_confirmed"
    if rebound <= 5 and closes[-1] > closes[-2]: return "attempting_bottom"
    return "not_confirmed"


def review_state(
    *, profit_rate: float, trend: str, bottom: str, above_ma20: bool,
    volume_multiple: float | None, leadership: bool, stop_breached: bool,
) -> str:
    if stop_breached or bottom == "failed": return "exit_review"
    if trend == "strong_downtrend": return "averaging_down_high_risk" if profit_rate < 0 else "reduce_risk"
    if trend == "downtrend": return "wait"
    if profit_rate < 0 and bottom in {"partially_confirmed", "confirmed"} and above_ma20: return "averaging_down_candidate"
    if profit_rate > 0 and trend in {"uptrend", "strong_uptrend"} and above_ma20 and (volume_multiple or 0) >= 1.2 and leadership: return "add_on_strength_candidate"
    return "no_action"


class HoldingsCollector:
    name = "holdings_analysis"

    def __init__(
        self, config_path: Path, stock_source: StockBasicDataSource,
        daily_source: LeadershipDataSource,
        leadership_codes: Callable[[], set[str]] = set,
        account_source: KiwoomAccountHoldingsSource | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._config_path, self._stock_source, self._daily_source = config_path, stock_source, daily_source
        self._leadership_codes, self._clock = leadership_codes, clock
        self._account_source = account_source

    def collect(self, context: BriefingContext) -> dict[str, object]:
        accounts, warnings, errors = self._load_accounts()
        if accounts and any(account["status"] == "completed" for account in accounts):
            source = "kiwoom_accounts"
            consolidated_result = consolidate(accounts)
            configured_holdings = [
                {
                    "code": item["code"], "name": item["name"],
                    "quantity": int(item["quantity"]),
                    "average_price": item["average_price"],
                    "target_price": None, "stop_price": None,
                    "maximum_additional_budget": None, "memo": "",
                    "account_ids": list(item.get("account_ids", [])),
                    "_account_data": item,
                }
                for item in consolidated_result["holdings"]
            ]
        else:
            config = load_holdings(self._config_path)
            configured_holdings = config["holdings"]
            warnings.extend(config["warnings"]); errors.extend(config["errors"])
            source = "manual_fallback" if configured_holdings else "none"
        items = []
        for holding in configured_holdings:
            try: items.append(self._analyze_one(holding, context))
            except Exception as exc: errors.append(f"{holding['code']}: {type(exc).__name__}: {exc}")
        investment = sum(float(item["investment_amount"]) for item in items)
        valuation = sum(float(item["valuation_amount"]) for item in items)
        profit = sum(float(item["profit_loss"]) for item in items)
        return {
            "collector": self.name, "collected_at": self._clock().isoformat(), "source": source,
            "accounts": accounts,
            "consolidated": consolidated_result if source == "kiwoom_accounts" else {"holding_count": len(items), "total_invested_amount": investment, "total_market_value": valuation, "total_unrealized_profit": valuation - investment, "total_return_rate": ((valuation / investment - 1) * 100 if investment else None), "holdings": items},
            "basis": "latest_close" if context.briefing_type is BriefingType.PRE_MARKET else "current_price",
            "portfolio": {"investment_amount": investment, "valuation_amount": valuation, "profit_loss": profit, "profit_rate": (round(profit / investment * 100, 6) if investment else None)},
            "holdings": items, "warnings": warnings, "errors": errors,
        }

    def _load_accounts(
        self,
    ) -> tuple[list[dict[str, object]], list[str], list[str]]:
        if self._account_source is None:
            return [], [], []
        try:
            account_numbers = self._account_source.accounts()
        except Exception as exc:
            return [], [], [f"계좌 목록 조회 실패: {type(exc).__name__}: {exc}"]
        if not account_numbers:
            return [], ["로그인 계정에서 조회 가능한 계좌가 없습니다."], []
        accounts, warnings, errors = [], [], []
        for account_number in account_numbers:
            account_id = mask_account(account_number)
            try:
                rows = self._account_source.holdings(account_number)
                normalized = [normalize_account_row(row) for row in rows]
                account = {"account_id": account_id, "status": "completed", "holding_count": len(normalized), "summary": account_summary(normalized), "holdings": normalized, "warnings": [], "errors": []}
                accounts.append(account)
            except Exception as exc:
                safe_detail = str(exc).replace(account_number, account_id)
                message = f"{account_id} 잔고 조회 실패: {type(exc).__name__}: {safe_detail}"
                accounts.append({"account_id": account_id, "status": "failed", "holding_count": 0, "summary": {}, "holdings": [], "warnings": [message], "errors": [message]})
                warnings.append(message); errors.append(message)
        if not any(account["status"] == "completed" for account in accounts):
            warnings.append("모든 자동 계좌조회가 실패하여 수동 설정을 사용합니다.")
        return accounts, warnings, errors

    def _analyze_one(self, holding: dict[str, object], context: BriefingContext) -> dict[str, object]:
        code = str(holding["code"]); account_data = holding.get("_account_data")
        raw = account_data.get("raw", {}) if isinstance(account_data, dict) else self._stock_source.get_stock_basic_info(code)
        current = int(account_data["current_price"]) if isinstance(account_data, dict) and account_data.get("current_price") is not None else normalize_integer(raw.get("현재가", ""), absolute=True)
        if current is None: raise ValueError("현재가가 없습니다")
        quantity, average = int(holding["quantity"]), float(holding["average_price"])
        investment, valuation = quantity * average, quantity * current
        item = {key: value for key, value in holding.items() if key != "_account_data"}
        official_investment = account_data.get("invested_amount") if isinstance(account_data, dict) else None
        official_valuation = account_data.get("market_value") if isinstance(account_data, dict) else None
        official_profit = account_data.get("unrealized_profit") if isinstance(account_data, dict) else None
        official_rate = account_data.get("return_rate") if isinstance(account_data, dict) else None
        investment = float(official_investment) if official_investment is not None else investment
        valuation = float(official_valuation) if official_valuation is not None else valuation
        item.update({"name": (account_data.get("name") if isinstance(account_data, dict) else raw.get("종목명", "").strip()) or holding.get("name"), "current_price": current, "investment_amount": investment, "valuation_amount": valuation, "profit_loss": float(official_profit) if official_profit is not None else valuation - investment, "profit_rate": float(official_rate) if official_rate is not None else round((current / average - 1) * 100, 6), "target_distance": distance(current, holding.get("target_price")), "stop_distance": distance(current, holding.get("stop_price")), "valuation_source": "official_account" if isinstance(account_data, dict) else "calculated", "fees_and_taxes_included": False, "raw": {"account_holding": raw} if isinstance(account_data, dict) else {"basic": raw}, "warnings": []})
        try:
            daily = self._daily_source.daily(code, context.trading_date.isoformat())
            history = normalize_history(list(reversed(daily))); item["raw"]["daily_count"] = len(history)
            apply_technical(item, history, code in self._leadership_codes())
        except Exception as exc:
            item["trend"] = item["bottom_confirmation"] = item["review_status"] = "insufficient_data"
            item["warnings"].append(f"일봉 분석 실패: {type(exc).__name__}: {exc}")
        item["next_session_observation"] = next_session_observation(item)
        return item


def apply_technical(item: dict[str, object], history: list[dict[str, object]], leadership: bool) -> None:
    closes = [float(row["close"]) for row in history if numeric(row.get("close")) is not None]
    highs = [float(row["high"]) for row in history if numeric(row.get("high")) is not None]
    lows = [float(row["low"]) for row in history if numeric(row.get("low")) is not None]
    volumes = [float(row["volume"]) for row in history if numeric(row.get("volume")) is not None]
    current = float(item["current_price"]); ma5, ma20, ma60 = (moving_average(closes, p) for p in (5, 20, 60))
    avg_volume = moving_average(volumes[:-1], 20) if len(volumes) > 20 else None
    volume_multiple = volumes[-1] / avg_volume if avg_volume and volumes else None
    trend = trend_state(current, ma5, ma20, ma60); bottom = bottom_state(current, lows, closes, ma20)
    stop = item.get("stop_price"); stop_breached = isinstance(stop, (int, float)) and current <= float(stop)
    item.update({"moving_averages": {"ma5": ma5, "ma20": ma20, "ma60": ma60}, "high_low": {"high20": max(highs[-20:]) if len(highs) >= 20 else None, "low20": min(lows[-20:]) if len(lows) >= 20 else None, "high60": max(highs[-60:]) if len(highs) >= 60 else None, "low60": min(lows[-60:]) if len(lows) >= 60 else None}, "range_position_20": range_position(current, lows[-20:], highs[-20:]), "rsi14": rsi14(closes), "macd": macd_12_26_9(closes), "volume_multiple": volume_multiple, "rebound_from_recent_low": ((current / min(lows[-20:]) - 1) * 100 if len(lows) >= 20 else None), "drawdown_from_recent_high": ((current / max(highs[-20:]) - 1) * 100 if len(highs) >= 20 else None), "trend": trend, "bottom_confirmation": bottom, "review_status": review_state(profit_rate=float(item["profit_rate"]), trend=trend, bottom=bottom, above_ma20=ma20 is not None and current > ma20, volume_multiple=volume_multiple, leadership=leadership, stop_breached=stop_breached)})


def distance(current: float, target: object) -> float | None:
    return round((float(target) / current - 1) * 100, 6) if isinstance(target, (int, float)) and not isinstance(target, bool) else None


def range_position(current: float, lows: list[float], highs: list[float]) -> float | None:
    if not lows or not highs or max(highs) == min(lows): return None
    return (current - min(lows)) / (max(highs) - min(lows)) * 100


def account_summary(holdings: list[dict[str, object]]) -> dict[str, object]:
    invested = sum(float(item.get("invested_amount") or 0) for item in holdings)
    market = sum(float(item.get("market_value") or 0) for item in holdings)
    profit = sum(float(item.get("unrealized_profit") or 0) for item in holdings)
    return {"invested_amount": invested, "market_value": market, "unrealized_profit": profit, "return_rate": profit / invested * 100 if invested else None}


def next_session_observation(item: dict[str, object]) -> str:
    if item.get("trend") == "insufficient_data":
        return "기술 데이터 확보 후 추세와 저점 방어를 재확인합니다."
    if item.get("review_status") in {"exit_review", "reduce_risk", "averaging_down_high_risk"}:
        return "직전 저점 이탈 여부와 거래량 동반 약세를 우선 확인합니다."
    return "20일선 유지와 거래량 동반 추세 개선 여부를 확인합니다."
