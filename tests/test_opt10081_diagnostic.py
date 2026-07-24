from __future__ import annotations

from qz_briefing.__main__ import parse_cli_arguments
from qz_briefing.recommendations.opt10081_diagnostic import inspect_callback, normalize_market_codes


class DiagnosticAdapter:
    def __init__(self) -> None:
        self.repeat = {
            "주식일봉차트조회": 2,
            "qz_diag_d1": 0,
        }

    def get_connect_state(self):
        return 1

    def get_repeat_count(self, tr_code, record_name):
        assert tr_code == "opt10081"
        return self.repeat.get(record_name, 0)

    def get_comm_data_ex(self, tr_code, record_name):
        return [["row1"], ["row2"]] if record_name == "주식일봉차트조회" else []

    def get_comm_data(self, tr_code, record_name, index, field):
        values = {
            (0, "일자"): "20260724",
            (1, "일자"): "20260723",
        }
        return values.get((index, field), "1")


def test_market_code_normalization_keeps_six_digit_codes_and_reports_each_stage():
    codes, counts = normalize_market_codes("005930;000660;;005930;bad;")
    assert codes == ["005930", "000660"]
    assert counts == {
        "raw_semicolon_items": 6,
        "nonempty_items": 4,
        "unique_items": 3,
        "six_digit_codes": 2,
    }


def test_callback_record_name_finds_rows_when_current_rq_name_parser_does_not():
    result = inspect_callback(
        DiagnosticAdapter(),
        ("9181", "qz_diag_d1", "opt10081", "주식일봉차트조회", "2", "0", "0", "", ""),
        "qz_diag_d1",
    )
    assert result["repeat_counts"] == {
        "callback_record_name": 2,
        "known_record_name": 2,
        "current_parser_name": 0,
    }
    assert result["known_record_get_comm_data_ex_rows"] == 2
    assert result["parsed_row_count"] == 0
    assert result["sample_fields"]["first"]["일자"] == "20260724"
    assert result["sample_fields"]["last"]["일자"] == "20260723"


def test_live_diagnostic_cli_requires_and_accepts_one_symbol():
    parsed = parse_cli_arguments(["--diagnose-opt10081-live", "--symbol", "005930"])
    assert parsed.diagnose_opt10081_live
    assert parsed.symbol == "005930"
