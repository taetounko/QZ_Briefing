# -*- coding: utf-8 -*-
r"""Read-only derivatives and KOSPI program-trading collection.

Local official sources:
- C:\OpenAPI\data\opt50001.enc (OPT50001, 선옵현재가정보요청)
- C:\OpenAPI\data\opt50038.enc (OPT50038, 투자자별만기손익차트요청)
- C:\OpenAPI\data\opt90005.enc (OPT90005, 프로그램매매추이요청)
- C:\OpenAPI\koatrinputlegend.ini (input codes and documented units)

The local material does not document a safe KOSPI200 front-month resolver.
It also marks OPT50039 (institution aggregate positions) unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from qz_briefing.kiwoom.tr_requests import KiwoomTrRequestQueue, TrRequest

from .collectors import normalize_decimal, normalize_integer
from .models import BriefingContext

FUTURES_QUOTE_FIELDS = (
    "현재가", "전일대비", "등락율", "거래량", "누적거래대금", "종목명",
    "시가", "고가", "저가", "미결제약정",
)
FUTURES_INVESTOR_FIELDS = ("종목코드", "투자자별순매수수량")
PROGRAM_FIELDS = (
    "체결시간", "일자", "차익거래매도", "차익거래매수", "차익거래순매수",
    "비차익거래매도", "비차익거래매수", "비차익거래순매수",
    "전체매도", "전체매수", "전체순매수",
)


@dataclass(frozen=True)
class FuturesContractResolution:
    status: str
    code: str | None = None
    method: str = "local_official_material_unavailable"
    warning: str | None = None


class FuturesContractResolver(Protocol):
    def resolve(self) -> FuturesContractResolution: ...


class UnavailableFuturesContractResolver:
    """Refuse to guess a dated futures code without a documented resolver."""

    def resolve(self) -> FuturesContractResolution:
        return FuturesContractResolution(
            status="unavailable",
            warning=(
                "local official OpenAPI material does not document a safe "
                "KOSPI200 front-month contract resolver"
            ),
        )


class DerivativesDataSource(Protocol):
    def get_futures_quote(self, code: str) -> dict[str, str]: ...
    def get_futures_investor(
        self, code: str, trading_date: str, investor_code: str
    ) -> dict[str, str]: ...
    def get_program_trading(self, trading_date: str) -> dict[str, str]: ...


class KiwoomDerivativesDataSource:
    def __init__(self, tr_queue: KiwoomTrRequestQueue) -> None:
        self._tr_queue = tr_queue

    def get_futures_quote(self, code: str) -> dict[str, str]:
        return self._tr_queue.request(TrRequest(
            request_name=f"qz_futures_quote_{code}", tr_code="OPT50001",
            inputs={"종목코드": code}, output_fields=FUTURES_QUOTE_FIELDS,
        ))

    def get_futures_investor(
        self, code: str, trading_date: str, investor_code: str
    ) -> dict[str, str]:
        rows = self._tr_queue.request_rows(TrRequest(
            request_name=f"qz_futures_investor_{investor_code}",
            tr_code="OPT50038",
            inputs={
                "일자구분": "1", "일자": trading_date.replace("-", ""),
                "투자자구분": investor_code, "수량금액구분": "1",
            },
            output_fields=FUTURES_INVESTOR_FIELDS, repeat=True,
        ))
        row = next((row for row in rows if row.get("종목코드", "").strip() == code), None)
        if row is None:
            raise ValueError(f"OPT50038 row for {code} is missing")
        return row

    def get_program_trading(self, trading_date: str) -> dict[str, str]:
        rows = self._tr_queue.request_rows(TrRequest(
            request_name="qz_kospi_program_trading", tr_code="OPT90005",
            inputs={
                "날짜": trading_date.replace("-", ""), "시간구분": "1",
                "금액수량구분": "1", "시장구분": "P00101",
                "분틱구분": "0", "거래소구분": "",
            },
            output_fields=PROGRAM_FIELDS, repeat=True,
        ))
        if not rows:
            raise ValueError("OPT90005 returned no rows")
        return rows[0]


class KiwoomDerivativesFlowCollector:
    name = "kiwoom_derivatives_flows"

    def __init__(
        self, resolver: FuturesContractResolver, data_source: DerivativesDataSource,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._resolver = resolver
        self._data_source = data_source
        self._clock = clock

    def collect(self, context: BriefingContext) -> dict[str, object]:
        collected_at = self._clock().isoformat()
        warnings: list[str] = []
        errors: list[str] = []
        resolution = self._resolver.resolve()
        futures = self._empty_futures(resolution, collected_at)
        if resolution.warning:
            warnings.append(resolution.warning)
            futures["warnings"].append(resolution.warning)  # type: ignore[union-attr]
        if resolution.status == "resolved" and resolution.code:
            self._collect_resolved_futures(
                futures, resolution.code, context.trading_date.isoformat(), errors
            )
        program = self._empty_program()
        try:
            raw = self._data_source.get_program_trading(context.trading_date.isoformat())
            program["raw"] = dict(raw)
            for key, prefix in (
                ("arbitrage", "차익거래"),
                ("non_arbitrage", "비차익거래"),
                ("total", "전체"),
            ):
                section = program[key]
                for output, suffix in (("sell", "매도"), ("buy", "매수"), ("net_buy", "순매수")):
                    field = prefix + suffix
                    section["raw"][field] = raw.get(field, "")
                    self._set_number(section, output, raw.get(field, ""), False)
        except Exception as exc:
            error = self._error("program trading", exc)
            program["warnings"].append(error)  # type: ignore[union-attr]
            errors.append(error)
        warnings.extend(futures["warnings"])  # type: ignore[arg-type]
        warnings.extend(program["warnings"])  # type: ignore[arg-type]
        return {
            "collector": self.name, "collected_at": collected_at,
            "kospi200_futures": futures, "program_trading": program,
            "warnings": list(dict.fromkeys(warnings)), "errors": errors,
        }

    def _collect_resolved_futures(
        self, futures: dict[str, object], code: str, trading_date: str,
        errors: list[str],
    ) -> None:
        try:
            raw = self._data_source.get_futures_quote(code)
            futures["raw"] = dict(raw)
            futures["name"] = raw.get("종목명", "").strip() or None
            for output, field, absolute, decimal in (
                ("current", "현재가", True, True), ("change", "전일대비", False, True),
                ("change_rate", "등락율", False, True), ("open", "시가", True, True),
                ("high", "고가", True, True), ("low", "저가", True, True),
                ("volume", "거래량", True, False),
                ("trading_value", "누적거래대금", True, False),
                ("open_interest", "미결제약정", True, False),
            ):
                self._set_number(futures, output, raw.get(field, ""), absolute, decimal)
        except Exception as exc:
            error = self._error("futures quote", exc)
            futures["warnings"].append(error)  # type: ignore[union-attr]
            errors.append(error)
        investors = futures["investors"]
        for key, name, investor_code in (
            ("foreign", "외국인", "09"), ("individual", "개인", "08"),
        ):
            try:
                raw = self._data_source.get_futures_investor(code, trading_date, investor_code)
                investors[key]["raw"] = dict(raw)  # type: ignore[index]
                self._set_number(
                    investors[key], "net_buy", raw.get("투자자별순매수수량", ""), False  # type: ignore[index]
                )
            except Exception as exc:
                error = self._error(f"futures investor {name}", exc)
                investors[key]["warnings"].append(error)  # type: ignore[index]
                errors.append(error)

    @staticmethod
    def _empty_futures(
        resolution: FuturesContractResolution, collected_at: str
    ) -> dict[str, object]:
        unavailable = "OPT50039 institution aggregate TR is documented as unavailable"
        return {
            "code": resolution.code, "name": None,
            "contract_resolution": resolution.__dict__, "collected_at": collected_at,
            "current": None, "change": None, "change_rate": None,
            "open": None, "high": None, "low": None, "volume": None,
            "trading_value": None, "open_interest": None,
            "investors": {
                "foreign": {"name": "외국인", "sell": None, "buy": None, "net_buy": None, "unit": "contracts", "raw": {}, "warnings": []},
                "individual": {"name": "개인", "sell": None, "buy": None, "net_buy": None, "unit": "contracts", "raw": {}, "warnings": []},
                "institution": {"name": "기관", "sell": None, "buy": None, "net_buy": None, "unit": "official scale unspecified", "raw": {}, "warnings": [unavailable]},
            },
            "raw": {}, "warnings": [unavailable],
        }

    @staticmethod
    def _empty_program() -> dict[str, object]:
        section = lambda: {"sell": None, "buy": None, "net_buy": None, "unit": "KRW million", "raw": {}}
        return {"market": "KOSPI", "arbitrage": section(), "non_arbitrage": section(), "total": section(), "raw": {}, "warnings": []}

    @staticmethod
    def _set_number(
        target: dict[str, object], key: str, raw: str, absolute: bool,
        decimal: bool = False,
    ) -> None:
        try:
            target[key] = (
                normalize_decimal(raw, absolute=absolute)
                if decimal else normalize_integer(raw, absolute=absolute)
            )
        except (TypeError, ValueError) as exc:
            target[key] = None
            warning = f"invalid {key}: {type(exc).__name__}: {exc}"
            target.setdefault("warnings", []).append(warning)  # type: ignore[union-attr]

    @staticmethod
    def _error(area: str, exc: Exception) -> str:
        return f"{area} failed: {type(exc).__name__}: {exc}"
