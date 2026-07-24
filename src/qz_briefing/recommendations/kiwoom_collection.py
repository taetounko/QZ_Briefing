"""Read-only Kiwoom adapters for recommendation data; no account or order TRs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Protocol

from qz_briefing.briefing.collectors import normalize_integer
from qz_briefing.briefing.leadership import DAILY_FIELDS
from qz_briefing.kiwoom.tr_requests import KiwoomTrRequestQueue, TrRequest

from .data_models import DataMetadata, DailyBar, InvestorFlowSnapshot, StockMasterRecord


MARKET_CODES = {"KOSPI": "0", "KOSDAQ": "10"}
FLOW_FIELDS = ("일자", "개인투자자", "외국인투자자", "기관계", "금융투자", "보험", "투신", "기타금융", "은행", "연기금등", "사모펀드", "국가", "기타법인", "내외국인")
ALLOWED_TR_CODES = {"OPT10081", "OPT10059"}


class MasterAdapter(Protocol):
    def get_code_list_by_market(self, market_code: str) -> list[str]: ...
    def get_master_code_name(self, code: str) -> str: ...
    def get_master_stock_state(self, code: str) -> str: ...
    def get_master_construction(self, code: str) -> str: ...
    def get_master_listed_stock_date(self, code: str) -> str: ...
    def get_master_last_price(self, code: str) -> str: ...
    def get_master_stock_info(self, code: str) -> str: ...


class KiwoomMasterDataSource:
    def __init__(self, adapter: MasterAdapter, *, security_type_resolver: Callable[[str,str],str], clock: Callable[[],datetime]=datetime.now) -> None:
        self._adapter=adapter; self._resolver=security_type_resolver; self._clock=clock

    def collect_market(self, market: str) -> list[StockMasterRecord]:
        now=self._clock(); output=[]
        for code in sorted(self._adapter.get_code_list_by_market(MARKET_CODES[market])):
            name=self._adapter.get_master_code_name(code); state=self._adapter.get_master_stock_state(code); construction=self._adapter.get_master_construction(code); info=self._adapter.get_master_stock_info(code)
            raw_listed=self._adapter.get_master_listed_stock_date(code); raw_price=self._adapter.get_master_last_price(code)
            try: listed=datetime.strptime(raw_listed,"%Y%m%d").date()
            except ValueError: listed=None
            reference=normalize_integer(raw_price,absolute=True)
            labels=_risk_labels("|".join((state,construction,info))); status="trading_halt" if "거래정지" in state else "normal"
            meta=DataMetadata(code,name,market,now,"Kiwoom local master",now,True,False,1.0)
            output.append(StockMasterRecord(meta,self._resolver(code,info),status=="normal",status,labels,listed,float(reference) if reference is not None else None,"|".join((state,construction,info))))
        return output


def _risk_labels(text: str) -> tuple[str,...]:
    mapping=(("관리","managed"),("투자주의","investment_caution"),("투자경고","investment_warning"),("투자위험","investment_risk"),("환기","ventilation"))
    return tuple(value for token,value in mapping if token in text)


class KiwoomDailyDataSource:
    def __init__(self, queue: KiwoomTrRequestQueue, clock: Callable[[],datetime]=datetime.now) -> None: self._queue=queue; self._clock=clock

    @staticmethod
    def request(code: str, target_date: date, max_pages: int=10) -> TrRequest:
        return TrRequest(f"qz_recommendation_daily_{code}","OPT10081",{"종목코드":code,"기준일자":target_date.strftime('%Y%m%d'),"수정주가구분":"1"},DAILY_FIELDS,repeat=True,paginate=True,max_pages=max_pages)

    def collect(self, master: StockMasterRecord, target_date: date) -> list[DailyBar]:
        rows=self._queue.request_rows(self.request(master.metadata.code,target_date)); now=self._clock(); output=[]
        for row in rows:
            raw_date=str(row.get("일자","")).strip()
            try: trading_date=datetime.strptime(raw_date,"%Y%m%d").date()
            except ValueError: continue
            values=[normalize_integer(row.get(key,""),absolute=True) for key in ("시가","고가","저가","현재가","거래량")]
            if any(value is None for value in values): continue
            trading_value=normalize_integer(row.get("거래대금",""),absolute=True)
            meta=DataMetadata(master.metadata.code,master.metadata.name,master.metadata.market,now,"Kiwoom OPT10081",now,True,False,1.0)
            output.append(DailyBar(meta,trading_date,*[float(value) for value in values],float(trading_value) if trading_value is not None else None,True))
        return sorted(output,key=lambda item:item.trading_date)


class KiwoomInvestorFlowDataSource:
    def __init__(self, queue: KiwoomTrRequestQueue, clock: Callable[[],datetime]=datetime.now) -> None: self._queue=queue; self._clock=clock

    @staticmethod
    def request(code: str, target_date: date, max_pages: int=3) -> TrRequest:
        return TrRequest(f"qz_recommendation_flow_{code}","OPT10059",{"일자":target_date.strftime('%Y%m%d'),"종목코드":code,"금액수량구분":"1","매매구분":"0","단위구분":"1"},FLOW_FIELDS,repeat=True,paginate=True,max_pages=max_pages)

    def collect(self, master: StockMasterRecord, target_date: date) -> InvestorFlowSnapshot:
        rows=self._queue.request_rows(self.request(master.metadata.code,target_date)); now=self._clock()
        foreign=[]; institution=[]; missing=False
        for row in sorted(rows,key=lambda value:str(value.get("일자",""))):
            foreign_value=normalize_integer(row.get("외국인투자자","")); institution_value=normalize_integer(row.get("기관계",""))
            if foreign_value is None or institution_value is None: missing=True; continue
            foreign.append(float(foreign_value)); institution.append(float(institution_value))
        meta=DataMetadata(master.metadata.code,master.metadata.name,master.metadata.market,now,"Kiwoom OPT10059 amount",now,True,missing,.8 if missing else 1.0,"missing investor fields" if missing else None)
        return InvestorFlowSnapshot(meta,tuple(foreign),tuple(institution))


def merge_daily_cache(cached: tuple[DailyBar,...], collected: list[DailyBar], *, keep: int=260) -> tuple[DailyBar,...]:
    adjusted={item.adjusted for item in (*cached,*collected)}
    if len(adjusted)>1: raise ValueError("수정주가 계열과 비수정주가 계열 혼합")
    merged={item.trading_date:item for item in cached}
    for item in collected: merged[item.trading_date]=item
    return tuple(merged[key] for key in sorted(merged)[-keep:])
