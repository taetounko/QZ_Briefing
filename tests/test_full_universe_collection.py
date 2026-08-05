from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import hashlib
import signal
from pathlib import Path

import pytest

from qz_briefing.__main__ import parse_cli_arguments, run
from qz_briefing.recommendations.data_models import DataMetadata, StockMasterRecord
from qz_briefing.recommendations.full_universe_collection import (
    CollectionProgress, FullCollectionSession, deterministic_universe, protected_validation_root,
    failed_flow_repair_targets, failed_price_repair_targets, remaining_counts,
    run_full_collection_live, run_full_collection_plan,
    select_balanced_universe, select_flow_targets, should_abort_for_failures,
    validate_cross_session_cache, validate_full_collection_session, validate_live_scope, validate_scope,
)
from qz_briefing.recommendations import full_universe_collection as full_collection_module
from qz_briefing.kiwoom import KiwoomTrTimeoutError
from qz_briefing.recommendations.request_planner import PreliminaryCandidate
from qz_briefing.recommendations.partitioned_full_universe import (
    ParentSymbol, apply_mock_batch, choose_flow_candidates, create_parent_collection,
    deterministic_parent_universe, finalize_mock_report, finalize_price_phase,
    load_universe_snapshot, next_batch, verify_parent_collection,
)
from qz_briefing.recommendations.full_universe_snapshot import (
    build_full_universe_snapshot, save_snapshot, snapshot_payload,
)


def master(code: str, market: str = "KOSPI", *, security_type: str = "common_stock") -> StockMasterRecord:
    now = datetime(2026, 7, 26, 10)
    return StockMasterRecord(DataMetadata(code, code, market, now, "fixture", now), security_type)


def forbidden(*args, **kwargs):
    raise AssertionError("external runtime must not be constructed")


def test_validation_path_is_strictly_protected(tmp_path: Path):
    expected = tmp_path / "data/validation/recommendations/full_collection"
    assert protected_validation_root(tmp_path) == expected.resolve()
    with pytest.raises(ValueError):
        protected_validation_root(tmp_path, tmp_path / "data/recommendations")


def test_universe_is_filtered_deduplicated_and_deterministic():
    rows = [master("000002", "KOSDAQ"), master("000001"), master("000002", "KOSDAQ"), master("000003", security_type="etf")]
    assert [(row.metadata.market, row.metadata.code) for row in deterministic_universe(rows)] == [("KOSPI", "000001"), ("KOSDAQ", "000002")]


def test_scope_requires_bounded_limit_or_explicit_full_confirmation():
    assert validate_scope(20, 5, False) == 5
    assert validate_scope(20, None, True) == 20
    with pytest.raises(ValueError): validate_scope(20, None, False)
    with pytest.raises(ValueError): validate_scope(20, 21, False)


def test_flow_candidates_are_deterministic_and_capped_at_120():
    candidates = [PreliminaryCandidate(f"7{i:05d}", 100, .9, True, True, 1, 1_000_000-i) for i in range(140)]
    first = select_flow_targets(list(reversed(candidates)))
    assert len(first) == 120
    assert first == select_flow_targets(candidates)


def test_failure_threshold_is_bounded_and_configurable():
    assert not should_abort_for_failures(1, 8)
    assert should_abort_for_failures(7, 3)
    assert not should_abort_for_failures(8, 2)


def test_session_checkpoint_is_atomic_and_resume_skips_completed(tmp_path: Path):
    session = FullCollectionSession(tmp_path, "session", clock=lambda: datetime(2026, 7, 26, 10))
    progress = session.create([{"market": "KOSPI", "code": "000001"}, {"market": "KOSDAQ", "code": "000002"}], mode="dry-run", symbol_limit=2)
    assert all((session.path / name).exists() for name in ("session.json", "universe.json", "plan.json", "progress.json", "failures.json"))
    progress.price_completed_codes.append("000001")
    session.checkpoint(progress)
    assert session.pending_price_codes() == ["000002"]
    assert not list(session.path.glob(".*.tmp"))
    assert json.loads((session.path / "progress.json").read_text(encoding="utf-8"))["estimated_remaining"] == 1


def test_restart_never_overwrites_an_existing_session(tmp_path: Path):
    session = FullCollectionSession(tmp_path, "same")
    session.create([{"market": "KOSPI", "code": "000001"}], mode="dry-run", symbol_limit=1)
    with pytest.raises(ValueError):
        session.create([{"market": "KOSPI", "code": "000001"}], mode="dry-run", symbol_limit=1, restart=True)


def test_offline_validation_cli_never_constructs_runtime(capsys):
    assert run(["--validate-full-universe-collection"], application_factory=forbidden, adapter_factory=forbidden) == 0
    output = capsys.readouterr().out
    assert "FULL UNIVERSE COLLECTION VALIDATION: PASS" in output
    assert "EXTERNAL_CALLS=0" in output


def test_dry_run_cli_creates_only_validation_session(tmp_path: Path, capsys, monkeypatch):
    root = tmp_path / "data/validation/recommendations/full_collection"
    from qz_briefing.recommendations import full_universe_collection as module
    original = module.run_full_collection_plan
    monkeypatch.setattr(module, "run_full_collection_plan", lambda _project_root, **kwargs: original(tmp_path, **kwargs))
    assert run(["--collect-recommendation-universe", "--dry-run", "--max-symbols", "20", "--validation-root", str(root)], application_factory=forbidden, adapter_factory=forbidden) == 0
    output = capsys.readouterr().out
    assert "SYMBOL_LIMIT=20" in output
    assert "LIVE_TR_CALLS=0" in output and "ORDER_ACCOUNT_TR=0" in output
    assert not (tmp_path / "data/recommendations").exists()


def test_collection_cli_requires_full_universe_confirmation(tmp_path: Path, capsys, monkeypatch):
    from qz_briefing.recommendations import full_universe_collection as module
    original = module.run_full_collection_plan
    monkeypatch.setattr(module, "run_full_collection_plan", lambda _project_root, **kwargs: original(tmp_path, **kwargs))
    common = ["--collect-recommendation-universe", "--validation-root", str(tmp_path / "data/validation/recommendations/full_collection")]
    assert run(common + ["--dry-run"], application_factory=forbidden, adapter_factory=forbidden, manager_factory=forbidden, tr_queue_factory=forbidden) == 2
    assert "full collection requires --full-universe-confirmed" in capsys.readouterr().out


