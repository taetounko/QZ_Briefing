from datetime import date

from qz_briefing.__main__ import parse_cli_arguments
from qz_briefing.recommendations.opt10059_diagnostic import cached_candidates, signed_flow_metrics, validate_opt10059_inputs


def valid_inputs():
    return {"일자":"20260724","종목코드":"000001","금액수량구분":"1","매매구분":"0","단위구분":"1"}


def test_local_enc_input_definition_accepts_verified_values():
    assert validate_opt10059_inputs(valid_inputs(),today=date(2026,7,24))==[]


def test_missing_invalid_and_future_inputs_are_blocked_locally():
    value=valid_inputs(); value.pop("매매구분"); value["단위구분"]="0"; value["일자"]="20260725"
    errors=validate_opt10059_inputs(value,today=date(2026,7,24))
    assert "missing:매매구분" in errors and "invalid:매매구분" in errors
    assert "invalid:단위구분" in errors and "future:일자" in errors


def test_signed_values_are_preserved_and_missing_is_not_zero():
    rows=[]
    for index in range(20):
        rows.append({"일자":f"202607{24-index:02d}","외국인투자자":str(-index-1),"기관계":str(index+1)})
    rows[0]["외국인투자자"]=""
    metrics=signed_flow_metrics(rows)
    assert metrics["foreign_5"] is None
    assert metrics["foreign_20"] is None
    assert metrics["institution_5"]==15
    assert metrics["institution_20"]==210


def test_duplicate_dates_are_not_counted_twice():
    rows=[{"일자":"20260724","외국인투자자":"+10","기관계":"-5"}]*2
    metrics=signed_flow_metrics(rows)
    assert metrics["row_count"]==1


def test_opt10059_cli_is_single_symbol_optional():
    parsed=parse_cli_arguments(["--diagnose-opt10059-live","--symbol","000001"])
    assert parsed.diagnose_opt10059_live and parsed.symbol=="000001"


def test_cached_candidates_are_deterministic_and_limited(tmp_path):
    path=tmp_path/"snapshots"; path.mkdir()
    (path/"live_summary.json").write_text('{"data":{"daily_metrics":{"000003":{"weekly":{"weekly_close_above_ma5":true}},"000001":{"weekly":{"weekly_close_above_ma5":true}},"000002":{"weekly":{"weekly_close_above_ma5":true}},"000004":{"weekly":{"weekly_close_above_ma5":true}}}}}',encoding="utf-8")
    assert cached_candidates(tmp_path)==["000001","000002","000003"]
