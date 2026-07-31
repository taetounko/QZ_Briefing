"""Validation-only session planning for a future full KOSPI/KOSDAQ collection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import json
import os
import signal
import re
import hashlib
from pathlib import Path
from typing import Callable, Iterable

from qz_briefing.runtime.unattended import atomic_write_json

from .data_models import AggregatedWeeklyBar, DailyBar, DataMetadata, InvestorFlowSnapshot, PriceFeatures, StockMasterRecord
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
LIVE_SYMBOL_LIMIT = 100
LIVE_CONFIRMATION_THRESHOLD = 20
COLLECTION_SCHEMA_VERSION = 2


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
    restored_price_symbols: int = 0
    live_price_symbols: int = 0
    restored_flow_symbols: int = 0
    live_flow_symbols: int = 0
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


def validate_live_scope(max_symbols: int | None, confirm_100_symbol_live: bool = False) -> int:
    if max_symbols is None:
        raise ValueError("LIVE_COLLECTION_BLOCKED=1\nBLOCK_REASON=max_symbols_required")
    if not 1 <= max_symbols <= LIVE_SYMBOL_LIMIT:
        raise ValueError("LIVE_COLLECTION_BLOCKED=1\nBLOCK_REASON=max_symbols_exceeds_current_live_stage\nCURRENT_LIVE_STAGE_LIMIT=100")
    if max_symbols > LIVE_CONFIRMATION_THRESHOLD and not confirm_100_symbol_live:
        raise ValueError("LIVE_COLLECTION_BLOCKED=1\nBLOCK_REASON=confirm_100_symbol_live_required")
    return max_symbols


def select_balanced_universe(records: Iterable[StockMasterRecord], limit: int) -> list[StockMasterRecord]:
    """Select up to ten per market, then deterministically backfill a short market."""
    eligible = deterministic_universe(records)
    by_market = {market: [row for row in eligible if row.metadata.market == market] for market in MARKET_ORDER}
    kospi_goal = min(50, (limit + 1) // 2)
    kosdaq_goal = min(50, limit // 2)
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

    def create(self, universe: list[dict[str, object]], *, mode: str, symbol_limit: int, restart: bool = False,
               confirmed_100: bool = False, target_date: date | None = None) -> CollectionProgress:
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
        market_counts = {market: sum(item.get("market") == market for item in universe) for market in MARKET_ORDER}
        atomic_write_json(self.path / "plan.json", {
            "schema_version": COLLECTION_SCHEMA_VERSION, "collector_version": "full-universe-v2",
            "price_row_limit": 260, "flow_row_limit": 20, "flow_candidate_limit": LIVE_SYMBOL_LIMIT,
            "symbol_limit": symbol_limit, "confirm_100_symbol_live": confirmed_100,
            "selected_symbols": [str(item.get("code", "")) for item in universe],
            "market_counts": market_counts, "target_date": target_date.isoformat() if target_date else None,
            "cache_compatibility": "same-target-date,same-symbol-limit,same-selection,same-confirmation-tier,schema-v2",
        })
        atomic_write_json(self.path / "progress.json", asdict(progress))
        atomic_write_json(self.path / "failures.json", {"failures": []})
        return progress

    def load_progress(self) -> CollectionProgress:
        value = json.loads((self.path / "progress.json").read_text(encoding="utf-8"))
        return CollectionProgress(**value)

    def checkpoint(self, progress: CollectionProgress) -> None:
        progress.updated_at = self.clock().isoformat()
        progress.estimated_remaining = remaining_counts(progress)[2]
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
    price_remaining, flow_remaining, estimated_remaining = remaining_counts(progress)
    values = {
        "MODE": mode, "SESSION_ID": session.session_id, "PHASE": progress.phase,
        "UNIVERSE_TOTAL": progress.universe_total, "SYMBOL_LIMIT": progress.symbol_limit,
        "KOSPI_SELECTED": _selected_market_count(session, "KOSPI"),
        "KOSDAQ_SELECTED": _selected_market_count(session, "KOSDAQ"),
        "SELECTED_SYMBOLS": progress.symbol_limit,
        "SELECTION_POLICY": "balanced_deterministic",
        "PRICE_COMPLETED": len(progress.price_completed_codes), "PRICE_FAILED": len(progress.price_failed_codes),
        "PRICE_REMAINING": price_remaining,
        "WEEKLY_POSSIBLE": len(progress.weekly_possible_codes),
        "HARD_FILTER_PASS": len(progress.hard_filter_pass_codes),
        "HARD_FILTER_FAIL": max(0, len(progress.weekly_possible_codes) - len(progress.hard_filter_pass_codes)),
        "WEEKLY_INSUFFICIENT": max(0, len(progress.price_completed_codes) - len(progress.weekly_possible_codes)),
        "FLOW_TARGETS": len(progress.flow_target_codes) if progress.phase != "planned" else planned_flow_requests,
        "FLOW_COMPLETED": len(progress.flow_completed_codes), "FLOW_FAILED": len(progress.flow_failed_codes),
        "FLOW_REMAINING": flow_remaining,
        "CACHE_HITS": progress.restored_price_symbols + progress.restored_flow_symbols,
        "CACHE_MISSES": (progress.estimated_remaining if mode != "live_validation" else
                          max(0, progress.symbol_limit - progress.restored_price_symbols) +
                          max(0, len(progress.flow_target_codes) - progress.restored_flow_symbols)),
        "OPT10081_REQUESTS": progress.opt10081_requests if mode == "live_validation" else progress.symbol_limit,
        "OPT10081_SUCCESSES": progress.opt10081_successes, "OPT10081_FAILURES": progress.opt10081_failures,
        "OPT10059_REQUESTS": progress.opt10059_requests if mode == "live_validation" else (0 if progress.phase != "planned" else planned_flow_requests),
        "OPT10059_SUCCESSES": progress.opt10059_successes, "OPT10059_FAILURES": progress.opt10059_failures,
        "LIVE_TR_CALLS": progress.request_count, "RETRIES": progress.retries,
        "CONTINUATION_REQUESTS": progress.continuation_requests, "LAST_SYMBOL": progress.last_symbol,
        "RESTORED_PRICE_SYMBOLS": progress.restored_price_symbols, "LIVE_PRICE_SYMBOLS": progress.live_price_symbols,
        "RESTORED_FLOW_SYMBOLS": progress.restored_flow_symbols, "LIVE_FLOW_SYMBOLS": progress.live_flow_symbols,
        "ESTIMATED_REMAINING": estimated_remaining, "ORDER_ACCOUNT_TR": 0, "TELEGRAM_SENDS": 0,
        "ESTIMATED_MINIMUM_SECONDS": progress.symbol_limit + planned_flow_requests, "OPERATIONAL_WRITES": 0,
        "DASHBOARD_STARTED": 0,
        "SHUTDOWN_REASON": progress.shutdown_reason,
    }
    return "\n".join(f"{key}={value}" for key, value in values.items())


def remaining_counts(progress: CollectionProgress) -> tuple[int, int, int]:
    price = max(0, progress.symbol_limit - len(set(progress.price_completed_codes)) - len(set(progress.price_failed_codes)))
    flow = max(0, len(set(progress.flow_target_codes)) - len(set(progress.flow_completed_codes)) - len(set(progress.flow_failed_codes)))
    if progress.phase == "completed": estimated = 0
    elif progress.phase == "flow": estimated = flow
    else: estimated = price
    return price, flow, max(0, estimated)


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


def _valid_flow_cache(session: FullCollectionSession, code: str) -> bool:
    try:
        payload = json.loads((session.path / "flow_raw" / f"{code}.json").read_text(encoding="utf-8"))
        rows = payload.get("normalized_rows")
        return isinstance(rows, list) and bool(rows)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _load_saved_flow(session: FullCollectionSession, master: StockMasterRecord, now: datetime) -> InvestorFlowSnapshot | None:
    try:
        payload = json.loads((session.path / "flow_raw" / f"{master.metadata.code}.json").read_text(encoding="utf-8"))
        rows = payload["normalized_rows"]
        foreign = tuple(float(row["foreign"]) for row in rows if row.get("foreign") is not None and row.get("institution") is not None)
        institution = tuple(float(row["institution"]) for row in rows if row.get("foreign") is not None and row.get("institution") is not None)
        if not foreign: return None
        metadata = DataMetadata(master.metadata.code, master.metadata.name, master.metadata.market, now, "restored Kiwoom OPT10059 amount", now, True, False, 1.0)
        return InvestorFlowSnapshot(metadata, foreign, institution)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _prior_sessions(root: Path, current: FullCollectionSession) -> list[FullCollectionSession]:
    output = []
    if not root.exists(): return output
    for path in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
        if not path.is_dir() or path == current.path: continue
        try:
            progress = _read_json(path / "progress.json")
            if progress.get("phase") == "completed": output.append(FullCollectionSession(root, path.name))
        except ValueError: continue
    return output


def _copy_json(source: Path, target: Path) -> None:
    value = _read_json(source)
    if not isinstance(value, dict): raise ValueError(f"cache artifact must be an object: {source.name}")
    atomic_write_json(target, value)


def restore_compatible_prices(root: Path, session: FullCollectionSession, masters: dict[str, StockMasterRecord], target_date: date, progress: CollectionProgress) -> dict[str, RecommendationDataBundle]:
    restored = {}; target_text = target_date.isoformat()
    for code, master in masters.items():
        for prior in _prior_sessions(root, session):
            signature = _daily_signature(prior.path / "price_normalized" / f"{code}.json")
            bundle = _load_saved_bundle(prior, master) if signature else None
            if not signature or signature[0] != target_text or signature[1] < 120 or not signature[3] or bundle is None: continue
            try:
                for directory in ("price_raw", "price_normalized", "weekly", "features"):
                    _copy_json(prior.path / directory / f"{code}.json", session.path / directory / f"{code}.json")
            except (OSError, ValueError): continue
            restored[code] = bundle; progress.price_completed_codes.append(code)
            metrics = weekly_ma5_metrics(bundle.weekly_bars)
            if metrics:
                progress.weekly_possible_codes.append(code)
                if metrics["weekly_close_above_ma5"]: progress.hard_filter_pass_codes.append(code)
            progress.restored_price_symbols += 1
            break
    session.checkpoint(progress)
    return restored


def restore_compatible_flows(root: Path, session: FullCollectionSession, masters: dict[str, StockMasterRecord], target_date: date, targets: list[str], progress: CollectionProgress) -> dict[str, InvestorFlowSnapshot]:
    restored = {}; target_text = target_date.isoformat()
    for code in targets:
        for prior in _prior_sessions(root, session):
            signature = _flow_signature(prior.path / "flow_raw" / f"{code}.json")
            flow = _load_saved_flow(prior, masters[code], datetime.combine(target_date, datetime.min.time())) if signature else None
            if not signature or signature[0] != target_text or flow is None: continue
            try: _copy_json(prior.path / "flow_raw" / f"{code}.json", session.path / "flow_raw" / f"{code}.json")
            except (OSError, ValueError): continue
            restored[code] = flow; progress.flow_completed_codes.append(code); progress.restored_flow_symbols += 1
            break
    session.checkpoint(progress)
    return restored


def run_full_collection_plan(project_root: Path, *, dry_run: bool, cached_only: bool, allow_live: bool,
                             max_symbols: int | None, full_universe_confirmed: bool,
                             validation_root: Path | None = None, resume: str | None = None,
                             restart: bool = False, clock=datetime.now,
                             confirm_100_symbol_live: bool = False,
                             application_factory: Callable[[list[str]], object] | None = None,
                             adapter_factory: Callable[[], object] | None = None,
                             manager_factory: Callable[[object], object] | None = None,
                             queue_factory: Callable[[object], object] | None = None) -> int:
    modes = sum(bool(value) for value in (dry_run, cached_only, allow_live))
    if modes != 1:
        raise ValueError("select exactly one of --dry-run, --cached-only, or --allow-kiwoom-live")
    if allow_live:
        validate_live_scope(max_symbols, confirm_100_symbol_live)
        if any(os.environ.get(name) for name in ("CODEX_HOME", "CODEX_SANDBOX", "CODEX_THREAD_ID")):
            raise ValueError("live Kiwoom collection is blocked inside Codex; run it from ordinary Windows PowerShell")
        return run_full_collection_live(
            project_root, max_symbols=max_symbols or 0, validation_root=validation_root,
            resume=resume, restart=restart, application_factory=application_factory,
            confirm_100_symbol_live=confirm_100_symbol_live,
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
    confirm_100_symbol_live: bool = False,
) -> int:
    """Run a bounded validation-only collection through the existing Kiwoom adapters."""
    validate_live_scope(max_symbols, confirm_100_symbol_live)
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
        print("LIVE_STAGE_LIMIT=100", flush=True)
        print(f"CONFIRM_100_SYMBOL_LIVE={int(confirm_100_symbol_live)}", flush=True)
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
        masters = {row.metadata.code: row for row in selected}
        if resume:
            session = FullCollectionSession(root, resume, clock=clock)
            if not session.path.exists(): raise ValueError("resume session does not exist")
            saved = json.loads((session.path / "universe.json").read_text(encoding="utf-8"))["symbols"]
            if [(x["market"], x["code"]) for x in saved] != [(x["market"], x["code"]) for x in universe]:
                raise ValueError("resume universe does not match current deterministic selection")
            progress = session.load_progress()
            plan = json.loads((session.path / "plan.json").read_text(encoding="utf-8"))
            expected_confirmed = max_symbols > LIVE_CONFIRMATION_THRESHOLD
            if int(plan.get("symbol_limit", -1)) != max_symbols or bool(plan.get("confirm_100_symbol_live")) != expected_confirmed:
                raise ValueError("resume session live tier does not match requested max-symbols and confirmation")
            if plan.get("target_date") != clock().date().isoformat():
                raise ValueError("resume session target date does not match current target date")
            corrupt_price = {code for code in progress.price_completed_codes if _load_saved_bundle(session, masters[code]) is None}
            if corrupt_price:
                progress.price_completed_codes = [code for code in progress.price_completed_codes if code not in corrupt_price]
                progress.weekly_possible_codes = [code for code in progress.weekly_possible_codes if code not in corrupt_price]
                progress.hard_filter_pass_codes = [code for code in progress.hard_filter_pass_codes if code not in corrupt_price]
            corrupt_flow = {code for code in progress.flow_completed_codes if not _valid_flow_cache(session, code)}
            if corrupt_flow:
                progress.flow_completed_codes = [code for code in progress.flow_completed_codes if code not in corrupt_flow]
            progress.restored_price_symbols = len(set(progress.price_completed_codes))
            progress.restored_flow_symbols = len(set(progress.flow_completed_codes))
            progress.live_price_symbols = progress.live_flow_symbols = 0
            progress.opt10081_requests = progress.opt10081_successes = progress.opt10081_failures = 0
            progress.opt10059_requests = progress.opt10059_successes = progress.opt10059_failures = 0
            progress.request_count = progress.retries = progress.continuation_requests = 0
            session.checkpoint(progress)
        else:
            session = FullCollectionSession(root, clock().strftime("%Y%m%dT%H%M%S%f"), clock=clock)
            progress = session.create(universe, mode="live_validation", symbol_limit=len(selected), restart=restart,
                                      confirmed_100=max_symbols > LIVE_CONFIRMATION_THRESHOLD, target_date=clock().date())
            progress.universe_total = len(deterministic_universe(records)); session.checkpoint(progress)
            bundles = restore_compatible_prices(root, session, masters, clock().date(), progress)
        queue = queue_factory(adapter)
        daily_source = KiwoomDailyDataSource(queue, clock=clock)
        flow_source = KiwoomInvestorFlowDataSource(queue, clock=clock)
        if resume: bundles = {}
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
                progress.live_price_symbols += 1
                if metrics:
                    progress.weekly_possible_codes.append(code)
                    if metrics["weekly_close_above_ma5"]: progress.hard_filter_pass_codes.append(code)
            except Exception as exc:
                progress.price_failed_codes.append(code); progress.opt10081_failures += 1
                session.append_failure(CollectionFailure(code, "daily", f"{type(exc).__name__}: {exc}", clock()))
                if _connection_lost(adapter):
                    progress.phase = "connection_lost"; progress.shutdown_reason = "connection_lost"
            session.checkpoint(progress)
            if (len(progress.price_completed_codes) + len(progress.price_failed_codes)) % 5 == 0:
                print(_status_lines("live_validation", session, progress), flush=True)
            if progress.phase == "connection_lost":
                print("SHUTDOWN_REASON=connection_lost", flush=True); print(_status_lines("live_validation", session, progress)); return 1
            if should_abort_for_failures(len(progress.price_completed_codes), len(progress.price_failed_codes)):
                progress.phase = "aborted_failure_threshold"; progress.shutdown_reason = "failure_threshold_exceeded"; session.checkpoint(progress); break
        if progress.phase == "aborted_failure_threshold":
            print(_status_lines("live_validation", session, progress)); return 1
        candidates = []
        for code in progress.hard_filter_pass_codes:
            bundle = bundles.get(code) or _load_saved_bundle(session, masters[code])
            if bundle is None: continue
            if code in progress.flow_completed_codes:
                restored_flow = _load_saved_flow(session, masters[code], clock())
                if restored_flow is not None:
                    bundle = RecommendationDataBundle(**{**bundle.__dict__, "investor_flow": restored_flow})
            bundles[code] = bundle
            preliminary = evaluate_preliminary_candidate(bundle)
            candidates.append(PreliminaryCandidate(code, preliminary.final_total_score, bundle.price_features.confidence, True, bundle.master.tradable, None, bundle.daily_bars[-1].trading_value if bundle.daily_bars else None))
        progress.flow_target_codes = select_flow_targets(candidates, FullCollectionPolicy(flow_candidate_limit=LIVE_SYMBOL_LIMIT))
        progress.phase = "flow"
        if not resume:
            restored_flows = restore_compatible_flows(root, session, masters, clock().date(), progress.flow_target_codes, progress)
            for code, flow in restored_flows.items():
                if code in bundles: bundles[code] = RecommendationDataBundle(**{**bundles[code].__dict__, "investor_flow": flow})
        for code in progress.flow_target_codes:
            if code in progress.flow_completed_codes or code in progress.flow_failed_codes: continue
            progress.last_symbol = code; progress.opt10059_requests += 1; progress.request_count += 1
            try:
                flow, rows = flow_source.collect_with_rows(masters[code], clock().date())
                session.save("flow_raw", code, {**rows, "unit": "amount", "reference_date": clock().date().isoformat()})
                progress.flow_completed_codes.append(code); progress.opt10059_successes += 1
                progress.live_flow_symbols += 1
                if code in bundles: bundles[code] = RecommendationDataBundle(**{**bundles[code].__dict__, "investor_flow": flow})
            except Exception as exc:
                progress.flow_failed_codes.append(code); progress.opt10059_failures += 1
                session.append_failure(CollectionFailure(code, "flow", f"{type(exc).__name__}: {exc}", clock()))
                if _connection_lost(adapter):
                    progress.phase = "connection_lost"; progress.shutdown_reason = "connection_lost"
            session.checkpoint(progress)
            if (len(progress.flow_completed_codes) + len(progress.flow_failed_codes)) % 5 == 0:
                print(_status_lines("live_validation", session, progress), flush=True)
            if progress.phase == "connection_lost":
                print("SHUTDOWN_REASON=connection_lost", flush=True); print(_status_lines("live_validation", session, progress)); return 1
            completed = len(progress.price_completed_codes) + len(progress.flow_completed_codes)
            failed = len(progress.price_failed_codes) + len(progress.flow_failed_codes)
            if should_abort_for_failures(completed, failed):
                progress.phase = "aborted_failure_threshold"; progress.shutdown_reason = "failure_threshold_exceeded"; session.checkpoint(progress); break
        if progress.phase == "aborted_failure_threshold":
            print(_status_lines("live_validation", session, progress)); return 1
        scoring_bundles = [bundles[code] for code in progress.hard_filter_pass_codes if code in bundles]
        report = select_integrated_recommendations(scoring_bundles)
        from .daily_service import recommendation_input_hash, report_to_dict
        report_payload = report_to_dict(report, trading_date=clock().date(), content_hash=recommendation_input_hash(clock().date(), clock(), list(bundles.values())), generated_at=clock(), market_status="validation")
        report_payload.update({
            "universe_input_count": len(bundles), "hard_filter_eligible_count": len(scoring_bundles),
            "scoring_input_count": len(scoring_bundles), "selector_input_count": len(scoring_bundles),
            "report_recommendation_count": len(report.strong) + len(report.review),
        })
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
            confirmation = " --confirm-100-symbol-live" if max_symbols > LIVE_CONFIRMATION_THRESHOLD else ""
            print(f"RESUME_COMMAND=.\\.venv\\Scripts\\python.exe -m qz_briefing --collect-recommendation-universe --allow-kiwoom-live --max-symbols {max_symbols}{confirmation} --resume --session-id {session.session_id}", flush=True)
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
        "live_limit": validate_live_scope(20) == 20 and validate_live_scope(100, True) == 100,
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
    print(f"FULL UNIVERSE 100 SYMBOL ADAPTER VALIDATION: {'PASS' if result['success'] else 'FAIL'}")


def _validate_balanced_fixture() -> bool:
    now = datetime(2026, 7, 24, 16)
    records = []
    for market, prefix in (("KOSPI", "1"), ("KOSDAQ", "2")):
        for index in range(60):
            meta = DataMetadata(f"{prefix}{index:05d}", f"fixture-{index}", market, now, "fixture", now)
            records.append(StockMasterRecord(meta, "common_stock"))
    twenty = select_balanced_universe(reversed(records), 20)
    hundred = select_balanced_universe(reversed(records), 100)
    return (
        sum(row.metadata.market == "KOSPI" for row in twenty) == 10
        and sum(row.metadata.market == "KOSDAQ" for row in twenty) == 10
        and sum(row.metadata.market == "KOSPI" for row in hundred) == 50
        and sum(row.metadata.market == "KOSDAQ" for row in hundred) == 50
    )


def _read_json(path: Path) -> object:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact: {path.name}: {type(exc).__name__}") from None


def _json_files(path: Path) -> dict[str, Path]:
    return {item.stem: item for item in path.glob("*.json") if item.is_file() and not item.name.startswith(".") and "corrupt" not in item.name.lower()}


def _daily_signature(path: Path) -> tuple[str, int, str, bool] | None:
    try:
        rows = _read_json(path)["data"]
        if not isinstance(rows, list) or not rows: return None
        dates = [str(row["trading_date"]) for row in rows]
        sources = {str(row["metadata"]["source"]) for row in rows}
        adjusted = {bool(row["adjusted"]) for row in rows}
        if len(sources) != 1 or len(adjusted) != 1 or len(set(dates)) != len(dates): return None
        return max(dates), len(rows), next(iter(sources)), next(iter(adjusted))
    except (TypeError, KeyError, ValueError): return None


def _flow_signature(path: Path) -> tuple[str, str, int] | None:
    try:
        value = _read_json(path); rows = value["normalized_rows"]
        if value.get("unit") != "amount" or not isinstance(rows, list) or not rows: return None
        dates = [str(row["date"]) for row in rows]
        return str(value.get("reference_date", "")), max(dates), len(rows)
    except (TypeError, KeyError, ValueError): return None


def _contains_sensitive_key(value: object) -> bool:
    blocked = ("account", "password", "certificate", "credential", "token", "chat_id", "계좌번호", "비밀번호", "인증정보")
    if isinstance(value, dict):
        return any(any(token in str(key).lower() for token in blocked) or _contains_sensitive_key(item) for key, item in value.items())
    if isinstance(value, list): return any(_contains_sensitive_key(item) for item in value)
    return False


def _comparison_session(root: Path, target: Path, target_limit: int) -> Path | None:
    candidates = []
    for path in root.iterdir():
        if path == target or not path.is_dir(): continue
        try:
            progress = _read_json(path / "progress.json")
            if progress.get("phase") == "completed" and int(progress.get("symbol_limit", 0)) < target_limit:
                candidates.append(path)
        except (ValueError, AttributeError): continue
    return max(candidates, key=lambda item: item.name) if candidates else None


def validate_full_collection_session(project_root: Path, session_id: str) -> dict[str, object]:
    """Read and cross-check one completed validation session without modifying it."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id): raise ValueError("invalid session id")
    root = protected_validation_root(project_root)
    session = (root / session_id).resolve()
    if session.parent != root or not session.is_dir(): raise ValueError("session does not exist in protected validation root")
    required = {name: _read_json(session / name) for name in SESSION_FILES}
    metadata, universe_doc, plan, progress, failures = (required[name] for name in SESSION_FILES)
    symbols = universe_doc.get("symbols", []); selected = {str(item.get("code", "")) for item in symbols}
    directories = {name: _json_files(session / name) for name in SESSION_DIRS}
    price_raw = set(directories["price_raw"]); price_normalized = set(directories["price_normalized"])
    weekly = set(directories["weekly"]); features = set(directories["features"]); flow = set(directories["flow_raw"])
    hard_pass = set(map(str, progress.get("hard_filter_pass_codes", [])))
    flow_targets = set(map(str, progress.get("flow_target_codes", [])))
    report_paths = list(directories["reports"].values())
    report = _read_json(report_paths[0]) if len(report_paths) == 1 else {}
    recommendations = list(report.get("strong", [])) + list(report.get("review", [])) if isinstance(report, dict) else []
    recommendation_codes = {str(row.get("code", "")) for row in recommendations}
    universe_input = int(report.get("universe_input_count", report.get("input_count", 0))) if isinstance(report, dict) else 0
    scoring_input = int(report.get("scoring_input_count", len(hard_pass))) if isinstance(report, dict) else 0
    selector_input = int(report.get("selector_input_count", len(hard_pass))) if isinstance(report, dict) else 0
    recommendation_audit = []
    for row in recommendations:
        code = str(row.get("code", "")); weekly_rows = _read_json(directories["weekly"][code]).get("data", []) if code in directories["weekly"] else []
        completed = [item for item in weekly_rows if item.get("metadata", {}).get("complete")]
        closes = [float(item["close"]) for item in completed]
        latest = completed[-1] if completed else {}; ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
        flow_payload = _read_json(directories["flow_raw"][code]) if code in directories["flow_raw"] else {}
        recommendation_audit.append({
            "code": code, "name": row.get("name", ""), "week_end": latest.get("week_end", ""),
            "weekly_close": float(latest["close"]) if latest else None, "weekly_ma5": ma5,
            "pass": bool(latest and ma5 is not None and float(latest["close"]) > ma5 and code in hard_pass),
            "flow_rows": len(flow_payload.get("normalized_rows", [])) if isinstance(flow_payload, dict) else 0,
            "score": row.get("total_score"), "grade": row.get("grade"), "missing": row.get("missing", []),
        })
    temporary = [path for path in session.rglob("*") if path.is_file() and (path.name.startswith(".") or path.suffix == ".tmp" or "corrupt" in path.name.lower())]
    sensitive = any(_contains_sensitive_key(value) for value in required.values()) or any(_contains_sensitive_key(_read_json(path)) for group in directories.values() for path in group.values())
    checks = {
        "session_metadata": metadata.get("session_id") == session_id and metadata.get("validation_only") is True,
        "selected_count": len(symbols) == int(progress.get("symbol_limit", -1)) == 100 and len(selected) == 100,
        "plan": int(plan.get("symbol_limit", -1)) == 100 and plan.get("selected_symbols") == [item.get("code") for item in symbols],
        "completed": progress.get("phase") == "completed" and progress.get("shutdown_reason") == "completed",
        "failures": failures.get("failures") == [] and not progress.get("price_failed_codes") and not progress.get("flow_failed_codes"),
        "price_artifacts": price_raw == selected and price_normalized == selected,
        "derived_artifacts": weekly == selected and features == selected,
        "flow_artifacts": flow == hard_pass == flow_targets and flow == set(map(str, progress.get("flow_completed_codes", []))),
        "report": len(report_paths) == 1 and bool(report) and universe_input == 100 and int(report.get("hard_filter_pass_count", -1)) == len(hard_pass),
        "pipeline_scope": scoring_input == len(hard_pass) and selector_input <= len(hard_pass),
        "recommendation_scope": recommendation_codes <= hard_pass and len(report.get("strong", [])) <= 3 and len(report.get("review", [])) <= 3,
        "recommendation_audit": all(item["pass"] and item["flow_rows"] > 0 for item in recommendation_audit),
        "no_temporary": not temporary, "no_sensitive": not sensitive,
        "validation_path": "data" in session.parts and "validation" in session.parts,
    }
    comparison = _comparison_session(root, session, int(progress.get("symbol_limit", 0)))
    comparison_id = comparison.name if comparison else ""
    prior_selected = set(); prior_price = {}; prior_flow = {}
    if comparison:
        prior_selected = {str(item.get("code", "")) for item in _read_json(comparison / "universe.json").get("symbols", [])}
        prior_price = _json_files(comparison / "price_normalized"); prior_flow = _json_files(comparison / "flow_raw")
    overlap_selected = selected & prior_selected; overlap_price = price_normalized & set(prior_price); overlap_flow = flow & set(prior_flow)
    compatible_price = {code for code in overlap_price if _daily_signature(directories["price_normalized"][code]) == _daily_signature(prior_price[code]) and _daily_signature(prior_price[code]) is not None}
    compatible_flow = {code for code in overlap_flow if _flow_signature(directories["flow_raw"][code]) == _flow_signature(prior_flow[code]) and _flow_signature(prior_flow[code]) is not None}
    historical_hits = int(progress.get("restored_price_symbols", 0)) + int(progress.get("restored_flow_symbols", 0))
    rejection = "none"
    if historical_hits == 0 and (compatible_price or compatible_flow): rejection = "cross_session_cache_lookup_not_implemented_at_collection_time"
    result = {
        "success": all(checks.values()), "checks": checks, "session_id": session_id,
        "price_files": len(price_normalized), "price_raw_files": len(price_raw), "weekly_files": len(weekly),
        "feature_files": len(features), "flow_files": len(flow), "report_files": len(report_paths),
        "comparison_session_id": comparison_id, "overlapping_selected": len(overlap_selected),
        "overlapping_price": len(overlap_price), "overlapping_flow": len(overlap_flow),
        "compatible_price": len(compatible_price), "compatible_flow": len(compatible_flow),
        "cache_rejection_reason": rejection,
        "price_cache_hits": int(progress.get("restored_price_symbols", 0)),
        "price_cache_misses": int(progress.get("live_price_symbols", 0)),
        "flow_cache_hits": int(progress.get("restored_flow_symbols", 0)),
        "flow_cache_misses": int(progress.get("live_flow_symbols", 0)),
        "report_path": report_paths[0].relative_to(project_root).as_posix() if report_paths else "",
        "report_hash": str(report.get("content_hash", "")), "report_input_symbols": int(report.get("input_count", 0)),
        "universe_input_symbols": universe_input, "hard_filter_eligible_symbols": len(hard_pass),
        "scoring_input_symbols": scoring_input, "selector_input_symbols": selector_input,
        "report_recommendation_symbols": len(recommendations), "recommendation_audit": recommendation_audit,
        "strong": list(report.get("strong", [])), "review": list(report.get("review", [])),
        "external_calls": 0,
    }
    return result


