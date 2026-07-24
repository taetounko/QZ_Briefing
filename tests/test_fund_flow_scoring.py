from qz_briefing.recommendations.fund_flow import compute_fund_flow_score, parse_signed_number


AVG=100


def score(foreign,institution,average=AVG):
    return compute_fund_flow_score(foreign,institution,average)


def test_both_foreign_and_institution_strong_buy_reaches_25():
    result=score([.1]*15+[2]*5,[.1]*15+[2]*5)
    assert result.fund_flow_score==25 and result.joint_buy_5d and result.fund_flow_status=="complete"


def test_foreign_only_strong_buy():
    result=score([1]*20,[-1]*20)
    assert result.foreign_net_20d>0 and result.institution_net_20d<0 and 0<result.fund_flow_score<15


def test_institution_only_strong_buy():
    result=score([-1]*20,[1]*20)
    assert result.institution_net_20d>0 and result.foreign_net_20d<0 and 0<result.fund_flow_score<15


def test_recent_five_day_turnaround_gets_partial_score_despite_negative_20d():
    result=score([-2]*15+[3]*5,[-2]*15+[3]*5)
    assert result.foreign_net_20d<0 and result.foreign_net_5d>0 and 5<result.fund_flow_score<25
    assert result.flow_acceleration>0


def test_one_large_buy_with_four_sell_days_is_not_full_score():
    result=score([0]*15+[-1,-1,-1,-1,100],[0]*15+[-1,-1,-1,-1,100])
    assert result.foreign_buy_days_5d==1 and result.institution_buy_days_5d==1
    assert result.fund_flow_score<25


def test_all_sell_is_zero_and_floor_is_enforced():
    result=score([-100]*20,[-100]*20)
    assert result.fund_flow_score==0


def test_partial_and_empty_statuses():
    partial=score([1]*4,[1]*4); empty=score([],[])
    assert partial.fund_flow_status=="partial" and partial.foreign_net_5d is None
    assert empty.fund_flow_status=="data_unavailable" and empty.fund_flow_score==0


def test_zero_average_trading_value_is_safe():
    result=score([1]*20,[1]*20,0)
    assert result.foreign_normalized_5d is None and 0<=result.fund_flow_score<=25


def test_signed_number_parses_spaces_commas_and_signs():
    assert parse_signed_number(" +1,234 ")==1234
    assert parse_signed_number("-2,345")==-2345
    assert parse_signed_number("") is None


def test_score_ceiling_and_floor():
    assert score([1]*15+[10**9]*5,[1]*15+[10**9]*5).fund_flow_score==25
    assert score([-10**9]*20,[-10**9]*20).fund_flow_score==0


def test_same_input_is_deterministic():
    args=([1,-1]*10,[2,-2]*10,AVG)
    assert compute_fund_flow_score(*args)==compute_fund_flow_score(*args)
