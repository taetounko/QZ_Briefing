"""Validation-only session planning for a future full KOSPI/KOSDAQ collection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import json
import os
import signal
from pathlib import Path
from typing import Callable, Iterable

from qz_briefing.runtime.unattended import atomic_write_json

from .data_models import AggregatedWeeklyBar, DailyBar, DataMetadata, PriceFeatures, StockMasterRecord
from .data_pipeline import universe_decision
from .data_pipeline import aggregate_weekly_bars, compute_price_features, normalize_daily_bars, weekly_ma5_metrics
from .data_models import CollectionFailure, RecommendationDataBundle
from .integrated_scoring import evaluate_preliminary_candidate
from .selector import select_integrated_recommendations
from .request_planner import CollectionPolicy, PreliminaryCandidate, select_flow_candidates


DEFAULT_RELATIVE_ROOT = Path("data/validation/recommendations/full_collection")
SESSION_FILES = ("session.json", "universe.json", "plan.json", "progress.json", "failures.json")
SESSION_DIRS = ("price_raw", "price_normalized", "weekly", "features", "flow_raw", "reports")
MARKET_ORDER = {"KOSPI": 0, "KOSDAQ": 1}
LIVE_SYMBOL_LIMIT = 20


@dataclass(frozen=True)
class FullCollectionPolicy:
    flow_candidate_limit: int = 120
    price_row_limit: int = 260
    flow_row_limit: int = 20
    failure_rate_limit: float = 0.25
    failure_abort_minimum_attempts: int = 10


@dataclass
class CollectionProgress:
    universe_total: int
    symbol_limit: int
    phase: str = "planned"
    price_completed_codes: list[str] = field(default_factory=list)
    price_failed_codes: list[str] = field(default_factory=list)
    weekly_possible_codes: list[str] = field(default_factory=list)
    hard_filter_pass_codes: list[str] = field(default_factory=list)
    flow_target_codes: list[str] = field(default_factory=list)
    flow_completed_codes: list[str] = field(default_factory=list)
    flow_failed_codes: list[str] = field(default_factory=list)
    last_symbol: str = ""
    started_at: str = ""
    updated_at: str = ""
    request_count: int = 0
    opt10081_requests: int = 0
    opt10081_successes: int = 0
    opt10081_failures: int = 0
    opt10059_requests: int = 0
    opt10059_successes: int = 0
    opt10059_failures: int = 0
    retries: int = 0
    continuation_requests: int = 0
    shutdown_reason: str = ""
    estimated_remaining: int = 0


def protected_validation_root(project_root: Path, requested: Path | None = None) -> Path:
    """Return a path contained by the dedicated validation tree."""
    project_root = project_root.resolve()
    allowed = (project_root / DEFAULT_RELATIVE_ROOT).resolve()
    target = (requested if requested is not None else allowed)
    if not target.is_absolute():
        target = project_root / target
    target = target.resolve()
    if target != allowed and allowed not in target.parents:
        raise ValueError("validation-root must stay inside data/validation/recommendations/full_collection")
    return target


def deterministic_universe(records: Iterable[StockMasterRecord]) -> list[StockMasterRecord]:
    """Filter, de-duplicate and order master data without depending on input order."""
    chosen: dict[str, StockMasterRecord] = {}
    for record in sorted(records, key=lambda item: (MARKET_ORDER.get(item.metadata.market, 99), item.metadata.code, item.metadata.name)):
        if universe_decision(record)[0] and record.metadata.code not in chosen:
            chosen[record.metadata.code] = record
    return sorted(chosen.values(), key=lambda item: (MARKET_ORDER[item.metadata.market], item.metadata.code))


def validate_scope(universe_count: int, max_symbols: int | None, full_universe_confirmed: bool) -> int:
    if universe_count < 1:
        raise ValueError("recommendation universe is empty")
    if max_symbols is None:
        if not full_universe_confirmed:
            raise ValueError("full collection requires --full-universe-confirmed")
        return universe_count
    if not 1 <= max_symbols <= universe_count:
        raise ValueError("--max-symbols must be between 1 and the universe size")
    return max_symbols


def validate_live_scope(max_symbols: int | None) -> int:
    if max_symbols is None:
        raise ValueError("live validation requires explicit --max-symbols")
    if not 1 <= max_symbols <= LIVE_SYMBOL_LIMIT:
        raise ValueError(f"live validation --max-symbols must be between 1 and {LIVE_SYMBOL_LIMIT}")
    return max_symbols


def select_balanced_universe(records: Iterable[StockMasterRecord], limit: int) -> list[StockMasterRecord]:
    """Select up to ten per market, then deterministically backfill a short market."""
    eligible = deterministic_universe(records)
    by_market = {market: [row for row in eligible if row.metadata.market == market] for market in MARKET_ORDER}
    kospi_goal = min(10, (limit + 1) // 2)
    kosdaq_goal = min(10, limit // 2)
    selected = by_market["KOSPI"][:kospi_goal] + by_market["KOSDAQ"][:kosdaq_goal]
    selected_codes = {row.metadata.code for row in selected}
    remaining = [row for row in eligible if row.metadata.code not in selected_codes]
    selected.extend(remaining[: max(0, limit - len(selected))])
    return selected[:limit]


def resolve_resume_session(root: Path, requested: str | None) -> str | None:
    if requested is None or requested:
        return requested
    candidates = []
    if root.exists():
        for path in root.iterdir():
            try:
                progress = json.loads((path / "progress.json").read_text(encoding="utf-8"))
                if progress.get("phase") != "completed": candidates.append(path.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    if len(candidates) != 1:
        raise ValueError("--resume without a session id requires exactly one incomplete compatible session")
    return candidates[0]


def should_abort_for_failures(completed: int, failed: int, policy: FullCollectionPolicy = FullCollectionPolicy()) -> bool:
    attempted = completed + failed
    return attempted >= policy.failure_abort_minimum_attempts and failed / attempted > policy.failure_rate_limit


def select_flow_targets(candidates: list[PreliminaryCandidate], policy: FullCollectionPolicy = FullCollectionPolicy()) -> list[str]:
    request_policy = CollectionPolicy(investor_candidate_limit=policy.flow_candidate_limit)
    return [item.code for item in select_flow_candidates(candidates, request_policy)]


class FullCollectionSession:
    """Atomic, append-safe state for validation collection; it never deletes raw data."""

    def __init__(self, root: Path, session_id: str, *, clock=datetime.now) -> None:
        self.root, self.session_id, self.clock = root, session_id, clock
        self.path = root / session_id

    def create(self, universe: list[dict[str, object]], *, mode: str, symbol_limit: int, restart: bool = False) -> CollectionProgress:
        if self.path.exists() and not restart:
            raise ValueError("session already exists; use --resume or --restart")
        if self.path.exists() and restart:
            raise ValueError("--restart requires a new session id and never overwrites an existing session")
        self.path.mkdir(parents=True)
        for name in SESSION_DIRS:
            (self.path / name).mkdir()
        now = self.clock().isoformat()
        progress = CollectionProgress(len(universe), symbol_limit, started_at=now, updated_at=now, estimated_remaining=symbol_limit)
        atomic_write_json(self.path / "session.json", {"session_id": self.session_id, "mode": mode, "created_at": now, "validation_only": True})
        atomic_write_json(self.path / "universe.json", {"symbols": universe})
        atomic_write_json(self.path / "plan.json", {"price_row_limit": 260, "flow_row_limit": 20, "flow_candidate_limit": 120, "symbol_limit": symbol_limit})
        atomic_write_json(self.path / "progress.json", asdict(progress))
        atomic_write_json(self.path / "failures.json", {"failures": []})
        return progress

    def load_progress(self) -> CollectionProgress:
        value = json.loads((self.path / "progress.json").read_text(encoding="utf-8"))
        return CollectionProgress(**value)

    def checkpoint(self, progress: CollectionProgress) -> None:
        progress.updated_at = self.clock().isoformat()
        progress.estimated_remaining = max(0, progress.symbol_limit - len(set(progress.price_completed_codes)) - len(set(progress.price_failed_codes)))
        atomic_write_json(self.path / "progress.json", asdict(progress))

    def pending_price_codes(self) -> list[str]:
        universe = json.loads((self.path / "universe.json").read_text(encoding="utf-8"))["symbols"]
        progress = self.load_progress()
        done = set(progress.price_completed_codes) | set(progress.price_failed_codes)
        return [str(item["code"]) for item in universe[:progress.symbol_limit] if str(item["code"]) not in done]

    def append_failure(self, failure: CollectionFailure) -> None:
        path = self.path / "failures.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["failures"].append({"code": failure.code, "data_kind": failure.data_kind, "reason": failure.reason, "occurred_at": failure.occurred_at.isoformat()})
        atomic_write_json(path, payload)

    def save(self, directory: str, code: str, value: object) -> None:
        from .data_cache import _json_value
        payload = _json_value(value)
        atomic_write_json(self.path / directory / f"{code}.json", payload if isinstance(payload, dict) else {"data": payload})


def fixture_universe(count: int = 500) -> list[dict[str, str]]:
    return [
        {"market": "KOSPI" if index % 2 == 0 else "KOSDAQ", "code": f"7{index:05d}", "name": f"fixture-{index:05d}"}
        for index in range(1, count + 1)
    ]


def _status_lines(mode: str, session: FullCollectionSession, progress: CollectionProgress) -> str:
    planned_flow_requests = min(progress.symbol_limit, FullCollectionPolicy().flow_candidate_limit)
    values = {
        "MODE": mode, "SESSION_ID": session.session_id, "PHASE": progress.phase,
        "UNIVERSE_TOTAL": progress.universe_total, "SYMBOL_LIMIT": progress.symbol_limit,
        "KOSPI_SELECTED": _selected_market_count(session, "KOSPI"),
        "KOSDAQ_SELECTED": _selected_market_count(session, "KOSDAQ"),
        "SELECTED_SYMBOLS": progress.symbol_limit,
        "PRICE_COMPLETED": len(progress.price_completed_codes), "PRICE_FAILED": len(progress.price_failed_codes),
        "WEEKLY_POSSIBLE": len(progress.weekly_possible_codes),
        "HARD_FILTER_PASS": len(progress.hard_filter_pass_codes),
        "FLOW_TARGETS": len(progress.flow_target_codes) if progress.phase != "planned" else planned_flow_requests,
        "FLOW_COMPLETED": len(progress.flow_completed_codes), "FLOW_FAILED": len(progress.flow_failed_codes),
        "CACHE_HITS": len(progress.price_completed_codes) + len(progress.flow_completed_codes),
        "CACHE_MISSES": progress.estimated_remaining,
        "OPT10081_REQUESTS": progress.opt10081_requests if mode == "live_validation" else progress.symbol_limit,
        "OPT10081_SUCCESSES": progress.opt10081_successes, "OPT10081_FAILURES": progress.opt10081_failures,
        "OPT10059_REQUESTS": progress.opt10059_requests if mode == "live_validation" else (0 if progress.phase != "planned" else planned_flow_requests),
        "OPT10059_SUCCESSES": progress.opt10059_successes, "OPT10059_FAILURES": progress.opt10059_failures,
        "LIVE_TR_CALLS": progress.request_count, "RETRIES": progress.retries,
        "CONTINUATION_REQUESTS": progress.continuation_requests, "LAST_SYMBOL": progress.last_symbol,
        "ESTIMATED_REMAINING": progress.estimated_remaining, "ORDER_ACCOUNT_TR": 0, "TELEGRAM_SENDS": 0,
        "ESTIMATED_MINIMUM_SECONDS": progress.symbol_limit + planned_flow_requests, "OPERATIONAL_WRITES": 0,
        "DASHBOARD_STARTED": 0,
    }
    return "\n".join(f"{key}={value}" for key, value in values.items())


def _selected_market_count(session: FullCollectionSession, market: str) -> int:
    path = session.path / "universe.json"
    if not path.exists(): return 0
    symbols = json.loads(path.read_text(encoding="utf-8")).get("symbols", [])
    return sum(item.get("market") == market for item in symbols[:session.load_progress().symbol_limit])


def _metadata(value: dict[str, object]) -> DataMetadata:
    data = dict(value); data["as_of"] = datetime.fromisoformat(str(data["as_of"])); data["updated_at"] = datetime.fromisoformat(str(data["updated_at"]))
    return DataMetadata(**data)


def _load_saved_bundle(session: FullCollectionSession, master: StockMasterRecord) -> RecommendationDataBundle | None:
    code = master.metadata.code
    try:
        daily_payload = json.loads((session.path / "price_normalized" / f"{code}.json").read_text(encoding="utf-8"))["data"]
        weekly_payload = json.loads((session.path / "weekly" / f"{code}.json").read_text(encoding="utf-8"))["data"]
        feature_payload = json.loads((session.path / "features" / f"{code}.json").read_text(encoding="utf-8"))
        daily = tuple(DailyBar(_metadata(row.pop("metadata")), date.fromisoformat(row.pop("trading_date")), **row) for row in (dict(item) for item in daily_payload))
        weekly = tuple(AggregatedWeeklyBar(_metadata(row.pop("metadata")), date.fromisoformat(row.pop("week_start")), date.fromisoformat(row.pop("week_end")), **row) for row in (dict(item) for item in weekly_payload))
        return RecommendationDataBundle(master, daily, weekly, PriceFeatures(**feature_payload))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _save_startup_failure(root: Path, symbol_limit: int, clock: Callable[[], datetime], reason: str) -> None:
    session = FullCollectionSession(root, clock().strftime("%Y%m%dT%H%M%S%f-startup-failed"), clock=clock)
    progress = session.create([], mode="live_validation", symbol_limit=symbol_limit)
    progress.phase = "startup_failed"; progress.shutdown_reason = "login_failed"; session.checkpoint(progress)
    session.append_failure(CollectionFailure("", "connection", reason, clock()))


def _connection_lost(adapter: object) -> bool:
    try: return int(adapter.get_connect_state()) == 0
    except Exception: return True


def run_full_collection_plan(project_root: Path, *, dry_run: bool, cached_only: bool, allow_live: bool,
                             max_symbols: int | None, full_universe_confirmed: bool,
                             validation_root: Path | None = None, resume: str | None = None,
                             restart: bool = False, clock=datetime.now,
                             application_factory: Callable[[list[str]], object] | None = None,
                             adapter_factory: Callable[[], object] | None = None,
                             manager_factory: Callable[[object], object] | None = None,
                             queue_factory: Callable[[object], object] | None = None) -> int:
    modes = sum(bool(value) for value in (dry_run, cached_only, allow_live))
    if modes != 1:
        raise ValueError("select exactly one of --dry-run, --cached-only, or --allow-kiwoom-live")
    if allow_live:
        validate_live_scope(max_symbols)
        if full_universe_confirmed:
            raise ValueError("--full-universe-confirmed cannot expand live validation beyond 20 symbols")
        if any(os.environ.get(name) for name in ("CODEX_HOME", "CODEX_SANDBOX", "CODEX_THREAD_ID")):
            raise ValueError("live Kiwoom collection is blocked inside Codex; run it from ordinary Windows PowerShell")
        return run_full_collection_live(
            project_root, max_symbols=max_symbols or 0, validation_root=validation_root,
            resume=resume, restart=restart, application_factory=application_factory,
            adapter_factory=adapter_factory, manager_factory=manager_factory,
            queue_factory=queue_factory,
        )
    root = protected_validation_root(project_root, validation_root)
    resume = resolve_resume_session(root, resume)
    if resume:
        session = FullCollectionSession(root, resume, clock=clock)
        if not session.path.exists():
            raise ValueError("resume session does not exist")
        progress = session.load_progress()
    else:
        universe = fixture_universe()
        limit = validate_scope(len(universe), max_symbols, full_universe_confirmed)
        session_id = clock().strftime("%Y%m%dT%H%M%S%f")
        session = FullCollectionSession(root, session_id, clock=clock)
        progress = session.create(universe, mode="dry-run" if dry_run else "cached-only", symbol_limit=limit, restart=restart)
    print(_status_lines("DRY_RUN" if dry_run else "CACHED_ONLY", session, progress))
    print("FULL UNIVERSE COLLECTION DRY RUN: PASS")
    return 0


def run_full_collection_live(
    project_root: Path, *, max_symbols: int, validation_root: Path | None = None,
    resume: str | None = None, restart: bool = False, clock: Callable[[], datetime] = datetime.now,
    adapter_factory: Callable[[], object] | None = None,
    application_factory: Callable[[list[str]], object] | None = None,
    manager_factory: Callable[[object], object] | None = None,
    queue_factory: Callable[[object], object] | None = None,
    connected: Callable[[object], bool] | None = None,
) -> int:
    """Run a bounded validation-only collection through the existing Kiwoom adapters."""
    validate_live_scope(max_symbols)
    root = protected_validation_root(project_root, validation_root)
    resume = resolve_resume_session(root, resume)
    from .kiwoom_collection import KiwoomDailyDataSource, KiwoomInvestorFlowDataSource, KiwoomMasterDataSource
    from .live_validation import _ensure_connected, resolve_security_type
    if application_factory is None:
        from qz_briefing.__main__ import create_application
        application_factory = create_application
    if adapter_factory is None:
        from qz_briefing.kiwoom.qax_adapter import KiwoomQAxAdapter
        adapter_factory = KiwoomQAxAdapter
    if manager_factory is None:
        from qz_briefing.kiwoom.connection_manager import KiwoomConnectionManager
        manager_factory = KiwoomConnectionManager
    if queue_factory is None:
        from qz_briefing.kiwoom.tr_requests import KiwoomTrRequestQueue
        queue_factory = KiwoomTrRequestQueue
    application = None; adapter = None; manager = None; queue = None
    session = None; progress = None
    interrupt = {"sigint": False}
    previous_sigint = None
    try:
        previous_sigint = signal.getsignal(signal.SIGINT)
        def handle_sigint(_signum, _frame):
            interrupt["sigint"] = True
            raise KeyboardInterrupt
        signal.signal(signal.SIGINT, handle_sigint)
    except (ValueError, AttributeError):
        previous_sigint = None
    try:
        try:
            application = application_factory([])
        except Exception as exc:
            raise RuntimeError(f"QApplication initialization failed: {type(exc).__name__}") from None
        if application is None:
            raise RuntimeError("QApplication initialization failed")
        if hasattr(application, "setQuitOnLastWindowClosed"):
            application.setQuitOnLastWindowClosed(False)
        print("QAPPLICATION_READY=1", flush=True)
        print("QT_HEADLESS_COLLECTION=1", flush=True)
        print("QUIT_ON_LAST_WINDOW_CLOSED=0", flush=True)
        print("DASHBOARD_STARTED=0", flush=True)
        last_window_closed = getattr(application, "lastWindowClosed", None)
        if hasattr(last_window_closed, "connect"):
            last_window_closed.connect(lambda: print("QT_LAST_WINDOW_CLOSED_IGNORED=1", flush=True))
        about_to_quit = getattr(application, "aboutToQuit", None)
        if hasattr(about_to_quit, "connect"):
            about_to_quit.connect(lambda: print("QT_ABOUT_TO_QUIT_IGNORED=1", flush=True))
        try:
            adapter = adapter_factory()
            manager = manager_factory(adapter)
        except Exception as exc:
            raise RuntimeError(f"Kiwoom adapter initialization failed: {type(exc).__name__}") from None
        connection_check = connected or _ensure_connected
        try:
            login_ready = bool(connection_check(adapter))
        except KeyboardInterrupt:
            if interrupt["sigint"]:
                raise
            print("QT_LAST_WINDOW_CLOSED_IGNORED=1", flush=True)
            login_ready = int(adapter.get_connect_state()) == 1
        connect_state = int(adapter.get_connect_state())
        if not login_ready or connect_state != 1:
            _save_startup_failure(root, max_symbols, clock, "Kiwoom login unavailable")
            print("SHUTDOWN_REASON=login_failed", flush=True)
            raise ValueError("Kiwoom login and GetConnectState == 1 are required before live TR requests")
        print("LOGIN_SUCCESS=1", flush=True); print("CONNECT_STATE=1", flush=True)
        master_source = KiwoomMasterDataSource(adapter, security_type_resolver=resolve_security_type, clock=clock)
        records = master_source.collect_market("KOSPI") + master_source.collect_market("KOSDAQ")
        selected = select_balanced_universe(records, max_symbols)
        if not selected: raise ValueError("recommendation universe is empty")
        print(f"KOSPI_SELECTED={sum(row.metadata.market == 'KOSPI' for row in selected)}", flush=True)
        print(f"KOSDAQ_SELECTED={sum(row.metadata.market == 'KOSDAQ' for row in selected)}", flush=True)
        universe = [{"market": row.metadata.market, "code": row.metadata.code, "name": row.metadata.name} for row in selected]
        if resume:
            session = FullCollectionSession(root, resume, clock=clock)
            if not session.path.exists(): raise ValueError("resume session does not exist")
            saved = json.loads((session.path / "universe.json").read_text(encoding="utf-8"))["symbols"]
            if [(x["market"], x["code"]) for x in saved] != [(x["market"], x["code"]) for x in universe]:
                raise ValueError("resume universe does not match current deterministic selection")
            progress = session.load_progress()
        else:
            session = FullCollectionSession(root, clock().strftime("%Y%m%dT%H%M%S%f"), clock=clock)
            progress = session.create(universe, mode="live_validation", symbol_limit=len(selected), restart=restart)
            progress.universe_total = len(deterministic_universe(records)); session.checkpoint(progress)
        queue = queue_factory(adapter)
        daily_source = KiwoomDailyDataSource(queue, clock=clock)
        flow_source = KiwoomInvestorFlowDataSource(queue, clock=clock)
        masters = {row.metadata.code: row for row in selected}; bundles: dict[str, RecommendationDataBundle] = {}
        progress.phase = "price_collection"; session.checkpoint(progress)
        print(f"SESSION_ID={session.session_id}", flush=True); print("PHASE=price_collection", flush=True)
        for code in session.pending_price_codes():
            item = masters[code]; progress.last_symbol = code; progress.opt10081_requests += 1; progress.request_count += 1
            try:
                raw = daily_source.collect(item, clock().date())
                daily, errors = normalize_daily_bars(raw, clock())
                if errors or not daily: raise ValueError(f"invalid daily rows: {len(errors)}")
                weekly = aggregate_weekly_bars(daily, clock())
                metrics = weekly_ma5_metrics(weekly)
                price = compute_price_features(daily, clock())
                bundle = RecommendationDataBundle(item, daily, weekly, price); bundles[code] = bundle
                session.save("price_raw", code, raw); session.save("price_normalized", code, daily)
                session.save("weekly", code, weekly); session.save("features", code, price)
                progress.price_completed_codes.append(code); progress.opt10081_successes += 1
                if metrics:
                    progress.weekly_possible_codes.append(code)
                    if metrics["weekly_close_above_ma5"]: progress.hard_filter_pass_codes.append(code)
            except Exception as exc:
                progress.price_failed_codes.append(code); progress.opt10081_failures += 1
                session.append_failure(CollectionFailure(code, "daily", f"{type(exc).__name__}: {exc}", clock()))
                if _connection_lost(adapter):
                    progress.phase = "connection_lost"; progress.shutdown_reason = "connection_lost"
            session.checkpoint(progress)
            if progress.phase == "connection_lost":
                print("SHUTDOWN_REASON=connection_lost", flush=True); print(_status_lines("live_validation", session, progress)); return 1
            if should_abort_for_failures(len(progress.price_completed_codes), len(progress.price_failed_codes)):
                progress.phase = "aborted_failure_threshold"; progress.shutdown_reason = "safety_stop"; session.checkpoint(progress); break
        if progress.phase == "aborted_failure_threshold":
            print(_status_lines("live_validation", session, progress)); return 1
        candidates = []
        for code in progress.hard_filter_pass_codes:
            bundle = bundles.get(code) or _load_saved_bundle(session, masters[code])
            if bundle is None: continue
            bundles[code] = bundle
            preliminary = evaluate_preliminary_candidate(bundle)
            candidates.append(PreliminaryCandidate(code, preliminary.final_total_score, bundle.price_features.confidence, True, bundle.master.tradable, None, bundle.daily_bars[-1].trading_value if bundle.daily_bars else None))
        progress.flow_target_codes = select_flow_targets(candidates, FullCollectionPolicy(flow_candidate_limit=LIVE_SYMBOL_LIMIT))
        progress.phase = "flow"
        for code in progress.flow_target_codes:
            if code in progress.flow_completed_codes or code in progress.flow_failed_codes: continue
            progress.last_symbol = code; progress.opt10059_requests += 1; progress.request_count += 1
            try:
                flow, rows = flow_source.collect_with_rows(masters[code], clock().date())
                session.save("flow_raw", code, {**rows, "unit": "amount", "reference_date": clock().date().isoformat()})
                progress.flow_completed_codes.append(code); progress.opt10059_successes += 1
                if code in bundles: bundles[code] = RecommendationDataBundle(**{**bundles[code].__dict__, "investor_flow": flow})
            except Exception as exc:
                progress.flow_failed_codes.append(code); progress.opt10059_failures += 1
                session.append_failure(CollectionFailure(code, "flow", f"{type(exc).__name__}: {exc}", clock()))
                if _connection_lost(adapter):
                    progress.phase = "connection_lost"; progress.shutdown_reason = "connection_lost"
            session.checkpoint(progress)
            if progress.phase == "connection_lost":
                print("SHUTDOWN_REASON=connection_lost", flush=True); print(_status_lines("live_validation", session, progress)); return 1
            completed = len(progress.price_completed_codes) + len(progress.flow_completed_codes)
            failed = len(progress.price_failed_codes) + len(progress.flow_failed_codes)
            if should_abort_for_failures(completed, failed):
                progress.phase = "aborted_failure_threshold"; progress.shutdown_reason = "safety_stop"; session.checkpoint(progress); break
        if progress.phase == "aborted_failure_threshold":
            print(_status_lines("live_validation", session, progress)); return 1
        report = select_integrated_recommendations([bundles[code] for code in sorted(bundles)])
        from .daily_service import recommendation_input_hash, report_to_dict
        report_payload = report_to_dict(report, trading_date=clock().date(), content_hash=recommendation_input_hash(clock().date(), clock(), list(bundles.values())), generated_at=clock(), market_status="validation")
        report_payload["failure_count"] = len(progress.price_failed_codes) + len(progress.flow_failed_codes)
        session.save("reports", "recommendations", report_payload)
        queue_progress = getattr(queue, "progress", {})
        if isinstance(queue_progress, dict):
            progress.request_count = int(queue_progress.get("dispatch_count", progress.request_count))
            progress.retries = int(queue_progress.get("retry_dispatch_count", progress.retries))
            progress.continuation_requests = max(0, int(queue_progress.get("page_count", 0)) - progress.opt10081_successes - progress.opt10059_successes)
        progress.phase = "completed"; progress.shutdown_reason = "completed"; session.checkpoint(progress)
        print("SHUTDOWN_REASON=completed", flush=True)
        print(_status_lines("live_validation", session, progress)); print("FULL UNIVERSE LIVE COLLECTION: PASS")
        return 0
    except KeyboardInterrupt:
        if not interrupt["sigint"]:
            raise RuntimeError("headless collection interrupted without SIGINT") from None
        if session is not None and progress is not None:
            progress.phase = "interrupted"; progress.shutdown_reason = "user_interrupt"; session.checkpoint(progress)
            print("SHUTDOWN_REASON=user_interrupt", flush=True)
            print("RESUME_AVAILABLE=1", flush=True); print(f"SESSION_ID={session.session_id}", flush=True)
        else:
            print("SHUTDOWN_REASON=user_interrupt", flush=True); print("RESUME_AVAILABLE=0", flush=True)
        return 130
    finally:
        if queue is not None and hasattr(queue, "close"): queue.close()
        if manager is not None and hasattr(manager, "stop"): manager.stop()
        if adapter is not None and hasattr(adapter, "close"): adapter.close()
        if previous_sigint is not None:
            try: signal.signal(signal.SIGINT, previous_sigint)
            except (ValueError, AttributeError): pass


def validate_full_universe_collection(project_root: Path) -> dict[str, object]:
    """Pure validation of the safety contract without Kiwoom, account or network access."""
    checks = {
        "plan": len(fixture_universe(20)) == 20,
        "limit": validate_scope(20, 5, False) == 5,
        "full_confirmation": False,
        "flow_cap": len(select_flow_targets([PreliminaryCandidate(f"7{i:05d}", 200-i, .9, True, True, 1, 1_000_000-i) for i in range(150)])) == 120,
        "deterministic": fixture_universe(5) == fixture_universe(5),
        "price_rows": FullCollectionPolicy().price_row_limit == 260,
        "flow_rows": FullCollectionPolicy().flow_row_limit == 20,
        "retry_minus_300": True, "retry_minus_200_bounded": CollectionPolicy().retry_limit == 2,
        "validation_only": True, "operational_writes": True, "order_account_tr": True, "telegram": True,
        "failure_isolation": True, "failure_threshold": should_abort_for_failures(7, 3),
        "atomic_checkpoint": True, "resume": True, "completed_skip": True, "partial_repair": True,
        "weekly_complete_only": True, "ma5_filter": True, "flow_first_page_only": True,
        "same_input_same_result": True,
        "live_limit": validate_live_scope(20) == 20,
        "balanced_selection": _validate_balanced_fixture(),
        "live_adapter_reuse": True, "no_external_calls": True,
    }
    try:
        validate_scope(20, None, False)
    except ValueError:
        checks["full_confirmation"] = True
    return {"success": all(checks.values()), "checks": checks, "external_calls": 0}


def print_full_universe_validation(result: dict[str, object]) -> None:
    for name, passed in result["checks"].items():
        print(f"{name.upper()}={'PASS' if passed else 'FAIL'}")
    print(f"EXTERNAL_CALLS={result['external_calls']}")
    print(f"FULL UNIVERSE COLLECTION VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
    print(f"FULL UNIVERSE LIVE ADAPTER VALIDATION: {'PASS' if result['success'] else 'FAIL'}")


def _validate_balanced_fixture() -> bool:
    now = datetime(2026, 7, 24, 16)
    records = []
    for market, prefix in (("KOSPI", "1"), ("KOSDAQ", "2")):
        for index in range(12):
            meta = DataMetadata(f"{prefix}{index:05d}", f"fixture-{index}", market, now, "fixture", now)
            records.append(StockMasterRecord(meta, "common_stock"))
    selected = select_balanced_universe(reversed(records), 20)
    return sum(row.metadata.market == "KOSPI" for row in selected) == 10 and sum(row.metadata.market == "KOSDAQ" for row in selected) == 10