def test_collection_cli_blocks_unsafe_modes(tmp_path: Path, capsys, monkeypatch):
    from qz_briefing.recommendations import full_universe_collection as module
    original = module.run_full_collection_plan
    monkeypatch.setattr(module, "run_full_collection_plan", lambda _project_root, **kwargs: original(tmp_path, **kwargs))
    monkeypatch.setenv("CODEX_THREAD_ID", "test-thread")
    common = ["--collect-recommendation-universe", "--validation-root", str(tmp_path / "data/validation/recommendations/full_collection")]
    assert run(
        common + ["--allow-kiwoom-live", "--max-symbols", "20"],
        application_factory=forbidden,
        adapter_factory=forbidden,
        manager_factory=forbidden,
        tr_queue_factory=forbidden,
    ) == 2
    assert "blocked inside Codex" in capsys.readouterr().out


def test_live_scope_is_explicit_and_preserves_twenty_without_confirmation():
    assert validate_live_scope(20) == 20
    with pytest.raises(ValueError): validate_live_scope(None)
    with pytest.raises(ValueError): validate_live_scope(21)


def test_live_selection_balances_ten_per_market_and_backfills_short_market():
    records = [master(f"1{index:05d}", "KOSPI") for index in range(12)] + [master(f"2{index:05d}", "KOSDAQ") for index in range(12)]
    selected = select_balanced_universe(reversed(records), 20)
    assert sum(row.metadata.market == "KOSPI" for row in selected) == 10
    assert sum(row.metadata.market == "KOSDAQ" for row in selected) == 10
    short = select_balanced_universe(records[:3] + records[12:], 20)
    assert len(short) == 15 and sum(row.metadata.market == "KOSPI" for row in short) == 3


def test_validation_reports_mock_live_adapter_contract(capsys):
    assert run(["--validate-full-universe-collection"]) == 0
    output = capsys.readouterr().out
    assert "FULL UNIVERSE LIVE ADAPTER VALIDATION: PASS" in output


class LiveAdapter:
    def __init__(self): self.closed = False
    def get_connect_state(self): return 1
    def get_code_list_by_market(self, market):
        prefix = "1" if market == "0" else "2"
        return [f"{prefix}{index:05d}" for index in range(12)]
    def get_master_code_name(self, code): return f"가상-{code}"
    def get_master_stock_state(self, code): return "정상"
    def get_master_construction(self, code): return "정상"
    def get_master_listed_stock_date(self, code): return "20200101"
    def get_master_last_price(self, code): return "1000"
    def get_master_stock_info(self, code): return "보통주"
    def close(self): self.closed = True


class LiveSignal:
    def __init__(self): self.callbacks=[]
    def connect(self, callback): self.callbacks.append(callback)
    def emit(self):
        for callback in list(self.callbacks): callback()


class LiveApplication:
    def __init__(self):
        self.quit_on_close = True
        self.lastWindowClosed=LiveSignal(); self.aboutToQuit=LiveSignal()
    def setQuitOnLastWindowClosed(self, value): self.quit_on_close = value


class LiveManager:
    def __init__(self, adapter): self.adapter=adapter; self.stopped=False
    def stop(self): self.stopped=True


class LiveQueue:
    def __init__(self, adapter): self.requests = []; self.closed = False
    def request_rows(self, request):
        self.requests.append((request.tr_code.upper(), request.inputs["종목코드"]))
        if request.tr_code.upper() == "OPT10059":
            return [{"일자":"20260724","외국인투자자":"100","기관계":"200"}]
        days=[]; current=date(2026,7,24)
        while len(days)<130:
            if current.weekday()<5: days.append(current)
            current-=timedelta(days=1)
        rows=[]
        for index, day in enumerate(reversed(days), 1):
            close=100+index
            rows.append({"일자":day.strftime("%Y%m%d"),"시가":str(close-1),"고가":str(close+1),"저가":str(close-2),"현재가":str(close),"거래량":"1000","거래대금":"100000"})
        return rows
    def close(self): self.closed = True


def test_mock_live_adapter_collects_balanced_twenty_and_saves_report(tmp_path, capsys):
    adapter=LiveAdapter(); queues=[]
    def make_queue(value):
        queue=LiveQueue(value); queues.append(queue); return queue
    result=run_full_collection_live(tmp_path,max_symbols=20,clock=lambda:datetime(2026,7,24,16),application_factory=lambda _:LiveApplication(),adapter_factory=lambda:adapter,manager_factory=LiveManager,queue_factory=make_queue,connected=lambda _:True)
    assert result == 0 and adapter.closed and queues[0].closed
    output=capsys.readouterr().out
    assert "KOSPI_SELECTED=10" in output and "KOSDAQ_SELECTED=10" in output
    assert "ORDER_ACCOUNT_TR=0" in output and "TELEGRAM_SENDS=0" in output and "DASHBOARD_STARTED=0" in output
    sessions=list((tmp_path/"data/validation/recommendations/full_collection").iterdir())
    assert len(sessions)==1 and (sessions[0]/"reports/recommendations.json").is_file()
    assert len([call for call in queues[0].requests if call[0]=="OPT10081"])==20
    assert "QAPPLICATION_READY=1" in output and "DASHBOARD_STARTED=0" in output


def test_live_creation_order_application_adapter_connection_login_collection(tmp_path):
    events=[]; adapter=LiveAdapter()
    class OrderedApplication(LiveApplication):
        def setQuitOnLastWindowClosed(self, value):
            events.append("application.disable_quit_on_last_window_closed")
            super().setQuitOnLastWindowClosed(value)
    app=OrderedApplication()
    class OrderedQueue(LiveQueue):
        def request_rows(self, request):
            if "collection" not in events: events.append("collection")
            return super().request_rows(request)
    def create_app(_): events.append("application.create"); return app
    def create_adapter():
        assert events == ["application.create","application.disable_quit_on_last_window_closed"]
        events.append("adapter.create"); return adapter
    def create_manager(value): events.append("connection.create"); return LiveManager(value)
    def login(value): events.append("login"); return True
    assert run_full_collection_live(tmp_path,max_symbols=1,clock=lambda:datetime(2026,7,24,16),application_factory=create_app,adapter_factory=create_adapter,manager_factory=create_manager,queue_factory=OrderedQueue,connected=login)==0
    assert events[:6]==["application.create","application.disable_quit_on_last_window_closed","adapter.create","connection.create","login","collection"]
    assert app.quit_on_close is False


