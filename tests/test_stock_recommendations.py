from dataclasses import replace
from datetime import timedelta

from qz_briefing.__main__ import parse_cli_arguments, run
from qz_briefing.recommendations.features import completed_weekly_signal
from qz_briefing.recommendations.models import CatalystEvidence, RecommendationPolicy, RiskFlag, WeeklyBar
from qz_briefing.recommendations.renderer import render_recommendations
from qz_briefing.recommendations.scoring import evaluate_candidate
from qz_briefing.recommendations.selector import select_recommendations
from qz_briefing.recommendations.validation import AS_OF, feature, validation_features, validate_stock_recommendations, weekly


def test_only_weekly_close_above_ma5_is_investment_signal_hard_filter():
    weak=feature("200001","보조부족",bottom=0,fund=0,daily=0,catalyst=None,liquidity=0)
    assert evaluate_candidate(weak).eligible
    below=feature("200003","주봉미달",closes=[110,108,105,102,99,95],bottom=1,fund=1,daily=1,catalyst=1)
    assert not evaluate_candidate(below).eligible


def test_incomplete_current_week_is_ignored_without_lookahead():
    bars=weekly([90,92,94,96,100,104],incomplete_close=50)
    signal=completed_weekly_signal(bars,AS_OF)
    assert signal.weekly_close==104 and signal.completed_at <= AS_OF
    future=WeeklyBar(AS_OF+timedelta(days=7),200,200,200,200,True)
    assert completed_weekly_signal(bars+(future,),AS_OF).weekly_close==104


def test_flexible_components_compensate_for_each_other():
    flow=evaluate_candidate(feature("200005","수급우세",bottom=.1,fund=1,daily=.8,catalyst=1))
    rebound=evaluate_candidate(feature("200007","반등우세",bottom=1,fund=.9,daily=.8,catalyst=None))
    assert flow.total_score >= 60 and rebound.total_score >= 52
    assert flow.components["fund_inflow"] > flow.components["bottom_rebound"]
    assert rebound.components["catalyst"]==0


def test_risk_deduction_is_exact_and_warning_does_not_exclude_tradable():
    base=feature("200009","위험",risks=())
    risky=replace(base,risks=(RiskFlag("managed","관리·경고 종목",12.5),))
    plain=evaluate_candidate(base); scored=evaluate_candidate(risky)
    assert scored.eligible and plain.total_score-scored.total_score==12.5


def test_operationally_untradable_and_non_common_instruments_are_excluded():
    stopped=feature("200011","정지",tradable=False)
    etf=feature("200013","ETF",security_type="etf")
    assert not evaluate_candidate(stopped).eligible
    assert not evaluate_candidate(etf).eligible


def test_groups_are_disjoint_target_sized_and_underfill_is_explicit():
    report=select_recommendations(validation_features())
    strong={r.score.item.code for r in report.strong}; review={r.score.item.code for r in report.review}
    assert not strong & review and 5 <= len(strong|review) <= 6
    assert 2 <= len(report.strong) <= 3
    assert 2 <= len(report.review) <= 3
    under=select_recommendations(validation_features()[:2])
    assert len(under.strong)+len(under.review)<5
    assert any("채우지 않았습니다" in warning for warning in under.warnings)


def test_tie_breaking_is_deterministic_by_policy_then_code():
    tied=[feature("200016","나",confidence=.8),feature("200014","가",confidence=.8)]
    first=select_recommendations(tied,RecommendationPolicy(review_threshold=0))
    second=select_recommendations(list(reversed(tied)),RecommendationPolicy(review_threshold=0))
    assert [r.score.item.code for r in first.strong+first.review]==[r.score.item.code for r in second.strong+second.review]
    assert [r.score.item.code for r in first.strong+first.review]==["200014","200016"]


def test_catalyst_requires_source_and_non_future_timestamp():
    base=feature("200017","재료검증",catalyst=1)
    invalid=replace(base,catalyst_evidence=(CatalystEvidence("미확인","",AS_OF),CatalystEvidence("미래","fixture",AS_OF+timedelta(days=1))))
    score=evaluate_candidate(invalid)
    assert score.components["catalyst"]==0
    assert any("재료 자료 부족" in item for item in score.missing)


def test_renderer_has_reasons_risks_no_null_and_execution_guidance():
    report=select_recommendations(validation_features())
    text=render_recommendations(report)
    assert "핵심 근거" in text and "주요 위험" in text
    assert "수급 평가" in text and "재료·실적 평가" in text and "과열·위험 감점" in text
    assert "한 줄 요약" in text
    assert "추격매수 금지" in text and "무효화 조건" in text
    assert "None" not in text and "null" not in text and "unknown" not in text


def test_offline_validation_and_cli_never_start_external_runtime(capsys):
    result=validate_stock_recommendations(); assert result["success"]
    assert parse_cli_arguments(["--validate-stock-recommendations"]).validate_stock_recommendations
    def forbidden(*args,**kwargs): raise AssertionError("external call")
    assert run(["--validate-stock-recommendations"],application_factory=forbidden,adapter_factory=forbidden,lock_factory=forbidden,notification_service_factory=forbidden)==0
    output=capsys.readouterr().out
    assert "STOCK RECOMMENDATION VALIDATION: PASS" in output
    assert "token" not in output.lower() and "account" not in output.lower()


def test_all_supporting_scores_can_be_weak_after_weekly_hard_filter():
    score=evaluate_candidate(feature("200019","하드필터만통과",bottom=0,fund=0,daily=0,catalyst=None,liquidity=0))
    assert score.eligible
    assert score.total_score == score.components["weekly_settlement"]


def test_missing_inputs_do_not_create_scores_or_fake_catalysts():
    score=evaluate_candidate(feature("200021","자료부족",bottom=None,fund=None,daily=None,catalyst=None,liquidity=None))
    assert score.components["bottom_rebound"] == 0
    assert score.components["fund_inflow"] == 0
    assert score.components["daily_trend"] == 0
    assert score.components["catalyst"] == 0
    assert score.components["liquidity"] == 0


def test_risk_can_change_rank_without_becoming_an_automatic_exclusion():
    safe=feature("200023","안전",bottom=.8,fund=.8,daily=.8,catalyst=.8)
    risky=feature("200025","위험",bottom=1,fund=1,daily=1,catalyst=1,risks=(RiskFlag("risk","확인 필요",20),))
    report=select_recommendations([risky,safe],RecommendationPolicy(review_threshold=0))
    selected=report.strong+report.review
    assert selected[0].score.item.code == "200023"
    assert evaluate_candidate(risky).eligible


def test_review_group_never_exceeds_three_even_when_strong_is_empty():
    candidates=[feature(f"{200030+index:06d}",f"후보{index}",confidence=.6) for index in range(6)]
    report=select_recommendations(candidates,RecommendationPolicy(review_threshold=0))
    assert not report.strong
    assert len(report.review) == 3
    assert report.warnings


def test_renderer_includes_full_weighted_breakdown_and_evidence_time():
    text=render_recommendations(select_recommendations(validation_features()))
    for label in ("주봉 5주선 안착 평가","일봉 추세 평가","유동성·매매 적합성","재료 근거"):
        assert label in text
    assert "offline_fixture" in text
    assert AS_OF.isoformat() not in text or "기준 시각" in text
