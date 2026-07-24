from datetime import date, datetime, timedelta

import pytest

from qz_briefing.__main__ import run
from qz_briefing.recommendations.collection_orchestrator import RecommendationCollectionOrchestrator, collection_paths, fixture_plan
from qz_briefing.recommendations.data_cache import RecommendationDataCache
from qz_briefing.recommendations.data_models import DataMetadata, DailyBar, InvestorFlowSnapshot, StockMasterRecord
from qz_briefing.recommendations.kiwoom_collection import (
    ALLOWED_TR_CODES, KiwoomDailyDataSource, KiwoomInvestorFlowDataSource,
    KiwoomMasterDataSource, merge_daily_cache,
)
from qz_briefing.recommendations.request_planner import CollectionMode


NOW=datetime(2026,7,24,16)


class MasterAdapter:
    def __init__(self): self.calls=[]
    def get_code_list_by_market(self,value): self.calls.append(("codes",value)); return ["800002","800001"]
    def get_master_code_name(self,code): self.calls.append(("name",code)); return "가상"+code[-1]
    def get_master_stock_state(self,code): return "투자경고" if code.endswith("2") else "정상"
    def get_master_construction(self,code): return "정상"
    def get_master_listed_stock_date(self,code): return "20200101"
    def get_master_last_price(self,code): return "1000"
    def get_master_stock_info(self,code): return "보통주"


class Queue:
    def __init__(self,rows): self.rows=rows; self.requests=[]
    def request_rows(self,request): self.requests.append(request); return list(self.rows)


def master():
    return StockMasterRecord(DataMetadata("800001","가상","KOSPI",NOW,"fixture",NOW),"common_stock")


def daily_row(day="20260724",close="+105",value="100000"):
    return {"종목코드":"800001","현재가":close,"거래량":"1000","거래대금":value,"일자":day,"시가":"100","고가":"110","저가":"95","전일종가":"99"}


def bar(day,value=100,adjusted=True):
    meta=DataMetadata("800001","가상","KOSPI",NOW,"fixture",NOW)
    return DailyBar(meta,day,value,value+1,value-1,value,1000,100000,adjusted)


@pytest.mark.parametrize(("market,market_code"),[("KOSPI","0"),("KOSDAQ","10")])
def test_master_market_conversion_uses_local_calls(market,market_code):
    adapter=MasterAdapter(); source=KiwoomMasterDataSource(adapter,security_type_resolver=lambda code,info:"common_stock",clock=lambda:NOW)
    records=source.collect_market(market)
    assert [item.metadata.code for item in records]==["800001","800002"]
    assert records[0].metadata.market==market and adapter.calls[0]==("codes",market_code)
    assert records[0].listed_date==date(2020,1,1) and records[0].reference_price==1000


def test_master_local_lookup_has_no_tr_queue_dependency():
    source=KiwoomMasterDataSource(MasterAdapter(),security_type_resolver=lambda code,info:"common_stock",clock=lambda:NOW)
    assert len(source.collect_market("KOSPI"))==2


def test_master_warning_becomes_risk_label_without_exclusion():
    source=KiwoomMasterDataSource(MasterAdapter(),security_type_resolver=lambda code,info:"common_stock",clock=lambda:NOW)
    warned=source.collect_market("KOSPI")[1]
    assert warned.tradable and warned.risk_labels==("investment_warning",)


def test_daily_request_uses_verified_adjusted_inputs_and_pagination():
    request=KiwoomDailyDataSource.request("800001",date(2026,7,24))
    assert request.tr_code=="OPT10081" and request.inputs=={"종목코드":"800001","기준일자":"20260724","수정주가구분":"1"}
    assert request.paginate and request.repeat


def test_daily_response_is_normalized_oldest_first():
    queue=Queue([daily_row("20260724"),daily_row("20260723","+103")])
    result=KiwoomDailyDataSource(queue,clock=lambda:NOW).collect(master(),date(2026,7,24))
    assert [item.trading_date for item in result]==[date(2026,7,23),date(2026,7,24)]
    assert result[-1].close==105 and result[-1].adjusted


def test_daily_missing_trading_value_stays_missing():
    result=KiwoomDailyDataSource(Queue([daily_row(value="")]),clock=lambda:NOW).collect(master(),date(2026,7,24))
    assert result[0].trading_value is None


def test_daily_cache_merge_replaces_duplicate_and_limits_history():
    cached=tuple(bar(date(2026,1,1)+timedelta(days=index),100+index) for index in range(260))
    changed=bar(cached[-1].trading_date,999); merged=merge_daily_cache(cached,[changed])
    assert len(merged)==260 and merged[-1].close==999


def test_daily_cache_merge_rejects_adjustment_mismatch():
    with pytest.raises(ValueError,match="수정주가"): merge_daily_cache((bar(date(2026,1,1)),),[bar(date(2026,1,2),adjusted=False)])


def test_flow_request_uses_verified_inputs_and_only_candidate_code():
    request=KiwoomInvestorFlowDataSource.request("800001",date(2026,7,24))
    assert request.tr_code=="OPT10059" and request.inputs=={"일자":"20260724","종목코드":"800001","금액수량구분":"1","매매구분":"0","단위구분":"1"}


def test_flow_values_are_signed_and_chronological():
    rows=[{"일자":"20260724","외국인투자자":"-1,200","기관계":"+500"},{"일자":"20260723","외국인투자자":"100","기관계":"-20"}]
    result=KiwoomInvestorFlowDataSource(Queue(rows),clock=lambda:NOW).collect(master(),date(2026,7,24))
    assert result.foreign_daily==(100.0,-1200.0) and result.institution_daily==(-20.0,500.0)