def test_application_factory_reuses_existing_instance_without_duplicate(tmp_path):
    existing=LiveApplication(); creations=[]
    def official_style_factory(_):
        if not creations: creations.append(existing)
        return creations[0]
    assert run_full_collection_live(tmp_path,max_symbols=1,clock=lambda:datetime(2026,7,24,16),application_factory=official_style_factory,adapter_factory=LiveAdapter,manager_factory=LiveManager,queue_factory=LiveQueue,connected=lambda _:True)==0
    assert creations == [existing]


def test_dry_run_and_offline_validation_never_create_qapplication_or_adapter(tmp_path):
    def forbidden(*args,**kwargs): raise AssertionError("Qt runtime constructed")
    root=tmp_path/"data/validation/recommendations/full_collection"
    assert run_full_collection_plan(tmp_path,dry_run=True,cached_only=False,allow_live=False,max_symbols=20,full_universe_confirmed=False,validation_root=root,application_factory=forbidden,adapter_factory=forbidden)==0
    assert run(["--validate-full-universe-collection"],application_factory=forbidden,adapter_factory=forbidden,dashboard_factory=forbidden)==0


def test_application_or_adapter_initialization_failure_makes_no_tr_calls(tmp_path):
    calls=[]
    with pytest.raises(RuntimeError,match="QApplication initialization failed"):
        run_full_collection_live(tmp_path,max_symbols=1,application_factory=lambda _:(_ for _ in ()).throw(RuntimeError("fixture")),adapter_factory=lambda:calls.append("adapter"))
    assert calls == []
    with pytest.raises(RuntimeError,match="Kiwoom adapter initialization failed"):
        run_full_collection_live(tmp_path,max_symbols=1,application_factory=lambda _:LiveApplication(),adapter_factory=lambda:(_ for _ in ()).throw(RuntimeError("fixture")),queue_factory=lambda _:calls.append("CommRqData"))
    assert calls == []


def test_login_failure_saves_safe_checkpoint_without_collection(tmp_path):
    queues=[]
    with pytest.raises(ValueError,match="GetConnectState"):
        run_full_collection_live(tmp_path,max_symbols=1,clock=lambda:datetime(2026,7,24,16),application_factory=lambda _:LiveApplication(),adapter_factory=LiveAdapter,manager_factory=LiveManager,queue_factory=lambda value:queues.append(value),connected=lambda _:False)
    assert queues == []
    sessions=list((tmp_path/"data/validation/recommendations/full_collection").iterdir())
    progress=json.loads((sessions[0]/"progress.json").read_text(encoding="utf-8"))
    assert progress["phase"]=="startup_failed"
    assert progress["shutdown_reason"]=="login_failed"


def test_last_window_closed_and_about_to_quit_do_not_stop_collection(tmp_path, capsys):
    app=LiveApplication(); queue=[]
    def login(_):
        app.lastWindowClosed.emit(); app.aboutToQuit.emit()
        return True
    assert run_full_collection_live(tmp_path,max_symbols=1,clock=lambda:datetime(2026,7,24,16),application_factory=lambda _:app,adapter_factory=LiveAdapter,manager_factory=LiveManager,queue_factory=lambda value:(queue.append(LiveQueue(value)) or queue[0]),connected=login)==0
    output=capsys.readouterr().out
    assert "QT_LAST_WINDOW_CLOSED_IGNORED=1" in output
    assert "QT_ABOUT_TO_QUIT_IGNORED=1" in output
    assert "SHUTDOWN_REASON=completed" in output
    assert "shutdown requested by user" not in output
    assert queue[0].requests


def test_non_sigint_keyboard_interrupt_after_login_window_close_is_ignored(tmp_path, capsys):
    adapter=LiveAdapter()
    def login(_): raise KeyboardInterrupt
    assert run_full_collection_live(tmp_path,max_symbols=1,clock=lambda:datetime(2026,7,24,16),application_factory=lambda _:LiveApplication(),adapter_factory=lambda:adapter,manager_factory=LiveManager,queue_factory=LiveQueue,connected=login)==0
    output=capsys.readouterr().out
    assert "QT_LAST_WINDOW_CLOSED_IGNORED=1" in output and "SHUTDOWN_REASON=completed" in output


def test_real_sigint_records_user_interrupt_and_atomic_checkpoint(tmp_path, capsys):
    class InterruptQueue(LiveQueue):
        def request_rows(self, request):
            signal.raise_signal(signal.SIGINT)
            raise AssertionError("SIGINT handler did not interrupt")
    result=run_full_collection_live(tmp_path,max_symbols=1,clock=lambda:datetime(2026,7,24,16),application_factory=lambda _:LiveApplication(),adapter_factory=LiveAdapter,manager_factory=LiveManager,queue_factory=InterruptQueue,connected=lambda _:True)
    assert result==130
    output=capsys.readouterr().out
    assert "SHUTDOWN_REASON=user_interrupt" in output and "RESUME_AVAILABLE=1" in output
    sessions=list((tmp_path/"data/validation/recommendations/full_collection").iterdir())
    progress=json.loads((sessions[0]/"progress.json").read_text(encoding="utf-8"))
    assert progress["phase"]=="interrupted" and progress["shutdown_reason"]=="user_interrupt"
    assert not list(sessions[0].rglob("*.tmp"))


def test_connection_loss_has_distinct_reason_and_checkpoint(tmp_path, capsys):
    adapter=LiveAdapter(); adapter.connected=True
    adapter.get_connect_state=lambda: 1 if adapter.connected else 0
    class DisconnectQueue(LiveQueue):
        def request_rows(self, request):
            adapter.connected=False
            raise RuntimeError("fixture disconnected")
    assert run_full_collection_live(tmp_path,max_symbols=1,clock=lambda:datetime(2026,7,24,16),application_factory=lambda _:LiveApplication(),adapter_factory=lambda:adapter,manager_factory=LiveManager,queue_factory=DisconnectQueue,connected=lambda _:True)==1
    assert "SHUTDOWN_REASON=connection_lost" in capsys.readouterr().out
    session=next((tmp_path/"data/validation/recommendations/full_collection").iterdir())
    progress=json.loads((session/"progress.json").read_text(encoding="utf-8"))
    assert progress["shutdown_reason"]=="connection_lost"


