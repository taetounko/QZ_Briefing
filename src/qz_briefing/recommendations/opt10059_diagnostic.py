"""Single-symbol, cache-seeded OPT10059 live diagnostic."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from qz_briefing.briefing.collectors import normalize_integer
from qz_briefing.kiwoom.qax_adapter import KiwoomQAxAdapter
from qz_briefing.kiwoom.tr_requests import KiwoomTrInputError, KiwoomTrRequestQueue
from qz_briefing.runtime.unattended import atomic_write_json

from .collection_orchestrator import collection_paths
from .kiwoom_collection import FLOW_FIELDS, KiwoomInvestorFlowDataSource
from .live_validation import _ensure_connected
from .fund_flow import compute_fund_flow_score


OPT10059_ALLOWED = {
    "금액수량구분": {"1", "2"},
    "매매구분": {"0", "1", "2"},
    "단위구분": {"1", "1000"},
}


def validate_opt10059_inputs(inputs: dict[str, str], *, today: date) -> list[str]:
    errors=[]
    required=("일자","종목코드",*OPT10059_ALLOWED)
    for field in required:
        if not inputs.get(field): errors.append(f"missing:{field}")
    try: reference=datetime.strptime(inputs.get("일자",""),"%Y%m%d").date()
    except ValueError: errors.append("invalid:일자")
    else:
        if reference>today: errors.append("future:일자")
    if not re.fullmatch(r"\d{6}",inputs.get("종목코드","")): errors.append("invalid:종목코드")
    for field,allowed in OPT10059_ALLOWED.items():
        if inputs.get(field) not in allowed: errors.append(f"invalid:{field}")
    return errors


def _cached_candidate(root: Path) -> tuple[str,str]:
    path=root/"snapshots"/"live_summary.json"
    payload=json.loads(path.read_text(encoding="utf-8"))["data"]
    metrics=payload.get("daily_metrics",{})
    passing=sorted((code,value) for code,value in metrics.items() if value.get("weekly",{}).get("weekly_close_above_ma5"))
    if not passing: raise RuntimeError("no cached MA5-pass candidate")
    code,value=passing[0]
    return code,str(value["latest"]).replace("-","")


def cached_candidates(root:Path,limit:int=3)->list[str]:
    payload=json.loads((root/"snapshots"/"live_summary.json").read_text(encoding="utf-8"))["data"]
    return sorted(code for code,value in payload.get("daily_metrics",{}).items() if value.get("weekly",{}).get("weekly_close_above_ma5"))[:limit]


def run_cached_opt10059_candidates(project_root:Path)->dict[str,object]:
    from PyQt5.QtWidgets import QApplication
    root=collection_paths(project_root).validation/"live_collection"
    codes=cached_candidates(root,3)
    app=QApplication.instance() or QApplication([]); app.setQuitOnLastWindowClosed(False)
    adapter=KiwoomQAxAdapter(); queue=None; results=[]
    try:
        if not _ensure_connected(adapter): raise RuntimeError("KIWOOM DISCONNECTED")
        queue=KiwoomTrRequestQueue(adapter)
        summary=json.loads((root/"snapshots"/"live_summary.json").read_text(encoding="utf-8"))["data"]
        for code in codes:
            reference=str(summary["daily_metrics"][code]["latest"]).replace("-","")
            request=KiwoomInvestorFlowDataSource.request(code,datetime.strptime(reference,"%Y%m%d").date())
            base={"symbol":code,"reference_date":reference,"tr_code":"opt10059","rq_name":request.request_name,"prev_next":0,"inputs":dict(request.inputs),"order_account_tr_requests":0,"telegram_sends":0,"operational_cache_writes":0}
            try:
                rows=queue.request_rows(request)
                present=[field for field in FLOW_FIELDS if any(str(row.get(field,"")).strip() for row in rows)]
                scored=scored_flow_rows(root,code,rows)
                item={**base,"status":"PASS" if len(rows)>=5 else "INSUFFICIENT_ROWS","comm_rq_data_return_code":0,"response_rows":len(rows),"actual_output_fields":present,"missing_output_fields":[field for field in FLOW_FIELDS if field not in present],"metrics":signed_flow_metrics(rows),"fund_flow":scored.__dict__}
            except KiwoomTrInputError as exc:
                item={**base,"status":"INPUT_ERROR","comm_rq_data_return_code":-300,"error":str(exc),"response_rows":0}
            results.append(item)
            atomic_write_json(root/"diagnostics"/f"opt10059_{code}.json",item)
    finally:
        if queue is not None: queue.close()
        adapter.close()
    return {"requested":len(results),"success":sum(item.get("status")=="PASS" for item in results),"failed":sum(item.get("status")!="PASS" for item in results),"continuations":sum(int(item.get("continuation_count",0)) for item in results),"overload_retries":sum(int(item.get("retry_count",0)) for item in results),"results":results,"order_account_tr_requests":0,"telegram_sends":0,"operational_cache_writes":0}


def signed_flow_metrics(rows: list[dict[str,str]]) -> dict[str,object]:
    normalized=[]; seen=set()
    for row in rows:
        day=str(row.get("일자","")).strip()
        if not re.fullmatch(r"\d{8}",day) or day in seen: continue
        seen.add(day)
        foreign=normalize_integer(row.get("외국인투자자",""))
        institution=normalize_integer(row.get("기관계",""))
        normalized.append((day,foreign,institution))
    normalized.sort(reverse=True)
    def values(index:int,limit:int): return [item[index] for item in normalized[:limit] if item[index] is not None]
    return {
        "row_count":len(normalized),
        "first_date":normalized[0][0] if normalized else None,
        "last_date":normalized[-1][0] if normalized else None,
        "foreign_5":sum(values(1,5)) if len(values(1,5))==5 else None,
        "foreign_20":sum(values(1,20)) if len(values(1,20))==20 else None,
        "institution_5":sum(values(2,5)) if len(values(2,5))==5 else None,
        "institution_20":sum(values(2,20)) if len(values(2,20))==20 else None,
    }


def _cached_average_trading_value(root:Path,code:str)->float:
    payload=json.loads((root/"daily"/f"{code}.json").read_text(encoding="utf-8"))["data"]
    values=[float(item["trading_value"]) for item in payload[-20:] if item.get("trading_value") is not None]
    return sum(values)/len(values) if values else 0.0


def scored_flow_rows(root:Path,code:str,rows:list[dict[str,str]]):
    ordered=sorted(rows,key=lambda row:str(row.get("일자","")))
    return compute_fund_flow_score([row.get("외국인투자자") for row in ordered],[row.get("기관계") for row in ordered],_cached_average_trading_value(root,code))


def run_opt10059_diagnostic(project_root:Path, symbol:str|None=None) -> dict[str,object]:
    from PyQt5.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([]); app.setQuitOnLastWindowClosed(False)
    root=collection_paths(project_root).validation/"live_collection"
    (root/"diagnostics").mkdir(parents=True,exist_ok=True)
    cached_symbol,reference=_cached_candidate(root)
    symbol=symbol or cached_symbol
    if symbol!=cached_symbol and not (root/"daily"/f"{symbol}.json").exists(): raise RuntimeError("symbol has no validation daily cache")
    if symbol!=cached_symbol:
        daily=json.loads((root/"daily"/f"{symbol}.json").read_text(encoding="utf-8"))["data"]
        reference=max(str(item["trading_date"]).replace("-","") for item in daily)
    inputs={"일자":reference,"종목코드":symbol,"금액수량구분":"1","매매구분":"0","단위구분":"1"}
    errors=validate_opt10059_inputs(inputs,today=datetime.now().date())
    result={"symbol":symbol,"reference_date":reference,"tr_code":"opt10059","rq_name":f"qz_f_{symbol}","screen_number":"allocated_four_digit","prev_next":0,"inputs":inputs,"expected_request_count":1,"order_account_tr_requests":0,"telegram_sends":0,"operational_cache_writes":0}
    if errors:
        result.update({"status":"LOCAL_INPUT_VALIDATION_FAILED","input_errors":errors,"comm_rq_data_return_code":None}); return result
    adapter=KiwoomQAxAdapter(); queue=None
    try:
        if not _ensure_connected(adapter): raise RuntimeError("KIWOOM DISCONNECTED")
        queue=KiwoomTrRequestQueue(adapter)
        request=KiwoomInvestorFlowDataSource.request(symbol,datetime.strptime(reference,"%Y%m%d").date())
        try: rows=queue.request_rows(request)
        except KiwoomTrInputError as exc:
            result.update({"status":"INPUT_ERROR","comm_rq_data_return_code":-300,"error":str(exc),"continuation_count":0,"retry_count":0}); return result
        present=[field for field in FLOW_FIELDS if any(str(row.get(field,"")).strip() for row in rows)]
        metrics=signed_flow_metrics(rows); scored=scored_flow_rows(root,symbol,rows)
        result.update({"status":"PASS" if len(rows)>=5 else "INSUFFICIENT_ROWS","comm_rq_data_return_code":0,"response_rows":len(rows),"actual_output_fields":present,"missing_output_fields":[field for field in FLOW_FIELDS if field not in present],"metrics":metrics,"fund_flow":scored.__dict__,"continuation_count":queue.progress["retry_dispatch_count"],"retry_count":queue.progress["overload_count"]})
        return result
    finally:
        if queue is not None: queue.close()
        adapter.close()
        atomic_write_json(root/"diagnostics"/f"opt10059_{symbol}.json",result)


def print_opt10059_diagnostic(result:dict[str,object])->bool:
    for key in ("symbol","reference_date","tr_code","rq_name","screen_number","prev_next","inputs","expected_request_count","comm_rq_data_return_code","status","response_rows","actual_output_fields","missing_output_fields","metrics","continuation_count","retry_count","order_account_tr_requests","telegram_sends","operational_cache_writes"):
        print(f"{key.upper()}={result.get(key,'NOT_REACHED')}")
    return result.get("status")=="PASS"