def test_missing_flow_fields_are_not_replaced_with_zero():
    result=KiwoomInvestorFlowDataSource(Queue([{"일자":"20260724","외국인투자자":"","기관계":"1"}]),clock=lambda:NOW).collect(master(),date(2026,7,24))
    assert not result.foreign_daily and result.metadata.missing


def test_market_program_tr_is_never_an_allowed_stock_tr(): assert "OPT90005" not in ALLOWED_TR_CODES


@pytest.mark.parametrize("mode",list(CollectionMode))
def test_fixture_orchestrator_supports_each_mode(mode):
    plan=fixture_plan(mode,max_symbols=5)
    expected=10 if mode is CollectionMode.BOOTSTRAP else 2
    assert plan.mode is mode and plan.universe_count==5 and plan.network_tr_requests==expected


def test_max_symbols_limits_dry_run(): assert fixture_plan(CollectionMode.BOOTSTRAP,max_symbols=3).universe_count==3


def test_validation_and_operational_cache_paths_are_separate(tmp_path):
    paths=collection_paths(tmp_path)
    assert paths.validation!=paths.operational and "validation" in paths.validation.parts


def test_plan_cli_never_starts_qax_network_or_telegram(capsys):
    def forbidden(*args,**kwargs): raise AssertionError("external call")
    assert run(["--plan-live-recommendation-collection","--max-symbols","5"],application_factory=forbidden,adapter_factory=forbidden,lock_factory=forbidden,notification_service_factory=forbidden)==0
    output=capsys.readouterr().out
    assert "ACTUAL_EXTERNAL_CALLS=0" in output and "PLAN: PASS" in output


def test_collect_dry_run_max_five_never_starts_external_runtime(capsys):
    def forbidden(*args,**kwargs): raise AssertionError("external call")
    assert run(["--collect-recommendation-data","--mode","bootstrap","--max-symbols","5","--dry-run"],application_factory=forbidden,adapter_factory=forbidden,lock_factory=forbidden,notification_service_factory=forbidden)==0
    assert "DRY RUN: PASS" in capsys.readouterr().out


def test_collect_without_dry_run_is_blocked(capsys):
    assert run(["--collect-recommendation-data","--mode","bootstrap","--max-symbols","5"])==2
    assert "COLLECTION BLOCKED" in capsys.readouterr().out


def test_collect_requires_mode_and_bounded_symbol_count():
    assert run(["--collect-recommendation-data","--max-symbols","5","--dry-run"])==2
    assert run(["--collect-recommendation-data","--mode","bootstrap","--max-symbols","6","--dry-run"])==2


def test_only_read_only_market_tr_codes_are_generated():
    assert ALLOWED_TR_CODES=={"OPT10081","OPT10059"}


class OrchestratorMaster:
    def collect_market(self,market):
        code="810001" if market=="KOSPI" else "810002"
        return [StockMasterRecord(DataMetadata(code,"가상",market,NOW,"fixture",NOW),"common_stock")]


class OrchestratorDaily:
    def __init__(self,fail=None): self.fail=fail
    def collect(self,item,target):
        if item.metadata.code==self.fail: raise RuntimeError("fixture daily failure")
        output=[]; day=date(2026,1,1); value=100
        while len(output)<130:
            if day.weekday()<5:
                meta=DataMetadata(item.metadata.code,item.metadata.name,item.metadata.market,NOW,"fixture",NOW)
                output.append(DailyBar(meta,day,value,value+2,value-1,value+1,1000,100000,True)); value+=1
            day+=timedelta(days=1)
        return output


class OrchestratorFlow:
    def __init__(self): self.codes=[]
    def collect(self,item,target):
        self.codes.append(item.metadata.code)
        return InvestorFlowSnapshot(item.metadata,(1,2,3),(2,3,4))


@pytest.mark.parametrize("mode",list(CollectionMode))
def test_orchestrator_runs_price_filter_flow_and_engine_for_each_mode(mode):
    flow=OrchestratorFlow(); orchestrator=RecommendationCollectionOrchestrator(OrchestratorMaster(),OrchestratorDaily(),flow,clock=lambda:NOW)
    result=orchestrator.run(mode,date(2026,7,24))
    assert result.preliminary_count==2 and result.flow_count==2
    assert flow.codes==["810001","810002"] and result.external_calls==0


def test_orchestrator_isolates_one_symbol_failure_and_continues():
    flow=OrchestratorFlow(); orchestrator=RecommendationCollectionOrchestrator(OrchestratorMaster(),OrchestratorDaily("810001"),flow,clock=lambda:NOW)
    result=orchestrator.run(CollectionMode.BOOTSTRAP,date(2026,7,24))
    assert result.failures[0].code=="810001" and flow.codes==["810002"]


def test_orchestrator_atomically_saves_validation_snapshots_and_checkpoint(tmp_path):
    cache=RecommendationDataCache(tmp_path/"validation")
    orchestrator=RecommendationCollectionOrchestrator(OrchestratorMaster(),OrchestratorDaily(),OrchestratorFlow(),cache=cache,clock=lambda:NOW)
    orchestrator.run(CollectionMode.BOOTSTRAP,date(2026,7,24),max_symbols=1)
    for kind,key in (("master","universe"),("daily","810001"),("weekly","810001"),("features","810001"),("flow","810001"),("snapshots","2026-07-24"),("failures","2026-07-24"),("checkpoints","bootstrap")):
        assert cache.path(kind,key).is_file()
    assert not list((tmp_path/"validation").rglob("*.tmp"))
