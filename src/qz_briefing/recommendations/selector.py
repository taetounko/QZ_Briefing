from __future__ import annotations

from .data_models import RecommendationDataBundle
from .models import DailyRecommendationReport, RecommendationFeatures, RecommendationPolicy, StockRecommendation
from .scoring import evaluate_candidate, evaluate_integrated_bundle


def _sort_key(score):
    return (
        -score.total_score, -score.confidence,
        -score.components["fund_inflow"], -score.components["weekly_settlement"],
        score.risk_deduction, -score.components["bottom_rebound"],
        -(score.features.trading_value or 0), score.item.code,
    )


def select_recommendations(features: list[RecommendationFeatures], policy: RecommendationPolicy | None = None) -> DailyRecommendationReport:
    """Legacy feature-only selector retained for existing validation compatibility."""
    policy=policy or RecommendationPolicy()
    scores=[evaluate_candidate(value,policy) for value in features]
    eligible=sorted((score for score in scores if score.eligible),key=_sort_key)
    excluded=[{"code":score.item.code,"name":score.item.name,"reason":"; ".join(score.exclusion_reasons)} for score in scores if not score.eligible]
    strong_scores=[score for score in eligible if score.total_score >= policy.strong_threshold and score.confidence >= 0.7 and score.risk_deduction < 15][:policy.strong_limit]
    strong_ids={score.item.code for score in strong_scores}
    remaining=policy.total_limit-len(strong_scores)
    review_scores=[score for score in eligible if score.item.code not in strong_ids and score.total_score >= policy.review_threshold][:min(remaining, policy.review_limit)]
    ranked=sorted(strong_scores+review_scores,key=_sort_key)
    rank_by_code={score.item.code:index for index,score in enumerate(ranked,1)}
    strong=[StockRecommendation(rank_by_code[score.item.code],"완전 강추",score) for score in strong_scores]
    review=[StockRecommendation(rank_by_code[score.item.code],"강추·추가 검토",score) for score in review_scores]
    warnings=[]
    if len(strong)<2: warnings.append("완전 강추 기준 충족 종목이 2개 미만입니다.")
    if len(strong)+len(review)<5: warnings.append("추천 기준 통과 종목이 5개 미만이며 낮은 품질 종목을 채우지 않았습니다.")
    hard_pass=sum(bool(score.weekly and score.weekly.weekly_close_above_ma5) for score in scores)
    return DailyRecommendationReport(features[0].as_of if features else __import__('datetime').datetime.now(),len(features),hard_pass,excluded,strong,review,warnings)


def _integrated_sort_key(score):
    preliminary = score.preliminary
    return (
        -score.total_score,
        -(preliminary.fund_flow_score if preliminary else 0),
        -(preliminary.bottom_reversal_score if preliminary else 0),
        -(preliminary.trading_value if preliminary else 0),
        -score.confidence,
        score.risk_deduction,
        score.item.code,
    )


def select_integrated_recommendations(
    bundles: list[RecommendationDataBundle],
    policy: RecommendationPolicy | None = None,
) -> DailyRecommendationReport:
    """Production selector using exactly one integrated evaluation per bundle."""
    policy = policy or RecommendationPolicy()
    scores = [evaluate_integrated_bundle(bundle) for bundle in bundles]
    eligible = sorted((score for score in scores if score.eligible), key=_integrated_sort_key)
    excluded = [
        {"code": score.item.code, "name": score.item.name, "reason": "; ".join(score.exclusion_reasons)}
        for score in scores if not score.eligible
    ]

    def stable_weekly(score) -> bool:
        return bool(score.weekly and (
            score.weekly.consecutive_weeks >= 1
            or (score.weekly.ma5_slope_rate is not None and score.weekly.ma5_slope_rate > 0)
        ))

    strong_scores = [
        score for score in eligible
        if score.total_score >= policy.strong_threshold
        and score.confidence >= .7
        and score.risk_deduction < 15
        and score.preliminary is not None
        and score.preliminary.evaluation_status == "complete"
        and len(score.reasons) >= 2
        and stable_weekly(score)
    ][:policy.strong_limit]
    strong_codes = {score.item.code for score in strong_scores}
    review_scores = [
        score for score in eligible
        if score.item.code not in strong_codes
        and score.total_score >= policy.review_threshold
        and score.preliminary is not None
        and max(
            score.preliminary.bottom_reversal_score,
            score.preliminary.fund_flow_score,
            score.preliminary.daily_trend_score,
            score.preliminary.liquidity_score,
        ) > 0
    ][:min(policy.review_limit, policy.total_limit-len(strong_scores))]
    ranked = sorted(strong_scores + review_scores, key=_integrated_sort_key)
    ranks = {score.item.code: index for index, score in enumerate(ranked, 1)}
    strong = [StockRecommendation(ranks[score.item.code], "완전 강추", score) for score in strong_scores]
    review = [StockRecommendation(ranks[score.item.code], "강추·추가 검토", score) for score in review_scores]
    warnings = []
    if len(strong) < 2: warnings.append("완전 강추 기준 충족 종목이 2개 미만입니다.")
    if len(strong) + len(review) < 5: warnings.append("조건 충족 후보가 부족하여 하위 종목을 강제로 충원하지 않았습니다.")
    as_of = bundles[0].master.metadata.as_of if bundles else __import__('datetime').datetime.now()
    return DailyRecommendationReport(as_of, len(bundles), len(eligible), excluded, strong, review, warnings)