def test_live_stage_confirmation_rules_and_cli_option():
    assert validate_live_scope(20, False)==20
    with pytest.raises(ValueError,match="confirm_100_symbol_live_required"): validate_live_scope(21, False)
    assert validate_live_scope(21, True)==21
    assert validate_live_scope(100, True)==100
    with pytest.raises(ValueError,match="confirm_500_symbol_live_required"): validate_live_scope(101, True)
    assert validate_live_scope(101, False, True)==101
    assert validate_live_scope(500, False, True)==500
    with pytest.raises(ValueError,match="confirm_500_symbol_live_required"): validate_live_scope(500, True, False)
    with pytest.raises(ValueError,match="max_symbols_exceeds_current_live_stage"): validate_live_scope(501, True, True)
    parsed=parse_cli_arguments(["--collect-recommendation-universe","--allow-kiwoom-live","--max-symbols","100","--confirm-100-symbol-live"])
    assert parsed.confirm_100_symbol_live
    parsed_500=parse_cli_arguments(["--collect-recommendation-universe","--allow-kiwoom-live","--max-symbols","500","--confirm-500-symbol-live"])
    assert parsed_500.confirm_500_symbol_live


def test_live_stage_blocks_before_qt_or_tr_even_with_full_confirmation(tmp_path):
    calls=[]
    def forbidden(*args,**kwargs): calls.append("external"); raise AssertionError("external")
    with pytest.raises(ValueError,match="confirm_100_symbol_live_required"):
        run_full_collection_plan(tmp_path,dry_run=False,cached_only=False,allow_live=True,max_symbols=100,full_universe_confirmed=False,application_factory=forbidden,adapter_factory=forbidden)
    with pytest.raises(ValueError,match="confirm_500_symbol_live_required"):
        run_full_collection_plan(tmp_path,dry_run=False,cached_only=False,allow_live=True,max_symbols=101,full_universe_confirmed=True,confirm_100_symbol_live=True,application_factory=forbidden,adapter_factory=forbidden)
    with pytest.raises(ValueError,match="confirm_500_symbol_live_required"):
        run_full_collection_plan(tmp_path,dry_run=False,cached_only=False,allow_live=True,max_symbols=500,full_universe_confirmed=True,confirm_100_symbol_live=True,application_factory=forbidden,adapter_factory=forbidden)
    with pytest.raises(ValueError,match="max_symbols_exceeds_current_live_stage"):
        run_full_collection_plan(tmp_path,dry_run=False,cached_only=False,allow_live=True,max_symbols=501,full_universe_confirmed=True,confirm_100_symbol_live=True,confirm_500_symbol_live=True,application_factory=forbidden,adapter_factory=forbidden)
    assert calls==[]


def test_five_hundred_plan_records_confirmation_tier_and_market_counts(tmp_path):
    universe=[{"market":"KOSPI","code":f"1{i:05d}"} for i in range(250)]+[{"market":"KOSDAQ","code":f"2{i:05d}"} for i in range(250)]
    session=FullCollectionSession(tmp_path,"five-hundred",clock=lambda:datetime(2026,8,1,10))
    session.create(universe,mode="live_validation",symbol_limit=500,confirmed_500=True,target_date=date(2026,8,1))
    plan=json.loads((session.path/"plan.json").read_text(encoding="utf-8"))
    assert plan["confirmation_tier"]==500 and plan["confirm_500_symbol_live"] is True
    assert plan["market_counts"]=={"KOSPI":250,"KOSDAQ":250} and len(plan["selected_symbols"])==500
    assert plan["generated_at"] and "per-symbol" in plan["cache_compatibility"]


def test_balanced_selection_scales_to_one_hundred_and_odd_limits():
    records=[master(f"1{index:05d}","KOSPI") for index in range(60)]+[master(f"2{index:05d}","KOSDAQ") for index in range(60)]
    hundred=select_balanced_universe(reversed(records),100)
    assert sum(row.metadata.market=="KOSPI" for row in hundred)==50
    assert sum(row.metadata.market=="KOSDAQ" for row in hundred)==50
    odd=select_balanced_universe(records,99)
    assert sum(row.metadata.market=="KOSPI" for row in odd)==50
    assert sum(row.metadata.market=="KOSDAQ" for row in odd)==49
    assert [row.metadata.code for row in hundred]==[row.metadata.code for row in select_balanced_universe(reversed(records),100)]


def test_balanced_selection_scales_to_five_hundred_and_backfills_short_market():
    records=[master(f"1{index:05d}","KOSPI") for index in range(300)]+[master(f"2{index:05d}","KOSDAQ") for index in range(300)]
    selected=select_balanced_universe(reversed(records),500)
    assert sum(row.metadata.market=="KOSPI" for row in selected)==250
    assert sum(row.metadata.market=="KOSDAQ" for row in selected)==250
    odd=select_balanced_universe(records,499)
    assert sum(row.metadata.market=="KOSPI" for row in odd)==250 and sum(row.metadata.market=="KOSDAQ" for row in odd)==249
    short=[master(f"1{index:05d}","KOSPI") for index in range(100)]+[master(f"2{index:05d}","KOSDAQ") for index in range(450)]
    backfilled=select_balanced_universe(short,500)
    assert len(backfilled)==500 and sum(row.metadata.market=="KOSPI" for row in backfilled)==100
    assert [row.metadata.code for row in selected]==[row.metadata.code for row in select_balanced_universe(reversed(records),500)]


class HundredAdapter(LiveAdapter):
    def get_code_list_by_market(self, market):
        prefix="1" if market=="0" else "2"
        return [f"{prefix}{index:05d}" for index in range(60)]


