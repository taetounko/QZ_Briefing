from __future__ import annotations

from .features import completed_weekly_signal
from .models import RecommendationFeatures, RecommendationPolicy, RecommendationScore


def _ratio(value: float | None) -> float:
    return min(1.0, max(0.0, float(value))) if value is not None else 0.0


def _weekly_quality(signal) -> float:
    score = 0.25
    score += min(signal.consecutive_weeks, 4) * 0.10
    if signal.ma5_slope_rate is not None:
        score += 0.20 if signal.ma5_slope_rate > 0 else -0.10
    if 0 < signal.distance_rate <= 8: score += 0.20
    elif signal.distance_rate > 15: score -= 0.15
    if signal.upper_wick_rate <= 0.35: score += 0.10
    elif signal.upper_wick_rate >= 0.60: score -= 0.15
    return _ratio(score)


def evaluate_candidate(features: RecommendationFeatures, policy: RecommendationPolicy | None = None) -> RecommendationScore:
    policy = policy or RecommendationPolicy()
    item = features.item
    exclusions: list[str] = []
    if item.market not in policy.allowed_markets: exclusions.append("비정상 또는 비대상 시장")
    if item.security_type not in policy.allowed_security_types: exclusions.append(f"추천 제외 유형: {item.security_type}")
    if not item.tradable: exclusions.append(item.exclusion_reason or "현재 거래 불가능")
    if len(item.code) != 6 or not item.code.isdigit(): exclusions.append("비정상 종목 코드")
    signal = completed_weekly_signal(features.weekly_bars, features.as_of)
    if signal is None: exclusions.append("완성 주봉 또는 5주 이동평균 자료 부족")
    elif not signal.weekly_close_above_ma5: exclusions.append("마지막 완성 주봉 종가가 5주 이동평균선 이하")

    valid_evidence = [e for e in features.catalyst_evidence if e.source.strip() and e.observed_at <= features.as_of]
    catalyst = _ratio(features.catalyst_strength) if valid_evidence else 0.0
    components = {
        "weekly_settlement": (_weekly_quality(signal) if signal else 0) * policy.weekly_weight,
        "bottom_rebound": _ratio(features.bottom_rebound) * policy.bottom_weight,
        "fund_inflow": _ratio(features.fund_inflow) * policy.fund_weight,
        "daily_trend": _ratio(features.daily_trend) * policy.daily_weight,
        "catalyst": catalyst * policy.catalyst_weight,
        "liquidity": _ratio(features.liquidity) * policy.liquidity_weight,
    }
    gross = sum(components.values())
    risk = sum(max(0.0, flag.deduction) for flag in features.risks)
    total = max(0.0, gross - risk)
    reasons = [
        name for name, value in sorted(components.items(), key=lambda pair: (-pair[1], pair[0])) if value >= 6
    ][:4]
    missing = list(features.missing)
    if features.catalyst_strength is not None and not valid_evidence:
        missing.append("출처와 기준 시각이 확인된 재료 자료 부족")
    return RecommendationScore(
        item=item, eligible=not exclusions, exclusion_reasons=exclusions,
        weekly=signal, components={key:round(value,2) for key,value in components.items()},
        gross_score=round(gross,2), risk_deduction=round(risk,2),
        total_score=round(total,2), confidence=_ratio(features.confidence),
        reasons=reasons, missing=missing, risks=[flag.reason for flag in features.risks],
        features=features,
    )