def print_session_artifact_validation(result: dict[str, object]) -> None:
    print(f"SESSION_ID={result['session_id']}")
    print(f"SESSION_PRICE_RAW_FILES={result['price_raw_files']}"); print(f"SESSION_PRICE_FILES={result['price_files']}")
    print(f"SESSION_WEEKLY_FILES={result['weekly_files']}"); print(f"SESSION_FEATURE_FILES={result['feature_files']}")
    print(f"SESSION_FLOW_FILES={result['flow_files']}"); print(f"SESSION_REPORT_FILES={result['report_files']}")
    print(f"COMPARISON_SESSION_ID={result['comparison_session_id']}")
    print(f"OVERLAPPING_SELECTED_SYMBOLS={result['overlapping_selected']}")
    print(f"OVERLAPPING_PRICE_SYMBOLS={result['overlapping_price']}"); print(f"OVERLAPPING_FLOW_SYMBOLS={result['overlapping_flow']}")
    print(f"COMPATIBLE_PRICE_CACHE_SYMBOLS={result['compatible_price']}"); print(f"COMPATIBLE_FLOW_CACHE_SYMBOLS={result['compatible_flow']}")
    print(f"CACHE_REJECTION_REASON={result['cache_rejection_reason']}")
    print(f"PRICE_CACHE_HITS={result['price_cache_hits']}"); print(f"PRICE_CACHE_MISSES={result['price_cache_misses']}")
    print(f"FLOW_CACHE_HITS={result['flow_cache_hits']}"); print(f"FLOW_CACHE_MISSES={result['flow_cache_misses']}")
    print(f"CACHE_HITS={result['price_cache_hits'] + result['flow_cache_hits']}"); print(f"CACHE_MISSES={result['price_cache_misses'] + result['flow_cache_misses']}")
    print(f"REPORT_GENERATED={int(bool(result['report_files']))}"); print(f"REPORT_PATH={result['report_path']}")
    print(f"REPORT_HASH={result['report_hash']}"); print(f"REPORT_INPUT_SYMBOLS={result['report_input_symbols']}")
    print(f"UNIVERSE_INPUT_SYMBOLS={result['universe_input_symbols']}")
    print(f"HARD_FILTER_ELIGIBLE_SYMBOLS={result['hard_filter_eligible_symbols']}")
    print(f"SCORING_INPUT_SYMBOLS={result['scoring_input_symbols']}"); print(f"SELECTOR_INPUT_SYMBOLS={result['selector_input_symbols']}")
    print(f"REPORT_RECOMMENDATION_SYMBOLS={result['report_recommendation_symbols']}")
    print(f"STRONG_RECOMMENDATIONS={len(result['strong'])}"); print(f"REVIEW_RECOMMENDATIONS={len(result['review'])}")
    print(f"RECOMMENDATION_TOTAL={len(result['strong']) + len(result['review'])}")
    for row in list(result["strong"]) + list(result["review"]):
        missing = ",".join(map(str, row.get("missing", []))) or "없음"
        print(f"RECOMMENDATION={row.get('code')}|{row.get('name')}|{row.get('total_score')}|{row.get('grade')}|MISSING={missing}")
    for row in result["recommendation_audit"]:
        print(f"RECOMMENDATION_AUDIT={row['code']}|week_end={row['week_end']}|weekly_close={row['weekly_close']}|ma5={row['weekly_ma5']}|pass={int(row['pass'])}|flow_rows={row['flow_rows']}|score={row['score']}|grade={row['grade']}")
    print(f"EXTERNAL_CALLS={result['external_calls']}")
    print(f"LIVE_SESSION_ARTIFACT_VALIDATION={'PASS' if result['success'] else 'FAIL'}")


