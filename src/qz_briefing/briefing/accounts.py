# -*- coding: utf-8 -*-
r"""Read-only multi-account stock holdings via GetLoginInfo and OPW00018.

Official sources: C:\OpenAPI\data\opw00018.enc and
C:\OpenAPI\koatrinputlegend.ini. Password is officially unused (empty),
비밀번호입력매체구분 is 00, 조회구분 is 2 (개별).
"""

from __future__ import annotations

from typing import Protocol

from qz_briefing.kiwoom.tr_requests import KiwoomTrRequestQueue, TrRequest
from .collectors import normalize_decimal, normalize_integer

ACCOUNT_FIELDS = (
    "종목번호", "종목명", "평가손익", "수익률(%)", "매입가", "전일종가",
    "보유수량", "매매가능수량", "현재가", "매입금액", "매입수수료",
    "평가금액", "평가수수료", "세금", "수수료합", "보유비중(%)",
    "신용구분", "신용구분명", "대출일",
)


class LoginInfoAdapter(Protocol):
    def get_login_info(self, tag: str) -> str: ...


def parse_accounts(raw: str) -> list[str]:
    seen, accounts = set(), []
    for value in str(raw).split(";"):
        account = value.strip()
        if account and account not in seen:
            seen.add(account); accounts.append(account)
    return accounts


def mask_account(account: str) -> str:
    return "*" * max(0, len(account) - 4) + account[-4:]


class KiwoomAccountHoldingsSource:
    def __init__(self, adapter: LoginInfoAdapter, queue: KiwoomTrRequestQueue) -> None:
        self._adapter, self._queue = adapter, queue

    def accounts(self) -> list[str]:
        return parse_accounts(self._adapter.get_login_info("ACCNO"))

    def holdings(self, account: str) -> list[dict[str, str]]:
        return self._queue.request_rows(TrRequest(
            request_name=f"qz_account_holdings_{account[-4:]}",
            tr_code="OPW00018",
            inputs={"계좌번호": account, "비밀번호": "", "비밀번호입력매체구분": "00", "조회구분": "2", "거래소구분": ""},
            output_fields=ACCOUNT_FIELDS, repeat=True, paginate=True, max_pages=20,
        ))


def normalize_account_row(row: dict[str, str]) -> dict[str, object]:
    code = row.get("종목번호", "").strip()
    if code.startswith("A"): code = code[1:]
    return {
        "code": code, "name": row.get("종목명", "").strip(),
        "quantity": normalize_integer(row.get("보유수량", ""), absolute=True),
        "average_price": normalize_integer(row.get("매입가", ""), absolute=True),
        "current_price": normalize_integer(row.get("현재가", ""), absolute=True),
        "invested_amount": normalize_integer(row.get("매입금액", ""), absolute=True),
        "market_value": normalize_integer(row.get("평가금액", ""), absolute=True),
        "unrealized_profit": normalize_integer(row.get("평가손익", "")),
        "return_rate": normalize_decimal(row.get("수익률(%)", "")),
        "available_quantity": normalize_integer(row.get("매매가능수량", ""), absolute=True),
        "additional_evaluation": {key: row.get(key, "") for key in ("매입수수료", "평가수수료", "세금", "수수료합", "보유비중(%)", "신용구분", "신용구분명", "대출일")},
        "raw": dict(row), "warnings": [],
    }


def consolidate(accounts: list[dict[str, object]]) -> dict[str, object]:
    merged: dict[str, dict[str, object]] = {}
    for account in accounts:
        for holding in account.get("holdings", []):
            if not isinstance(holding, dict) or not holding.get("code"): continue
            code = str(holding["code"]); item = merged.setdefault(code, {"code": code, "name": holding.get("name"), "quantity": 0, "available_quantity": 0, "invested_amount": 0, "market_value": 0, "unrealized_profit": 0, "current_price": holding.get("current_price"), "account_ids": []})
            for key in ("quantity", "available_quantity", "invested_amount", "market_value", "unrealized_profit"):
                item[key] = float(item[key]) + float(holding.get(key) or 0)
            item["account_ids"].append(account["account_id"])
    for item in merged.values():
        quantity, invested = float(item["quantity"]), float(item["invested_amount"])
        item["average_price"] = invested / quantity if quantity else None
        item["return_rate"] = float(item["unrealized_profit"]) / invested * 100 if invested else None
        item["multi_account"] = len(item["account_ids"]) > 1
    holdings = list(merged.values())
    invested = sum(float(item["invested_amount"]) for item in holdings)
    market = sum(float(item["market_value"]) for item in holdings)
    profit = sum(float(item["unrealized_profit"]) for item in holdings)
    return {"holding_count": len(holdings), "total_invested_amount": invested, "total_market_value": market, "total_unrealized_profit": profit, "total_return_rate": profit / invested * 100 if invested else None, "holdings": holdings}
