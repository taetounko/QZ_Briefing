from dataclasses import replace

from qz_briefing.__main__ import run
from qz_briefing.recommendations.integrated_validation import validation_bundles
from qz_briefing.recommendations.models import RecommendationPolicy
from qz_briefing.recommendations.renderer import render_recommendations
from qz_briefing.recommendations.scoring import evaluate_integrated_bundle
from qz_briefing.recommendations.selector import select_integrated_recommendations


def test_bundle_is_mapped_to_integrated_score_without_reweighting():
    score = evaluate_integrated_bundle(validation_bundles()[0])
    assert score.preliminary is not None
    assert score.gross_score == sum(score.components.values())
    assert score.components["fund_inflow"] == score.preliminary.fund_flow_score


def test_weekly_hard_filter_failure_is_not_integrated_or_ranked():
    report = select_integrated_recommendations(validation_bundles())
    assert "910006" not in {row.score.item.code for row in report.strong + report.review}
    assert any(row["code"] == "910006" for row in report.excluded)


def test_missing_flow_preserves_other_market_scores():
    score = evaluate_integrated_bundle(validation_bundles()[8])
    assert score.components["fund_inflow"] == 0
    assert score.components["weekly_settlement"] > 0
    assert score.components["daily_trend"] > 0


def test_missing_catalyst_is_not_evaluated_and_not_awarded_points():
    score = evaluate_integrated_bundle(validation_bundles()[2])
    assert score.preliminary.catalyst_status == "not_evaluated"
    assert score.components["catalyst"] == 0
    assert "검증된 재료·실적 자료 부족" in score.missing


def test_real_zero_and_missing_flow_have_distinct_statuses():
    bundle = validation_bundles()[0]
    zero_flow = replace(bundle.investor_flow, foreign_daily=(0,)*20, institution_daily=(0,)*20)
    zero_score = evaluate_integrated_bundle(replace(bundle, investor_flow=zero_flow))
    missing_score = evaluate_integrated_bundle(replace(bundle, investor_flow=None))
    assert zero_score.preliminary.evaluation_status == "complete"
    assert missing_score.preliminary.evaluation_status == "partial"


def test_risk_penalty_is_applied_exactly_once():
    score = evaluate_integrated_bundle(validation_bundles()[4])
    assert score.risk_deduction >= 18
    assert score.total_score == max(0, round(score.gross_score-score.risk_deduction, 1))


def test_final_weight_ceiling_is_one_hundred():
    score = evaluate_integrated_bundle(validation_bundles()[0])
    assert sum(score.components.values()) <= 100
    assert 0 <= score.total_score <= 100


def test_deterministic_integrated_tie_order():
    bundles = validation_bundles()
    first = select_integrated_recommendations(bundles)
    second = select_integrated_recommendations(list(reversed(bundles)))
    assert [r.score.item.code for r in first.strong+first.review] == [r.score.item.code for r in second.strong+second.review]


def test_integrated_tie_breaker_prefers_confidence_then_lower_risk():
    bundles = validation_bundles()[8:10]
    lower_confidence = replace(
        bundles[0], master=replace(
            bundles[0].master,
            metadata=replace(bundles[0].master.metadata, confidence=.5),
        ),
    )
    report = select_integrated_recommendations(
        [lower_confidence, bundles[1]], RecommendationPolicy(review_threshold=0)
    )
    assert [row.score.item.code for row in report.strong+report.review][0] == bundles[1].master.metadata.code


def test_grade_groups_are_disjoint_and_not_forced_to_fill():
    report = select_integrated_recommendations(validation_bundles()[:3])
    strong = {row.score.item.code for row in report.strong}
    review = {row.score.item.code for row in report.review}
    assert not strong & review
    assert len(strong) <= 3 and len(review) <= 3


def test_safe_markdown_contains_integrated_status_and_no_null_tokens():
    text = render_recommendations(select_integrated_recommendations(validation_bundles()))
    assert "평가 상태:" in text and "데이터 출처 요약:" in text
    assert all(token not in text for token in ("None", "null", "unknown"))


def test_offline_validation_cli_has_no_external_calls(capsys):
    assert run(["--validate-integrated-recommendation-pipeline"]) == 0
    output = capsys.readouterr().out
    assert "EXTERNAL_CALLS=0" in output
    assert "INTEGRATED RECOMMENDATION PIPELINE VALIDATION: PASS" in output