def test_mock_hundred_collects_50_50_and_resume_makes_no_duplicate_requests(tmp_path, capsys):
    clock=lambda:datetime(2026,7,24,16)
    queues=[]
    def make_queue(adapter):
        queue=LiveQueue(adapter); queues.append(queue); return queue
    assert run_full_collection_live(tmp_path,max_symbols=100,confirm_100_symbol_live=True,clock=clock,application_factory=lambda _:LiveApplication(),adapter_factory=HundredAdapter,manager_factory=LiveManager,queue_factory=make_queue,connected=lambda _:True)==0
    first_output=capsys.readouterr().out
    first=queues[0]
    assert len([item for item in first.requests if item[0]=="OPT10081"])==100
    assert len([item for item in first.requests if item[0]=="OPT10059"])<=100
    assert "KOSPI_SELECTED=50" in first_output and "KOSDAQ_SELECTED=50" in first_output
    assert "CONFIRM_100_SYMBOL_LIVE=1" in first_output and "SELECTED_SYMBOLS=100" in first_output
    root=tmp_path/"data/validation/recommendations/full_collection"
    session=next(root.iterdir())
    plan=json.loads((session/"plan.json").read_text(encoding="utf-8"))
    assert plan["symbol_limit"]==100 and plan["confirm_100_symbol_live"] is True
    assert plan["market_counts"]=={"KOSPI":50,"KOSDAQ":50}
    assert len(plan["selected_symbols"])==100 and plan["schema_version"]==2
    assert run_full_collection_live(tmp_path,max_symbols=100,confirm_100_symbol_live=True,resume=session.name,clock=clock,application_factory=lambda _:LiveApplication(),adapter_factory=HundredAdapter,manager_factory=LiveManager,queue_factory=make_queue,connected=lambda _:True)==0
    assert queues[1].requests==[]
    resumed=capsys.readouterr().out
    assert "RESTORED_PRICE_SYMBOLS=100" in resumed
    assert "LIVE_PRICE_SYMBOLS=0" in resumed and "OPT10081_REQUESTS=0" in resumed
    assert not (tmp_path/"data/recommendations").exists()


def test_corrupt_completed_price_cache_is_not_restored(tmp_path, capsys):
    clock=lambda:datetime(2026,7,24,16); queues=[]
    def make_queue(adapter):
        queue=LiveQueue(adapter); queues.append(queue); return queue
    assert run_full_collection_live(tmp_path,max_symbols=1,clock=clock,application_factory=lambda _:LiveApplication(),adapter_factory=LiveAdapter,manager_factory=LiveManager,queue_factory=make_queue,connected=lambda _:True)==0
    capsys.readouterr(); session=next((tmp_path/"data/validation/recommendations/full_collection").iterdir())
    code=json.loads((session/"universe.json").read_text(encoding="utf-8"))["symbols"][0]["code"]
    (session/"price_normalized"/f"{code}.json").write_text("{broken",encoding="utf-8")
    assert run_full_collection_live(tmp_path,max_symbols=1,resume=session.name,clock=clock,application_factory=lambda _:LiveApplication(),adapter_factory=LiveAdapter,manager_factory=LiveManager,queue_factory=make_queue,connected=lambda _:True)==0
    assert [item[0] for item in queues[1].requests].count("OPT10081")==1


def test_hundred_symbol_partial_interrupt_resumes_without_duplicate_prices(tmp_path, capsys):
    clock=lambda:datetime(2026,7,24,16); queues=[]
    class PartialQueue(LiveQueue):
        def request_rows(self, request):
            daily_count=sum(item[0]=="OPT10081" for item in self.requests)
            if request.tr_code.upper()=="OPT10081" and daily_count==10:
                signal.raise_signal(signal.SIGINT)
            return super().request_rows(request)
    def first_queue(adapter):
        queue=PartialQueue(adapter); queues.append(queue); return queue
    assert run_full_collection_live(tmp_path,max_symbols=100,confirm_100_symbol_live=True,clock=clock,application_factory=lambda _:LiveApplication(),adapter_factory=HundredAdapter,manager_factory=LiveManager,queue_factory=first_queue,connected=lambda _:True)==130
    capsys.readouterr(); session=next((tmp_path/"data/validation/recommendations/full_collection").iterdir())
    def resumed_queue(adapter):
        queue=LiveQueue(adapter); queues.append(queue); return queue
    assert run_full_collection_live(tmp_path,max_symbols=100,confirm_100_symbol_live=True,resume=session.name,clock=clock,application_factory=lambda _:LiveApplication(),adapter_factory=HundredAdapter,manager_factory=LiveManager,queue_factory=resumed_queue,connected=lambda _:True)==0
    output=capsys.readouterr().out
    assert len([item for item in queues[1].requests if item[0]=="OPT10081"])==90
    assert "RESTORED_PRICE_SYMBOLS=10" in output and "LIVE_PRICE_SYMBOLS=90" in output


@pytest.mark.parametrize(("completed","expected"),[(5,45),(45,5),(50,0),(55,0)])
def test_remaining_counts_use_current_flow_phase(completed,expected):
    progress=CollectionProgress(100,100,phase="flow",price_completed_codes=[str(i) for i in range(100)],flow_target_codes=[str(i) for i in range(50)],flow_completed_codes=[str(i) for i in range(completed)])
    price,flow,estimated=remaining_counts(progress)
    assert price==0 and flow==expected and estimated==expected
    progress.phase="completed"
    assert remaining_counts(progress)[2]==0


def test_new_session_restores_compatible_symbols_from_smaller_completed_session(tmp_path, capsys):
    queues=[]
    def make_queue(adapter):
        queue=LiveQueue(adapter); queues.append(queue); return queue
    common=dict(application_factory=lambda _:LiveApplication(),adapter_factory=LiveAdapter,manager_factory=LiveManager,queue_factory=make_queue,connected=lambda _:True)
    assert run_full_collection_live(tmp_path,max_symbols=2,clock=lambda:datetime(2026,7,24,16,0,0),**common)==0
    capsys.readouterr()
    assert run_full_collection_live(tmp_path,max_symbols=4,clock=lambda:datetime(2026,7,24,16,0,1),**common)==0
    output=capsys.readouterr().out
    assert len([item for item in queues[1].requests if item[0]=="OPT10081"])==2
    assert "RESTORED_PRICE_SYMBOLS=2" in output and "LIVE_PRICE_SYMBOLS=2" in output
    assert "PRICE_CACHE_HITS=2" in output and "PRICE_CACHE_MISSES=2" in output


