# -*- coding: utf-8 -*-
r"""Market leadership collection and screening.

Official local sources: C:\OpenAPI\data\opt10027.enc (등락률 순위),
opt10030.enc (거래량/거래대금 순위), opt10032.enc (거래대금 순위),
opt10081.enc (수정주가 일봉), and C:\OpenAPI\koatrinputlegend.ini.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from qz_briefing.kiwoom.tr_requests import KiwoomTrRequestQueue, TrRequest

from .collectors import normalize_decimal, normalize_integer
from .models import BriefingContext
from .technical_indicators import macd_12_26_9, rsi14

RANK_FIELDS = {
    "OPT10027": ("종목코드", "종목명", "현재가", "등락률", "현재거래량"),
    "OPT10030": ("종목코드", "종목명", "현재가", "등락률", "거래량", "거래금액"),
    "OPT10032": ("종목코드", "종목명", "현재가", "등락률", "현재거래량", "거래대금", "현재순위"),
}
DAILY_FIELDS = ("종목코드", "현재가", "거래량", "거래대금", "일자", "시가", "고가", "저가", "전일종가")
MARKETS = (("KOSPI", "001"), ("KOSDAQ", "101"))


class LeadershipDataSource(Protocol):
    def ranking(self, tr_code: str, market_code: str) -> list[dict[str, str]]: ...
    def daily(self, code: str, target_date: str) -> list[dict[str, str]]: ...


class KiwoomLeadershipDataSource:
    def __init__(self, queue: KiwoomTrRequestQueue) -> None:
        self._tr_queue = queue

    def ranking(self, tr_code: str, market_code: str) -> list[dict[str, str]]:
        requests = {
            "OPT10027": {"시장구분": market_code, "정렬구분": "1", "거래량조건": "0", "종목조건": "16", "신용조건": "0", "상하한포함": "0", "가격조건": "0", "거래대금조건": "0", "거래소구분": ""},
            "OPT10030": {"시장구분": market_code, "정렬구분": "1", "관리종목포함": "16", "신용구분": "0", "거래량구분": "0", "가격구분": "0", "거래대금구분": "0", "장운영구분": "1", "거래소구분": ""},
            "OPT10032": {"시장구분": market_code, "관리종목포함": "0", "거래소구분": ""},
        }
        return self._tr_queue.request_rows(TrRequest(
            request_name=f"qz_leadership_{tr_code}_{market_code}", tr_code=tr_code,
            inputs=requests[tr_code], output_fields=RANK_FIELDS[tr_code], repeat=True,
        ))

    def daily(self, code: str, target_date: str) -> list[dict[str, str]]:
        return self._tr_queue.request_rows(TrRequest(
            request_name=f"qz_leadership_daily_{code}", tr_code="OPT10081",
            inputs={"종목코드": code, "기준일자": target_date.replace("-", ""), "수정주가구분": "1"},
            output_fields=DAILY_FIELDS, repeat=True,
        ))


def merge_rankings(groups: list[list[dict[str, str]]]) -> dict[str, dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for source_rank, rows in enumerate(groups):
        for rank, raw in enumerate(rows, 1):
            code = raw.get("종목코드", "").strip()
            if not code:
                continue
            item = merged.setdefault(code, {"code": code, "raw": {}, "source_ranks": {}})
            item["raw"].update(raw)  # type: ignore[union-attr]
            item["source_ranks"][source_rank] = rank  # type: ignore[index]
    return merged


def score_leader(item: dict[str, object], market_rate: float | None) -> dict[str, object]:
    rate = numeric(item.get("change_rate")); current = numeric(item.get("current_price"))
    high = numeric(item.get("high")); open_price = numeric(item.get("open"))
    value_rank = item.get("trading_value_rank")
    score, reasons, warnings = 0.0, [], []
    if rate is not None:
        points = min(max(rate, -5), 15) * 0.3; score += points; reasons.append(f"등락률 {rate:+.2f}%")
    else: warnings.append("등락률 누락")
    if isinstance(value_rank, int):
        points = max(0, 4 - (value_rank - 1) * 0.15); score += points; reasons.append(f"거래대금 {value_rank}위")
    else: warnings.append("거래대금 순위 누락")
    if current and open_price:
        position = (current / open_price - 1) * 100; score += max(-2, min(2, position * 0.3)); reasons.append(f"시가 대비 {position:+.2f}%")
    if current and high:
        drawdown = (current / high - 1) * 100
        if drawdown < -5: score -= 2; warnings.append("고가 대비 큰 폭 이탈")
        else: score += 1; reasons.append("고가 부근 유지")
    relative = rate - market_rate if rate is not None and market_rate is not None else None
    if relative is not None: score += max(-2, min(2, relative * 0.25)); reasons.append(f"시장 대비 {relative:+.2f}%p")
    score -= len(warnings) * 0.5
    return {**item, "score": round(score, 2), "relative_strength": relative, "confidence": "high" if not warnings else "medium" if len(warnings) == 1 else "low", "reasons": reasons, "warnings": warnings}


def score_rebound(item: dict[str, object], market_rate: float | None) -> dict[str, object] | None:
    history = item.get("history")
    if not isinstance(history, list) or len(history) < 15:
        return None
    closes = [numeric(row.get("close")) for row in history if isinstance(row, dict)]
    lows = [numeric(row.get("low")) for row in history if isinstance(row, dict)]
    volumes = [numeric(row.get("volume")) for row in history if isinstance(row, dict)]
    if any(value is None for value in closes + lows + volumes): return None
    close_values = [float(v) for v in closes]; low_values = [float(v) for v in lows]; volume_values = [float(v) for v in volumes]
    current, prior = close_values[-1], close_values[-2]
    if current <= prior or current <= 0: return None
    low_position = (current / min(low_values[-20:]) - 1) * 100
    if low_position > 15: return None
    score, reasons = 3.0, ["전일 대비 상승 전환", f"최근 저가 대비 {low_position:.2f}%"]
    average_volume = sum(volume_values[-15:-1]) / 14
    if average_volume and volume_values[-1] > average_volume * 1.3: score += 2; reasons.append("거래량 증가")
    rsi = rsi14(close_values); macd = macd_12_26_9(close_values)
    if rsi is not None and 30 <= rsi <= 55: score += 1.5; reasons.append(f"RSI14 {rsi:.1f}")
    if macd and (macd["golden_cross"] or macd["histogram_rising"]): score += 1.5; reasons.append("MACD 회복 신호")
    rate = numeric(item.get("change_rate"))
    if rate is not None and market_rate is not None and rate > market_rate: score += 1; reasons.append("시장 대비 상대강도")
    if rate is not None and rate > 15: score -= 3; reasons.append("단기 추격 위험")
    return {**item, "score": round(score, 2), "reasons": reasons, "rsi14": rsi, "macd": macd, "confidence": "high" if len(history) >= 34 else "medium", "warnings": []}


class KiwoomLeadershipCollector:
    name = "kiwoom_market_leadership"

    def __init__(self, source: LeadershipDataSource, clock: Callable[[], datetime] = datetime.now) -> None:
        self._data_source, self._clock = source, clock

    def collect(self, context: BriefingContext) -> dict[str, object]:
        markets: dict[str, list[dict[str, object]]] = {}; rebounds: list[dict[str, object]] = []; errors = []
        warnings = [
            "ETF·ETN은 공식 종목조건 16으로 제외합니다. 관리종목·우선주·스팩 등은 모든 후보 TR에서 동시에 검증할 수 없어 추가 확인이 필요합니다."
        ]
        for market, market_code in MARKETS:
            groups = []
            for tr_code in ("OPT10027", "OPT10030", "OPT10032"):
                try: groups.append(self._data_source.ranking(tr_code, market_code))
                except Exception as exc: errors.append(f"{market} {tr_code}: {type(exc).__name__}: {exc}"); groups.append([])
            candidates = merge_rankings(groups)
            scored = []
            for code, candidate in list(candidates.items())[:30]:
                raw = candidate["raw"]
                try:
                    candidate.update(normalize_candidate(raw, market))  # type: ignore[arg-type]
                except (TypeError, ValueError) as exc:
                    warnings.append(
                        f"{market} {code} 비정상 후보 데이터 제외: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                ranks = candidate["source_ranks"]; candidate["trading_value_rank"] = ranks.get(2)  # type: ignore[union-attr]
                try:
                    daily = self._data_source.daily(code, context.trading_date.isoformat())
                    candidate["history"] = normalize_history(list(reversed(daily)))
                    if candidate["history"]:
                        latest = candidate["history"][-1]
                        candidate.update({key: latest.get(key) for key in ("open", "high", "low")})
                except Exception as exc: candidate["history"] = []; candidate.setdefault("warnings", []).append(f"일봉 실패: {type(exc).__name__}: {exc}")
                leader = score_leader(candidate, None)
                leader.pop("history", None)
                if leader["score"] >= 3 and numeric(leader.get("change_rate")) is not None and numeric(leader.get("change_rate")) > 0: scored.append(leader)
                rebound = score_rebound(candidate, None)
                if rebound:
                    rebound.pop("history", None)
                    if rebound["score"] >= 5:
                        rebounds.append(rebound)
            scored.sort(key=lambda row: (-float(row["score"]), int(row.get("trading_value_rank") or 9999), str(row["code"])))
            for rank, row in enumerate(scored[:10], 1): row["rank"] = rank
            markets[market] = scored[:10]
            if len(markets[market]) < 10: warnings.append(f"{market} 선정 기준 통과 종목이 10개 미만입니다.")
        rebounds.sort(key=lambda row: (-float(row["score"]), str(row["code"])))
        unique = []; seen = set()
        for row in rebounds:
            if row["code"] not in seen: seen.add(row["code"]); unique.append(row)
        return {"collector": self.name, "collected_at": self._clock().isoformat(), "kospi": markets.get("KOSPI", []), "kosdaq": markets.get("KOSDAQ", []), "rebound_candidates": unique[:10], "warnings": warnings, "errors": errors}


def normalize_candidate(raw: dict[str, str], market: str) -> dict[str, object]:
    return {"market": market, "name": raw.get("종목명", "").strip(), "current_price": normalize_integer(raw.get("현재가", ""), absolute=True), "change_rate": normalize_decimal(raw.get("등락률", "")), "volume": normalize_integer(raw.get("현재거래량", raw.get("거래량", "")), absolute=True), "trading_value": normalize_integer(raw.get("거래대금", raw.get("거래금액", "")), absolute=True), "open": None, "high": None, "low": None}


def normalize_history(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    output = []
    for row in rows:
        output.append({"date": row.get("일자", ""), "close": normalize_integer(row.get("현재가", ""), absolute=True), "open": normalize_integer(row.get("시가", ""), absolute=True), "high": normalize_integer(row.get("고가", ""), absolute=True), "low": normalize_integer(row.get("저가", ""), absolute=True), "volume": normalize_integer(row.get("거래량", ""), absolute=True)})
    return output


def numeric(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
