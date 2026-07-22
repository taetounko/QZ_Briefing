# -*- coding: utf-8 -*-

import json
from datetime import date, datetime
from pathlib import Path

from qz_briefing.briefing.accounts import (
    KiwoomAccountHoldingsSource, consolidate, mask_account,
    normalize_account_row, parse_accounts,
)
from qz_briefing.briefing.holdings import HoldingsCollector
from qz_briefing.briefing.models import BriefingContext, BriefingType
from qz_briefing.briefing.renderer import render_holdings


class Adapter:
    def __init__(self, value): self.value = value; self.tags = []
    def get_login_info(self, tag): self.tags.append(tag); return self.value


class Queue:
    def __init__(self, rows=None): self.rows = rows or []; self.requests = []
    def request_rows(self, request): self.requests.append(request); return self.rows


def official_row(code="A005930", quantity="10", price="120", invested="1,000", value="1,200", profit="200"):
    return {"종목번호": code, "종목명": "삼성전자", "보유수량": quantity, "매입가": "100", "현재가": price, "매입금액": invested, "평가금액": value, "평가손익": profit, "수익률(%)": "20.0", "매매가능수량": "8", "매입수수료": "1", "평가수수료": "1", "세금": "2", "수수료합": "4", "보유비중(%)": "50", "신용구분": "", "신용구분명": "", "대출일": ""}


def test_account_list_cleanup_mask_and_official_login_tag() -> None:
    assert parse_accounts(" 12345678;;87654321;12345678;") == ["12345678", "87654321"]
    assert mask_account("12345678") == "****5678"
    adapter = Adapter("12345678;"); source = KiwoomAccountHoldingsSource(adapter, Queue())
    assert source.accounts() == ["12345678"] and adapter.tags == ["ACCNO"]


def test_opw00018_official_inputs_repeat_and_pagination() -> None:
    queue = Queue([official_row()]); source = KiwoomAccountHoldingsSource(Adapter(""), queue)
    assert source.holdings("12345678") == [official_row()]
    request = queue.requests[0]
    assert request.tr_code == "OPW00018"
    assert request.inputs == {"계좌번호": "12345678", "비밀번호": "", "비밀번호입력매체구분": "00", "조회구분": "2", "거래소구분": ""}
    assert request.repeat and request.paginate and request.max_pages == 20


def test_official_row_normalization() -> None:
    item = normalize_account_row(official_row())
    assert item["code"] == "005930" and item["quantity"] == 10
    assert item["average_price"] == 100 and item["unrealized_profit"] == 200
    assert item["available_quantity"] == 8


def test_multi_account_consolidation_uses_weighted_average_and_official_profit() -> None:
    first = normalize_account_row(official_row(quantity="10", invested="1,000", value="1,200", profit="200"))
    second = normalize_account_row(official_row(quantity="20", invested="3,000", value="3,300", profit="300"))
    result = consolidate([
        {"account_id": "****1111", "holdings": [first]},
        {"account_id": "****2222", "holdings": [second]},
    ])
    item = result["holdings"][0]
    assert item["quantity"] == 30 and round(item["average_price"], 2) == 133.33
    assert item["unrealized_profit"] == 500 and item["multi_account"] is True
    assert result["total_return_rate"] == 12.5


class AccountSource:
    def __init__(self, accounts, results): self._accounts = accounts; self.results = results; self.calls = []
    def accounts(self): return self._accounts
    def holdings(self, account):
        self.calls.append(account)
        value = self.results[account]
        if isinstance(value, Exception):
            raise value
        return value


class Daily:
    def daily(self, code, target_date):
        rows = [{"일자": str(i), "현재가": str(61 + i), "시가": str(60 + i), "고가": str(63 + i), "저가": str(59 + i), "거래량": "1000"} for i in range(60)]
        return list(reversed(rows))


class Stock:
    def __init__(self): self.calls = 0
    def get_stock_basic_info(self, code): self.calls += 1; return {"종목명": "수동", "현재가": "100"}


def context():
    now = datetime(2026, 7, 22, 10)
    return BriefingContext(BriefingType.INTRADAY_10AM, date(2026, 7, 22), now, now, "open", "weekday")


def test_auto_success_wins_without_manual_or_duplicate_current_lookup(tmp_path: Path) -> None:
    manual = tmp_path / "holdings.json"
    manual.write_text(json.dumps({"schema_version": 1, "holdings": [{"code": "000660", "name": "수동", "quantity": 1, "average_price": 1}]}), encoding="utf-8")
    stock = Stock(); accounts = AccountSource(["12345678"], {"12345678": [official_row()]})
    result = HoldingsCollector(manual, stock, Daily(), account_source=accounts).collect(context())
    assert result["source"] == "kiwoom_accounts"
    assert [item["code"] for item in result["holdings"]] == ["005930"]
    assert stock.calls == 0
    serialized = json.dumps(result, ensure_ascii=False)
    assert "12345678" not in serialized and "****5678" in serialized


def test_one_account_failure_continues_and_all_failure_uses_manual(tmp_path: Path) -> None:
    manual = tmp_path / "holdings.json"
    manual.write_text(json.dumps({"schema_version": 1, "holdings": [{"code": "000660", "name": "수동", "quantity": 1, "average_price": 100}]}), encoding="utf-8")
    partial = AccountSource(["11111111", "22222222"], {"11111111": RuntimeError("unsupported"), "22222222": []})
    result = HoldingsCollector(manual, Stock(), Daily(), account_source=partial).collect(context())
    assert result["source"] == "kiwoom_accounts" and result["accounts"][1]["status"] == "completed"
    failed = AccountSource(["11111111"], {"11111111": RuntimeError("failed")})
    fallback = HoldingsCollector(manual, Stock(), Daily(), account_source=failed).collect(context())
    assert fallback["source"] == "manual_fallback"
    assert fallback["holdings"][0]["code"] == "000660"


def test_account_markdown_contains_only_masked_account_and_official_summary(tmp_path: Path) -> None:
    accounts = AccountSource(["12345678"], {"12345678": [official_row()]})
    result = HoldingsCollector(tmp_path / "missing.json", Stock(), Daily(), account_source=accounts).collect(context())
    markdown = "\n".join(render_holdings(result))

    assert "로그인 계좌 자동조회" in markdown
    assert "****5678" in markdown and "12345678" not in markdown
    assert "평가금액" in markdown and "1,200" in markdown
    assert result["portfolio"]["profit_loss"] == 200


def test_account_number_is_redacted_from_failure_detail(tmp_path: Path) -> None:
    accounts = AccountSource(["12345678"], {"12345678": RuntimeError("account 12345678 failed")})
    result = HoldingsCollector(tmp_path / "missing.json", Stock(), Daily(), account_source=accounts).collect(context())

    serialized = json.dumps(result, ensure_ascii=False)
    assert "12345678" not in serialized
    assert "****5678" in serialized