def test_mock_hundred_session_artifact_validator_is_read_only_and_consistent(tmp_path, capsys):
    clock=lambda:datetime(2026,7,24,16)
    assert run_full_collection_live(tmp_path,max_symbols=100,confirm_100_symbol_live=True,clock=clock,application_factory=lambda _:LiveApplication(),adapter_factory=HundredAdapter,manager_factory=LiveManager,queue_factory=LiveQueue,connected=lambda _:True)==0
    capsys.readouterr(); root=tmp_path/"data/validation/recommendations/full_collection"; session=next(root.iterdir())
    before={path.relative_to(session).as_posix():path.stat().st_mtime_ns for path in session.rglob("*") if path.is_file()}
    result=validate_full_collection_session(tmp_path,session.name)
    after={path.relative_to(session).as_posix():path.stat().st_mtime_ns for path in session.rglob("*") if path.is_file()}
    assert result["success"] and result["price_files"]==100 and result["flow_files"]<=100 and result["report_files"]==1
    assert before==after and result["external_calls"]==0
    cache_result=validate_cross_session_cache(tmp_path,session.name,clock=lambda:datetime(2026,7,24,17))
    assert cache_result["success"] and cache_result["restored_price"]==100
    assert cache_result["restored_flow"]==result["hard_filter_eligible_symbols"]
    assert cache_result["live_tr_calls"]==0 and cache_result["source_unchanged"]


def test_scoring_and_selector_receive_only_weekly_hard_filter_passes(tmp_path, capsys, monkeypatch):
    from qz_briefing.recommendations import full_universe_collection as module
    original=module.select_integrated_recommendations; received=[]
    monkeypatch.setattr(module,"select_integrated_recommendations",lambda bundles:(received.extend(item.master.metadata.code for item in bundles) or original(bundles)))
    class MixedQueue(LiveQueue):
        def request_rows(self, request):
            rows=super().request_rows(request)
            if request.tr_code.upper()=="OPT10081" and request.inputs["종목코드"].startswith("2"):
                for index,row in enumerate(rows):
                    close=400-index
                    row.update({"시가":str(close+1),"고가":str(close+2),"저가":str(close-1),"현재가":str(close)})
            return rows
    assert run_full_collection_live(tmp_path,max_symbols=2,clock=lambda:datetime(2026,7,24,16),application_factory=lambda _:LiveApplication(),adapter_factory=LiveAdapter,manager_factory=LiveManager,queue_factory=MixedQueue,connected=lambda _:True)==0
    capsys.readouterr(); session=next((tmp_path/"data/validation/recommendations/full_collection").iterdir())
    progress=json.loads((session/"progress.json").read_text(encoding="utf-8")); report=json.loads((session/"reports/recommendations.json").read_text(encoding="utf-8"))
    assert len(progress["hard_filter_pass_codes"])==1 and received==progress["hard_filter_pass_codes"]
    assert report["universe_input_count"]==2 and report["scoring_input_count"]==report["selector_input_count"]==1
    assert report["input_count"]==1


def test_repair_cli_is_distinct_from_resume_and_requires_explicit_live_session():
    parsed = parse_cli_arguments(["--collect-recommendation-universe", "--allow-kiwoom-live",
        "--max-symbols", "500", "--confirm-500-symbol-live", "--repair-failed", "--session-id", "repair-session"])
    assert parsed.repair_failed and parsed.resume is None and parsed.session_id == "repair-session"
    with pytest.raises(SystemExit):
        parse_cli_arguments(["--collect-recommendation-universe", "--allow-kiwoom-live", "--repair-failed"])
    with pytest.raises(SystemExit):
        parse_cli_arguments(["--collect-recommendation-universe", "--allow-kiwoom-live", "--repair-failed",
                             "--session-id", "repair-session", "--resume"])


def test_failed_repair_targets_skip_446_price_and_78_flow_successes():
    price_completed = [f"{index:06d}" for index in range(446)]
    price_failed = [f"{index:06d}" for index in range(446, 500)]
    flow_completed = price_completed[:78]
    flow_failed = price_failed[:18]
    progress = CollectionProgress(500, 500, phase="completed", price_completed_codes=price_completed,
        price_failed_codes=price_failed, flow_completed_codes=flow_completed, flow_failed_codes=flow_failed)
    assert failed_price_repair_targets(progress) == price_failed
    assert not (set(failed_price_repair_targets(progress)) & set(price_completed))
    newly_hard_pass = price_failed[18:23]
    targets = failed_flow_repair_targets(progress, flow_completed, flow_completed + newly_hard_pass)
    assert targets == sorted(set(flow_failed + newly_hard_pass))
    assert not (set(targets) & set(flow_completed))


def test_consecutive_timeout_breaker_trips_at_three_and_success_resets():
    progress = CollectionProgress(500, 500)
    timeout = KiwoomTrTimeoutError("TR request timed out")
    assert not full_collection_module._record_attempt_result(progress, timeout)
    assert not full_collection_module._record_attempt_result(progress, timeout)
    assert progress.consecutive_timeouts == 2
    assert not full_collection_module._record_attempt_result(progress, None)
    assert progress.consecutive_timeouts == 0
    assert not full_collection_module._record_attempt_result(progress, timeout)
    assert not full_collection_module._record_attempt_result(progress, timeout)
    assert full_collection_module._record_attempt_result(progress, timeout)
    assert progress.timeout_circuit_breaker == "TRIPPED"


def test_failure_history_is_deduplicated_and_resolved(tmp_path):
    session = FullCollectionSession(tmp_path, "repair", clock=lambda: datetime(2026, 8, 1, 12))
    session.create([{"market": "KOSPI", "code": "000001"}], mode="live_validation", symbol_limit=1)
    from qz_briefing.recommendations.data_models import CollectionFailure
    failure = CollectionFailure("000001", "daily", "KiwoomTrTimeoutError: timeout", datetime(2026, 8, 1, 11))
    session.append_failure(failure, repair_run_id="run-1")
    session.append_failure(failure, repair_run_id="run-1")
    payload = json.loads((session.path / "failures.json").read_text(encoding="utf-8"))
    assert len(payload["failures"]) == 1 and payload["failures"][0]["attempt"] == 2
    session.resolve_failure("000001", "daily", repair_run_id="run-1")
    resolved = json.loads((session.path / "failures.json").read_text(encoding="utf-8"))["failures"][0]
    assert resolved["resolved"] and resolved["resolved_at"] and resolved["repair_run_id"] == "run-1"


def partition_symbols(count=2510):
    return [ParentSymbol("KOSPI" if index < 1255 else "KOSDAQ", f"{index:06d}", str(index)) for index in range(count)]


