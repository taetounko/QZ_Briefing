"""Pure, deterministic preliminary recommendation scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .data_models import DailyBar, RecommendationDataBundle
from .data_pipeline import to_recommendation_features, universe_decision
from .features import completed_weekly_signal
from .models import PreliminaryRecommendationScore


@dataclass(frozen=True)
class IntegratedScoringConfig:
    weekly_sweet_distance: float = 3.0
    weekly_extended_distance: float = 12.0
    daily_overheat_5d: float = 0.15
    daily_overheat_20d: float = 0.30
    extreme_low_trading_value: float = 100.0
    liquid_trading_value: float = 2_000.0
    high_trading_value: float = 10_000.0
    liquidity_growth: float = 1.25
    minimum_daily_bars: int = 120


DEFAULT_CONFIG = IntegratedScoringConfig()


def _clip(value: float, maximum: float) -> float:
    return round(max(0.0, min(maximum, value)), 1)


def _slope(closes: list[float], period: int) -> float:
    if len(closes) < period + 5:
        return 0.0
    current = sum(closes[-period:]) / period
    prior = sum(closes[-period - 5:-5]) / period
    return current / prior - 1 if prior else 0.0


def _weekly_score(bundle: RecommendationDataBundle, signal, config: IntegratedScoringConfig) -> tuple[float, list[str]]:
    completed = [bar for bar in bundle.weekly_bars if bar.metadata.complete and bar.week_end <= signal.completed_at.date()]
    distance = signal.distance_rate
    score = 5.0
    reasons = ["마지막 완성 주봉 종가가 5주선 위"]
    if distance <= config.weekly_sweet_distance:
        score += 6
        reasons.append("5주선 돌파 후 근접 안착")
    elif distance <= 8:
        score += 4
    elif distance <= config.weekly_extended_distance:
        score += 2
    else:
        score -= min(5, (distance - config.weekly_extended_distance) / 3)
        reasons.append("5주선 과도 이격")
    if signal.ma5_slope_rate is not None:
        score += 4 if signal.ma5_slope_rate > 0.5 else 2 if signal.ma5_slope_rate > 0 else 0
    if len(completed) >= 3:
        last = completed[-3:]
        if last[-1].low >= last[-2].low >= last[-3].low:
            score += 2
        if last[-1].high >= last[-2].high:
            score += 1
    if 1 <= signal.consecutive_weeks <= 3:
        score += 2
    elif signal.consecutive_weeks >= 8:
        score -= 1
    score -= 2 if signal.upper_wick_rate > 0.6 else 0
    return _clip(score, 20), reasons


def _bottom_score(bundle: RecommendationDataBundle) -> tuple[float, list[str]]:
    values = bundle.price_features.values
    bars = list(bundle.daily_bars[-260:])
    if not bars:
        return 0.0, []
    score = 0.0
    reasons: list[str] = []
    position = float(values.get("position52", 1.0))
    if position <= .35:
        score += 5
    elif position <= .60:
        score += 4
    elif position <= .80:
        score += 2
    low60 = min(bar.low for bar in bars[-60:])
    rebound = bars[-1].close / low60 - 1 if low60 else 0
    if .05 <= rebound <= .25:
        score += 5; reasons.append("60일 저점에서 확인된 반등")
    elif .02 <= rebound < .05 or .25 < rebound <= .40:
        score += 3
    low20 = min(bar.low for bar in bars[-20:])
    if bars[-1].close > low20 * 1.03 and bool(values.get("recent_low_rising")):
        score += 3
    if bool(values.get("double_bottom_candidate")):
        score += 2
    if bool(values.get("recovered_ma20")):
        score += 2
    rsi = values.get("rsi14")
    if rsi is not None and 35 <= float(rsi) <= 60:
        score += 2
    if float(values.get("volume_surge", 0)) >= 1.3 and rebound > .03:
        score += 1
    if len(bars) >= 20 and min(bar.low for bar in bars[-5:]) <= min(bar.low for bar in bars[-20:]):
        score -= 4; reasons.append("최근 저점 갱신 지속")
    return _clip(score, 20), reasons


def _daily_score(bundle: RecommendationDataBundle, config: IntegratedScoringConfig) -> tuple[float, list[str]]:
    values = bundle.price_features.values
    closes = [bar.close for bar in bundle.daily_bars[-260:]]
    if not closes:
        return 0.0, []
    score = 0.0
    reasons: list[str] = []
    current = closes[-1]
    for key, points in (("ma5", 3), ("ma20", 3), ("ma60", 2)):
        if key in values and current > float(values[key]): score += points
    if _slope(closes, 5) > 0: score += 2
    if _slope(closes, 20) > 0: score += 2
    if len(bundle.daily_bars) >= 20:
        recent = bundle.daily_bars[-20:]
        if min(b.low for b in recent[-10:]) > min(b.low for b in recent[:10]): score += 2
    ret5 = float(values.get("return5", 0)); ret20 = float(values.get("return20", 0))
    if 0 < ret5 <= .10 and 0 < ret20 <= .20: score += 1; reasons.append("일봉 추세 상승 전환")
    if ret5 > config.daily_overheat_5d or ret20 > config.daily_overheat_20d or abs(float(values.get("gap_rate", 0))) > .12:
        score -= 5; reasons.append("단기 급등 과열")
    return _clip(score, 15), reasons


def _liquidity_score(bundle: RecommendationDataBundle, config: IntegratedScoringConfig) -> tuple[float, list[str]]:
    values = bundle.price_features.values
    avg20 = float(values.get("trading_value_avg20", 0) or 0)
    avg5 = float(values.get("trading_value_avg5", 0) or 0)
    score = 0.0
    if avg20 >= config.high_trading_value: score = 4
    elif avg20 >= config.liquid_trading_value: score = 3
    elif avg20 >= 500: score = 2
    elif avg20 >= config.extreme_low_trading_value: score = 1
    reasons: list[str] = []
    if avg20 and avg5 / avg20 >= config.liquidity_growth:
        score += 1; reasons.append("최근 거래대금 증가")
    return _clip(score, 5), reasons


def evaluate_preliminary_candidate(
    bundle: RecommendationDataBundle,
    as_of: datetime | None = None,
    config: IntegratedScoringConfig = DEFAULT_CONFIG,
) -> PreliminaryRecommendationScore:
    """Evaluate one bundle without I/O, UI, login, or TR dependencies."""
    features = to_recommendation_features(bundle)
    allowed, exclusion = universe_decision(bundle.master)
    if not allowed or not features.item.tradable:
        return PreliminaryRecommendationScore(features.item, False, exclusion or features.item.exclusion_reason or "추천 제외 종목")
    signal = completed_weekly_signal(features.weekly_bars, as_of or features.as_of)
    if signal is None:
        return PreliminaryRecommendationScore(features.item, False, "완성 주봉 5개 미만")
    if not signal.weekly_close_above_ma5:
        return PreliminaryRecommendationScore(features.item, False, "마지막 완성 주봉 종가가 5주 이동평균선 이하")

    weekly, weekly_reasons = _weekly_score(bundle, signal, config)
    bottom, bottom_reasons = _bottom_score(bundle)
    daily, daily_reasons = _daily_score(bundle, config)
    liquidity, liquidity_reasons = _liquidity_score(bundle, config)
    fund = _clip(float(features.fund_flow_score or 0), 25)
    verified = [c for c in bundle.catalysts if c.verified and c.metadata.source and c.announced_at and c.announced_at <= (as_of or features.as_of)]
    catalyst = _clip(max((c.metadata.confidence for c in verified), default=0) * 15, 15)
    catalyst_status = "complete" if verified else "not_evaluated"

    risk_flags = [risk.display for risk in bundle.risks]
    risk_penalty = sum(max(0, risk.deduction) for risk in bundle.risks)
    risk_penalty += sum(max(0, risk.deduction) for risk in features.risks if risk.code in bundle.master.risk_labels)
    avg20 = float(bundle.price_features.values.get("trading_value_avg20", 0) or 0)
    if avg20 < config.extreme_low_trading_value:
        risk_penalty += 5; risk_flags.append("극단적 저유동성")
    values = bundle.price_features.values
    if float(values.get("return5", 0)) > config.daily_overheat_5d or float(values.get("return20", 0)) > config.daily_overheat_20d:
        risk_penalty += 5; risk_flags.append("최근 단기 급등 과열")
    if len(bundle.daily_bars) < config.minimum_daily_bars:
        risk_penalty += 5; risk_flags.append("일봉 데이터 부족")
    if any(bar.close <= 0 or bar.volume < 0 for bar in bundle.daily_bars):
        risk_penalty += 10; risk_flags.append("비정상 가격 또는 거래량")
    raw = weekly + bottom + fund + daily + liquidity + catalyst
    final = _clip(raw - risk_penalty, 100)
    reasons = weekly_reasons + bottom_reasons + list(features.fund_flow_reasons) + daily_reasons + liquidity_reasons
    flow_status = features.fund_flow_status
    status = "complete" if (
        flow_status == "complete"
        and len(bundle.daily_bars) >= 260
        and catalyst_status == "complete"
    ) else "partial"
    risk_status = "clear" if not risk_flags else "flagged"
    trading_value = float(values.get("trading_value_avg20", 0) or 0)
    return PreliminaryRecommendationScore(
        features.item, True, "마지막 완성 주봉 종가 > 해당 시점 5주 이동평균선",
        weekly, bottom, fund, daily, liquidity, catalyst, catalyst_status,
        _clip(risk_penalty, 100), _clip(raw, 100), final, tuple(reasons), tuple(risk_flags),
        risk_status, status, trading_value,
    )


def rank_preliminary_candidates(
    bundles: tuple[RecommendationDataBundle, ...],
    as_of: datetime | None = None,
    *, limit: int | None = None,
) -> tuple[tuple[PreliminaryRecommendationScore, ...], tuple[PreliminaryRecommendationScore, ...]]:
    evaluated = [evaluate_preliminary_candidate(bundle, as_of) for bundle in bundles]
    ranked = sorted(
        (score for score in evaluated if score.weekly_filter_passed),
        key=lambda score: (-score.final_total_score, -score.fund_flow_score, -score.bottom_reversal_score, -score.trading_value, score.item.code),
    )
    if limit is not None: ranked = ranked[:max(0, limit)]
    excluded = tuple(score for score in evaluated if not score.weekly_filter_passed)
    return tuple(ranked), excluded
