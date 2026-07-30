from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import signal
from pathlib import Path

import pytest

from qz_briefing.__main__ import run
from qz_briefing.recommendations.data_models import DataMetadata, StockMasterRecord
from qz_briefing.recommendations.full_universe_collection import (
    FullCollectionSession, deterministic_universe, protected_validation_root,
    run_full_collection_live, run_full_collection_plan,
    select_balanced_universe, select_flow_targets, should_abort_for_failures,
    validate_live_scope, validate_scope,
)
from qz_briefing.recommendations.request_planner import PreliminaryCandidate


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


def test_collection_cli_blocks_unsafe_modes(tmp_path: Path, capsys, monkeypatch):
    from qz_briefing.recommendations import full_universe_collection as module
    original = module.run_full_collection_plan
    monkeypatch.setattr(module, "run_full_collection_plan", lambda _project_root, **kwargs: original(tmp_path, **kwargs))
    common = ["--collect-recommendation-universe", "--validation-root", str(tmp_path / "data/validation/recommendations/full_collection")]
    assert run(common + ["--dry-run"]) == 2
    assert "full collection requires --full-universe-confirmed" in capsys.readouterr().out
    assert run(common + ["--allow-kiwoom-live", "--max-symbols", "20"]) == 2
    assert "blocked inside Codex" in capsys.readouterr().out


def test_live_scope_is_explicit_and_never_exceeds_twenty():
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