def partition_snapshot(count=2510):
    rows = [{"market": item.market, "code": item.code, "name": item.name} for item in partition_symbols(count)]
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schema_version": 1, "parser_version": "full-universe-v2", "created_at": "2026-08-01T09:00:00",
            "source": "fixture", "trade_date": "2026-08-01", "kospi_master_codes": min(count, 1255),
            "kosdaq_master_codes": max(0, count - 1255), "master_codes_total": count,
            "filtered_universe_total": count, "excluded_total": 0, "duplicates": 0,
            "invalid_codes": 0, "symbols": rows, "universe_hash": digest}


def test_partitioned_manifest_covers_2510_once_in_eleven_price_batches(tmp_path):
    path = create_parent_collection(tmp_path, reversed(partition_symbols()), collection_id="parent",
        trade_date=date(2026, 8, 1), clock=lambda: datetime(2026, 8, 1, 9))
    manifest, progress = verify_parent_collection(path)
    assert manifest["universe_total"] == 2510
    assert [len(batch) for batch in manifest["price_batches"]] == [250] * 10 + [10]
    flattened = [code for batch in manifest["price_batches"] for code in batch]
    assert len(flattened) == len(set(flattened)) == 2510
    assert progress["phase"] == "planned"


def test_partitioned_universe_is_market_then_six_digit_code_deterministic():
    rows = [ParentSymbol("KOSDAQ", "000002"), ParentSymbol("KOSPI", "000003"),
            ParentSymbol("KOSPI", "000001"), ParentSymbol("KOSPI", "000001"), ParentSymbol("ETF", "123456")]
    expected = [("KOSPI", "000001"), ("KOSPI", "000003"), ("KOSDAQ", "000002")]
    assert [(row.market, row.code) for row in deterministic_parent_universe(rows)] == expected
    assert [(row.market, row.code) for row in deterministic_parent_universe(reversed(rows))] == expected


def test_partitioned_manifest_and_universe_hash_tampering_is_blocked(tmp_path):
    path = create_parent_collection(tmp_path, partition_symbols(10), collection_id="parent", trade_date=date(2026, 8, 1))
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8")); manifest["price_batch_size"] = 499
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest hash mismatch"): verify_parent_collection(path)


def test_partitioned_each_invocation_advances_exactly_one_batch(tmp_path):
    path = create_parent_collection(tmp_path, partition_symbols(600), collection_id="parent", trade_date=date(2026, 8, 1))
    assert apply_mock_batch(path)["index"] == 1
    _, progress = verify_parent_collection(path)
    assert [batch["status"] for batch in progress["price_batches"]] == ["complete", "pending", "pending"]
    assert apply_mock_batch(path)["index"] == 2


def test_partitioned_timeout_breaker_blocks_next_batch_until_resume_or_repair(tmp_path):
    path = create_parent_collection(tmp_path, partition_symbols(500), collection_id="parent", trade_date=date(2026, 8, 1))
    first_codes = verify_parent_collection(path)[0]["price_batches"][0]
    result = apply_mock_batch(path, fail_codes=first_codes[:3])
    assert result["status"] == "interrupted" and result["live_requests"] == 3
    with pytest.raises(ValueError, match="repair or resume"): next_batch(path)


def test_partitioned_resume_never_requests_completed_symbols_twice(tmp_path):
    path = create_parent_collection(tmp_path, partition_symbols(250), collection_id="parent", trade_date=date(2026, 8, 1))
    interrupted = apply_mock_batch(path, interrupt_after=10)
    assert interrupted["completed"] == 10
    _, progress = verify_parent_collection(path); progress["price_batches"][0]["status"] = "pending"
    atomic_path = path / "progress.json"
    from qz_briefing.runtime.unattended import atomic_write_json
    atomic_write_json(atomic_path, progress)
    resumed = apply_mock_batch(path)
    assert resumed["live_requests"] == 240 and resumed["completed"] == 250


def test_partitioned_repair_requests_only_failed_symbols(tmp_path):
    path = create_parent_collection(tmp_path, partition_symbols(10), collection_id="parent", trade_date=date(2026, 8, 1), price_batch_size=10)
    codes = verify_parent_collection(path)[0]["price_batches"][0]
    result = apply_mock_batch(path, fail_codes={codes[-1]})
    assert result["status"] == "partial" and result["completed"] == 9
    repaired = apply_mock_batch(path, repair=True)
    assert repaired["symbols"] == repaired["live_requests"] == 1 and repaired["status"] == "complete"


def test_partitioned_flow_selection_waits_for_all_prices_and_is_capped_deterministically(tmp_path):
    path = create_parent_collection(tmp_path, partition_symbols(251), collection_id="parent", trade_date=date(2026, 8, 1))
    apply_mock_batch(path)
    with pytest.raises(ValueError, match="all price batches"): finalize_price_phase(path, {})
    apply_mock_batch(path)
    _, progress = verify_parent_collection(path)
    scores = {code: 50.0 for code in progress["price_completed_codes"]}
    selected = finalize_price_phase(path, scores)
    assert len(selected) == 120 and selected == sorted(selected)
    assert [len(batch["codes"]) for batch in verify_parent_collection(path)[1]["flow_batches"]] == [40, 40, 40]


def test_partitioned_cache_hits_do_not_count_as_live_requests_and_final_report_is_validation_only(tmp_path):
    path = create_parent_collection(tmp_path, partition_symbols(120), collection_id="parent", trade_date=date(2026, 8, 1))
    price_codes = verify_parent_collection(path)[0]["price_batches"][0]
    price = apply_mock_batch(path, cache_codes=price_codes)
    assert price["cache_hits"] == 120 and price["live_requests"] == 0
    finalize_price_phase(path, {code: 100.0 for code in price_codes})
    flow_codes = verify_parent_collection(path)[1]["flow_candidate_codes"]
    flow = apply_mock_batch(path, cache_codes=flow_codes)
    assert flow["cache_hits"] == 40 and flow["live_requests"] == 0
    apply_mock_batch(path, cache_codes=flow_codes); apply_mock_batch(path, cache_codes=flow_codes)
    report = finalize_mock_report(path)
    assert report["validation_only"] and not (tmp_path / "data/recommendations").exists()


def test_partitioned_confirmation_is_blocked_before_factories(monkeypatch, capsys):
    assert run(["--collect-recommendation-universe", "--full-universe", "--allow-kiwoom-live",
                "--run-next-batch", "--collection-id", "parent"]) == 2
    output = capsys.readouterr().out
    assert "BLOCK_REASON=confirm_full_universe_live_required" in output and "LIVE_TR_CALLS=0" in output


