"""Pure normalization, weekly aggregation, indicators, and engine conversion."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from math import sqrt

from qz_briefing.briefing.technical_indicators import macd_12_26_9, rsi14

from .data_models import (
    AggregatedWeeklyBar, CatalystRecord, DailyBar, DataMetadata, PriceFeatures,
    RecommendationDataBundle, RiskEvent, StockMasterRecord,
)
from .models import CatalystEvidence, RecommendationFeatures, RiskFlag, StockUniverseItem, WeeklyBar


EXCLUDED_SECURITY_TYPES = {"etf", "etn", "reit", "spac", "preferred"}
UNTRADABLE_STATUSES = {"trading_halt", "liquidation", "delisting"}


def universe_decision(master: StockMasterRecord) -> tuple[bool, str | None]:
    if master.metadata.market not in {"KOSPI", "KOSDAQ"}: return False, "시장 정보 누락 또는 비대상"
    if len(master.metadata.code) != 6 or not master.metadata.code.isdigit(): return False, "비정상 종목 코드"
    if master.security_type != "common_stock": return False, f"제외 종목 유형: {master.security_type}"
    if master.security_type in EXCLUDED_SECURITY_TYPES: return False, f"제외 종목 유형: {master.security_type}"
    if not master.tradable or master.trading_status in UNTRADABLE_STATUSES: return False, f"거래 불가능: {master.trading_status}"
    return True, None


def normalize_daily_bars(bars: list[DailyBar], as_of: datetime) -> tuple[tuple[DailyBar, ...], tuple[str, ...]]:
    errors: list[str] = []
    accepted: dict[date, DailyBar] = {}
    adjustment: bool | None = None
    for bar in sorted(bars, key=lambda value: (value.trading_date, value.metadata.updated_at)):
        if bar.trading_date > as_of.date():
            errors.append(f"기준 시각 이후 데이터 제외: {bar.trading_date}"); continue
        if adjustment is None: adjustment = bar.adjusted
        elif adjustment != bar.adjusted: raise ValueError("수정주가 계열과 비수정주가 계열 혼합")
        if min(bar.open, bar.high, bar.low, bar.close, bar.volume) < 0:
            errors.append(f"음수 가격 또는 거래량: {bar.trading_date}"); continue
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close) or bar.low > bar.high:
            errors.append(f"잘못된 OHLC 관계: {bar.trading_date}"); continue
        if bar.trading_date in accepted: errors.append(f"중복 거래일 대체: {bar.trading_date}")
        accepted[bar.trading_date] = bar
    return tuple(accepted[key] for key in sorted(accepted)), tuple(errors)


def aggregate_weekly_bars(
    bars: tuple[DailyBar, ...], as_of: datetime,
    *, week_last_trading_days: dict[tuple[int, int], date] | None = None,
    market_close: time = time(15, 40),
) -> tuple[AggregatedWeeklyBar, ...]:
    groups: dict[tuple[int, int], list[DailyBar]] = defaultdict(list)
    for bar in bars:
        iso = bar.trading_date.isocalendar(); groups[(iso.year, iso.week)].append(bar)
    keys = sorted(groups)
    output = []
    for index, key in enumerate(keys):
        rows = sorted(groups[key], key=lambda row: row.trading_date)
        expected = (week_last_trading_days or {}).get(key)
        complete = index < len(keys) - 1 or (expected is not None and rows[-1].trading_date == expected and (as_of.date() > expected or as_of.time() >= market_close))
        values = [row.trading_value for row in rows]
        meta = rows[-1].metadata
        output.append(AggregatedWeeklyBar(
            DataMetadata(meta.code, meta.name, meta.market, as_of, "derived:daily_ohlcv", as_of, complete, False, min(row.metadata.confidence for row in rows)),
            rows[0].trading_date, rows[-1].trading_date, rows[0].open,
            max(row.high for row in rows), min(row.low for row in rows), rows[-1].close,
            sum(row.volume for row in rows), sum(value for value in values if value is not None) if all(value is not None for value in values) else None,
        ))
    return tuple(output)


def _sma(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def _atr(bars: tuple[DailyBar, ...], period: int = 14) -> float | None:
    if len(bars) < period + 1: return None
    ranges = [max(bars[i].high-bars[i].low, abs(bars[i].high-bars[i-1].close), abs(bars[i].low-bars[i-1].close)) for i in range(1, len(bars))]
    return sum(ranges[-period:]) / period


def weekly_ma5_metrics(bars: tuple[AggregatedWeeklyBar, ...]) -> dict[str, float | int | bool] | None:
    completed=[bar for bar in bars if bar.metadata.complete]
    if len(completed)<5: return None
    closes=[bar.close for bar in completed]; ma5=sum(closes[-5:])/5
    prior=sum(closes[-6:-1])/5 if len(closes)>=6 else None; consecutive=0
    for index in range(len(closes)-1,3,-1):
        if closes[index]>sum(closes[index-4:index+1])/5: consecutive+=1
        else: break
    return {"weekly_close":closes[-1],"weekly_ma5":ma5,"weekly_close_above_ma5":closes[-1]>ma5,"distance_rate":closes[-1]/ma5-1 if ma5 else 0,"consecutive_weeks":consecutive,"ma5_slope":ma5/prior-1 if prior else 0,"completed":True}


def compute_flow_features(values: tuple[float, ...]) -> dict[str, float | int]:
    if not values: return {}
    consecutive=0; direction=1 if values[-1]>0 else -1 if values[-1]<0 else 0
    for value in reversed(values):
        if direction and value*direction>0: consecutive+=1
        else: break
    return {"latest":values[-1],"sum5":sum(values[-5:]),"sum20":sum(values[-20:]),"consecutive_net_buy_days":consecutive if direction>0 else 0,"consecutive_net_sell_days":consecutive if direction<0 else 0}


def compute_price_features(bars: tuple[DailyBar, ...], as_of: datetime) -> PriceFeatures:
    closes=[bar.close for bar in bars]; highs=[bar.high for bar in bars]; lows=[bar.low for bar in bars]; volumes=[bar.volume for bar in bars]
    values: dict[str, float | bool]={}; missing=[]
    for period in (5,20,60):
        value=_sma(closes,period)
        if value is None: missing.append(f"ma{period}")
        else: values[f"ma{period}"]=value
    if closes:
        lookback=min(260,len(closes)); high52=max(highs[-lookback:]); low52=min(lows[-lookback:]); current=closes[-1]
        values.update({"high52":high52,"low52":low52,"drawdown52":current/high52-1 if high52 else 0,"position52":(current-low52)/(high52-low52) if high52>low52 else 0})
        for period in (5,20):
            if len(closes)>period: values[f"return{period}"]=current/closes[-period-1]-1
            else: missing.append(f"return{period}")
        values["close_to_high"]=(current-bars[-1].low)/(bars[-1].high-bars[-1].low) if bars[-1].high>bars[-1].low else 0
        values["gap_rate"]=bars[-1].open/bars[-2].close-1 if len(bars)>1 and bars[-2].close else 0
        values["upper_wick_rate"]=(bars[-1].high-max(bars[-1].open,current))/(bars[-1].high-bars[-1].low) if bars[-1].high>bars[-1].low else 0
        values["recent_low_rising"]=len(lows)>=20 and min(lows[-10:])>min(lows[-20:-10])
        values["double_bottom_candidate"]=len(lows)>=30 and abs(min(lows[-10:])/min(lows[-30:-10])-1)<=.03
        values["recovered_ma20"]="ma20" in values and len(closes)>1 and current>float(values["ma20"])
        values["recovered_ma60"]="ma60" in values and current>float(values["ma60"])
        recent_high=max(highs[-20:-1]) if len(highs)>1 else highs[-1]; values["breakout_distance"]=current/recent_high-1 if recent_high else 0
        returns=[closes[i]/closes[i-1]-1 for i in range(1,len(closes)) if closes[i-1]]
        if len(returns)>=20:
            recent=sqrt(sum(v*v for v in returns[-5:])/5); prior=sqrt(sum(v*v for v in returns[-20:-5])/15)
            values["volatility_contraction"]=recent<prior*.75; values["volatility_reexpansion"]=recent>prior*1.25
            values["overheat"]=max(0.0,(current/closes[-6]-1)-.15) if len(closes)>=6 else 0
        else: missing.append("volatility")
    rsi=rsi14(closes); macd=macd_12_26_9(closes); atr=_atr(bars)
    if rsi is None: missing.append("rsi14")
    else: values["rsi14"]=rsi
    if macd is None: missing.append("macd")
    else: values.update({f"macd_{key}":value for key,value in macd.items()})
    if atr is None: missing.append("atr14")
    else: values["atr14"]=atr
    for period in (5,20):
        if len(volumes)>=period and sum(volumes[-period:]): values[f"volume_avg{period}"]=sum(volumes[-period:])/period
    if len(volumes)>=20 and values.get("volume_avg20"): values["volume_surge"]=volumes[-1]/float(values["volume_avg20"])
    trading=[bar.trading_value for bar in bars]
    if len(trading)>=20 and all(value is not None for value in trading[-20:]):
        tv=[float(value) for value in trading if value is not None]; values["trading_value_avg5"]=sum(tv[-5:])/5; values["trading_value_avg20"]=sum(tv[-20:])/20; values["trading_value_surge"]=tv[-1]/values["trading_value_avg20"] if values["trading_value_avg20"] else 0
    else: missing.append("trading_value")
    if bars:
        obv=0.0
        for index in range(1,len(bars)):
            obv += bars[index].volume if bars[index].close>bars[index-1].close else -bars[index].volume if bars[index].close<bars[index-1].close else 0
        values["obv"]=obv
    if len(bars)>=20:
        money_flow=[]
        for bar in bars[-20:]:
            spread=bar.high-bar.low
            multiplier=((bar.close-bar.low)-(bar.high-bar.close))/spread if spread else 0
            money_flow.append(multiplier*bar.volume)
        denominator=sum(bar.volume for bar in bars[-20:]); values["cmf20"]=sum(money_flow)/denominator if denominator else 0
    confidence=max(.2,1-len(set(missing))*.05)
    return PriceFeatures(values,tuple(sorted(set(missing))),confidence)


def to_recommendation_features(bundle: RecommendationDataBundle) -> RecommendationFeatures:
    master=bundle.master; values=bundle.price_features.values; missing=list(bundle.price_features.missing)
    weekly=tuple(WeeklyBar(datetime.combine(row.week_end,time(15,40)),row.open,row.high,row.low,row.close,row.metadata.complete) for row in bundle.weekly_bars)
    completed=[row for row in bundle.weekly_bars if row.metadata.complete]
    if len(completed)<5: missing.append("완성 주봉 5개")
    bottom=None
    if "position52" in values: bottom=max(0.0,min(1.0,1-float(values["position52"])))
    daily=None
    if all(key in values for key in ("ma5","ma20","ma60")): daily=sum(bool(values.get(key)) for key in ("recovered_ma20","recovered_ma60"))/2
    flow=None
    if bundle.investor_flow:
        series=bundle.investor_flow.foreign_daily+bundle.investor_flow.institution_daily
        flow=max(0.0,min(1.0,.5+(sum(series)/(abs(sum(series))+1))*.5)) if series else None
    if flow is None: missing.append("종목별 투자자 수급")
    catalysts=[item for item in bundle.catalysts if item.verified and item.metadata.source and item.announced_at and item.announced_at<=master.metadata.as_of]
    fundamentals=[item for item in bundle.fundamentals if item.metadata.source and item.metadata.updated_at<=master.metadata.as_of]
    evidence=tuple(CatalystEvidence(item.summary,item.metadata.source,item.announced_at) for item in catalysts if item.announced_at)+tuple(CatalystEvidence(f"실적 {item.quarter}",item.metadata.source,item.metadata.updated_at) for item in fundamentals)
    catalyst_strength=max([item.metadata.confidence for item in catalysts]+[item.metadata.confidence for item in fundamentals],default=None)
    liquidity=None
    if "trading_value_surge" in values: liquidity=max(0.0,min(1.0,float(values["trading_value_surge"])/2))
    master_risk_deductions={"managed":5.0,"investment_caution":3.0,"investment_warning":8.0,"investment_risk":12.0,"ventilation":5.0}
    risks=tuple(RiskFlag(item.risk_type,item.display,item.deduction) for item in bundle.risks)+tuple(RiskFlag(label,f"마스터 위험 상태: {label}",master_risk_deductions[label]) for label in master.risk_labels if label in master_risk_deductions)
    hard=any(item.hard_exclusion for item in bundle.risks)
    price_sufficient=len(bundle.daily_bars)>=120
    if not price_sufficient: missing.append("일봉 120거래일")
    confidence=min(master.metadata.confidence,bundle.price_features.confidence,bundle.investor_flow.metadata.confidence if bundle.investor_flow else .7)
    return RecommendationFeatures(
        StockUniverseItem(master.metadata.market,master.metadata.code,master.metadata.name,master.security_type,master.tradable and not hard and price_sufficient,"거래 불가능 위험" if hard else "가격 데이터 120거래일 미만" if not price_sufficient else None),
        master.metadata.as_of,weekly,bottom,flow,daily,catalyst_strength,liquidity,confidence,
        bundle.daily_bars[-1].trading_value if bundle.daily_bars else None,
        sum(bundle.investor_flow.foreign_daily[-5:]) if bundle.investor_flow else None,
        sum(bundle.investor_flow.institution_daily[-5:]) if bundle.investor_flow else None,
        sum(bundle.program_flow.daily_net_buy[-5:]) if bundle.program_flow else None,
        evidence,risks,tuple(sorted(set(missing))),
    )