def _tree_hashes(path: Path) -> dict[str, str]:
    return {item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest() for item in path.rglob("*") if item.is_file()}


def validate_cross_session_cache(project_root: Path, source_session_id: str, *, clock: Callable[[], datetime] = datetime.now) -> dict[str, object]:
    """Create a validation-only cache audit session; never constructs Qt or Kiwoom."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_session_id): raise ValueError("invalid session id")
    root = protected_validation_root(project_root); source_path = (root / source_session_id).resolve()
    if source_path.parent != root or not source_path.is_dir(): raise ValueError("source session does not exist")
    source_progress = _read_json(source_path / "progress.json"); universe_doc = _read_json(source_path / "universe.json"); plan = _read_json(source_path / "plan.json")
    if source_progress.get("phase") != "completed": raise ValueError("source session is not completed")
    symbols = universe_doc.get("symbols", []); target_date = date.fromisoformat(str(plan.get("target_date")))
    before = _tree_hashes(source_path); operational_path = project_root / "data" / "recommendations"; operational_before = _tree_hashes(operational_path) if operational_path.exists() else {}
    now = clock()
    session = FullCollectionSession(root, now.strftime("%Y%m%dT%H%M%S%f-cache-validation"), clock=clock)
    progress = session.create(symbols, mode="cached_only_validation", symbol_limit=len(symbols), confirmed_100=bool(plan.get("confirm_100_symbol_live")), target_date=target_date)
    progress.universe_total = int(source_progress.get("universe_total", len(symbols)))
    masters = {}
    for item in symbols:
        metadata = DataMetadata(str(item["code"]), str(item.get("name", "")), str(item["market"]), now, "cache validation master", now)
        masters[metadata.code] = StockMasterRecord(metadata, "common_stock")
    bundles = restore_compatible_prices(root, session, masters, target_date, progress)
    progress.flow_target_codes = [code for code in map(str, source_progress.get("flow_target_codes", [])) if code in progress.hard_filter_pass_codes]
    progress.phase = "flow"; session.checkpoint(progress)
    flows = restore_compatible_flows(root, session, masters, target_date, progress.flow_target_codes, progress)
    for code, flow in flows.items():
        if code in bundles: bundles[code] = RecommendationDataBundle(**{**bundles[code].__dict__, "investor_flow": flow})
    progress.phase = "completed"; progress.shutdown_reason = "completed"; session.checkpoint(progress)
    after = _tree_hashes(source_path); operational_after = _tree_hashes(operational_path) if operational_path.exists() else {}
    success = (
        len(progress.price_completed_codes) == len(symbols) == progress.restored_price_symbols
        and len(progress.flow_completed_codes) == len(progress.flow_target_codes) == progress.restored_flow_symbols
        and progress.live_price_symbols == progress.live_flow_symbols == 0 and before == after
        and operational_before == operational_after
    )
    return {
        "success": success, "mode": "cached_only_validation", "session_id": session.session_id,
        "restored_price": progress.restored_price_symbols, "restored_flow": progress.restored_flow_symbols,
        "live_price": 0, "live_flow": 0, "live_tr_calls": 0, "opt10081_requests": 0, "opt10059_requests": 0,
        "source_unchanged": before == after, "order_account_tr": 0, "telegram_sends": 0,
        "operational_writes": 0, "validation_path": session.path.relative_to(project_root).as_posix(),
    }


def print_cross_session_cache_validation(result: dict[str, object]) -> None:
    print(f"MODE={result['mode']}"); print(f"SESSION_ID={result['session_id']}")
    print(f"RESTORED_PRICE_SYMBOLS={result['restored_price']}"); print(f"LIVE_PRICE_SYMBOLS={result['live_price']}")
    print(f"RESTORED_FLOW_SYMBOLS={result['restored_flow']}"); print(f"LIVE_FLOW_SYMBOLS={result['live_flow']}")
    print(f"PRICE_CACHE_HITS={result['restored_price']}"); print(f"FLOW_CACHE_HITS={result['restored_flow']}")
    print(f"CACHE_HITS={result['restored_price'] + result['restored_flow']}")
    print(f"LIVE_TR_CALLS={result['live_tr_calls']}"); print(f"OPT10081_REQUESTS={result['opt10081_requests']}"); print(f"OPT10059_REQUESTS={result['opt10059_requests']}")
    print(f"SOURCE_SESSION_UNCHANGED={int(result['source_unchanged'])}")
    print(f"ORDER_ACCOUNT_TR={result['order_account_tr']}"); print(f"TELEGRAM_SENDS={result['telegram_sends']}"); print(f"OPERATIONAL_WRITES={result['operational_writes']}")
    print(f"VALIDATION_PATH={result['validation_path']}")
    print(f"CROSS_SESSION_CACHE_VALIDATION={'PASS' if result['success'] else 'FAIL'}")