def test_partitioned_plan_only_auto_id_creates_complete_offline_plan(tmp_path, monkeypatch, capsys):
    from qz_briefing.recommendations import partitioned_full_universe as module
    validation_root = tmp_path / "full_universe"; validation_root.mkdir(parents=True)
    snapshot = partition_snapshot()
    (validation_root / "universe.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(module, "partitioned_root", lambda _project_root: validation_root)
    assert run(["--collect-recommendation-universe", "--full-universe", "--allow-kiwoom-live",
                "--confirm-full-universe-live", "--plan-only", "--price-batch-size", "250",
                "--flow-batch-size", "40"], application_factory=forbidden, adapter_factory=forbidden,
               clock=lambda: datetime(2026, 8, 1, 12, 34, 56, 123456)) == 0
    output = capsys.readouterr().out
    collection_id = "20260801T123456123456"; path = validation_root / collection_id
    assert f"COLLECTION_ID={collection_id}" in output and "PLAN_ONLY=1" in output
    assert "UNIVERSE_TOTAL=2510" in output and "PRICE_BATCH_COUNT=11" in output
    assert "PARENT_PHASE=planned" in output and "LIVE_TR_CALLS=0" in output
    assert "FULL UNIVERSE PARTITIONED PLAN: PASS" in output
    assert all((path / name).exists() for name in ("manifest.json", "progress.json", "failures.json"))
    manifest, progress = verify_parent_collection(path)
    flattened = [code for batch in manifest["price_batches"] for code in batch]
    assert len(flattened) == len(set(flattened)) == 2510 and progress["phase"] == "planned"


@pytest.mark.parametrize("extra", [["--run-next-batch"], ["--run-next-batch", "--resume"],
                                    ["--run-next-batch", "--repair-failed"]])
def test_partitioned_continuation_commands_require_collection_id(extra, capsys):
    assert run(["--collect-recommendation-universe", "--full-universe", "--allow-kiwoom-live",
                "--confirm-full-universe-live", *extra], application_factory=forbidden,
               adapter_factory=forbidden) == 2
    output = capsys.readouterr().out
    assert "BLOCK_REASON=collection_id_required" in output and "LIVE_TR_CALLS=0" in output


def test_snapshot_builder_confirmation_and_live_flag_block_before_factories(capsys):
    assert run(["--build-full-universe-snapshot", "--allow-kiwoom-live"],
               application_factory=forbidden, adapter_factory=forbidden) == 2
    assert "confirm_full_universe_snapshot_live_required" in capsys.readouterr().out
    assert run(["--build-full-universe-snapshot", "--confirm-full-universe-snapshot-live"],
               application_factory=forbidden, adapter_factory=forbidden) == 2
    assert "allow_kiwoom_live_required" in capsys.readouterr().out


def test_snapshot_builder_uses_only_master_apis_and_loader_accepts_output(tmp_path):
    calls = []
    class SnapshotAdapter(LiveAdapter):
        def __getattribute__(self, name):
            target = super().__getattribute__(name)
            if name.startswith("get_") and callable(target):
                def tracked(*args, **kwargs): calls.append(name); return target(*args, **kwargs)
                return tracked
            return target
    result = build_full_universe_snapshot(tmp_path, application_factory=lambda _: LiveApplication(),
        adapter_factory=SnapshotAdapter, manager_factory=LiveManager, connected=lambda _: True,
        clock=lambda: datetime(2026, 8, 1, 9))
    payload = load_universe_snapshot(result["path"])
    assert payload["filtered_universe_total"] == 24
    assert result["master_api_calls"] == 2 + 24 * 6
    allowed = {"get_connect_state", "get_code_list_by_market", "get_master_code_name", "get_master_stock_state",
               "get_master_construction", "get_master_stock_info", "get_master_listed_stock_date", "get_master_last_price"}
    assert calls and all(name in allowed for name in calls)
    assert not any("request" in name.lower() or "comm" in name.lower() for name in calls)


def test_snapshot_builder_reuses_existing_universe_filter():
    now = datetime(2026, 8, 1, 9)
    def row(code, market, name, security="common_stock", tradable=True, status="normal"):
        return StockMasterRecord(DataMetadata(code, name, market, now, "fixture", now), security, tradable, status)
    payload = snapshot_payload([row("000001", "KOSPI", "normal"), row("000002", "KOSDAQ", "ETF", "etf"),
        row("000003", "KOSPI", "halt", tradable=False, status="trading_halt"), row("BAD", "KOSPI", "bad"),
        row("000004", "KOSDAQ", "")], raw_market_counts={"KOSPI": 3, "KOSDAQ": 2}, clock=lambda: now)
    assert [item["code"] for item in payload["symbols"]] == ["000001"]
    assert payload["invalid_codes"] == 1 and payload["missing_names"] == 1 and payload["excluded_total"] == 4


def test_snapshot_atomic_replace_preserves_version_and_failed_candidate_preserves_current(tmp_path):
    first = partition_snapshot(10); path = save_snapshot(tmp_path, first)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already_exists"): save_snapshot(tmp_path, first)
    assert path.read_bytes() == before
    second = partition_snapshot(11); second["created_at"] = "2026-08-01T10:00:00"
    save_snapshot(tmp_path, second, replace=True)
    backups = list((path.parent / "snapshots").glob("*.json"))
    assert len(backups) == 1 and load_universe_snapshot(backups[0])["universe_hash"] == first["universe_hash"]
    broken = dict(second); broken["universe_hash"] = "broken"
    current = path.read_bytes()
    with pytest.raises(ValueError, match="hash mismatch"): save_snapshot(tmp_path, broken, replace=True)
    assert path.read_bytes() == current and not list(path.parent.glob("*.tmp"))


def test_snapshot_builder_login_failure_creates_no_snapshot(tmp_path):
    with pytest.raises(RuntimeError, match="login_failed"):
        build_full_universe_snapshot(tmp_path, application_factory=lambda _: LiveApplication(),
            adapter_factory=LiveAdapter, manager_factory=LiveManager, connected=lambda _: False)
    assert not (tmp_path / "data/validation/recommendations/full_universe/universe.json").exists()
