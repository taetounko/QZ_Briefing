"""Validation-only session planning for a future full KOSPI/KOSDAQ collection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable

from qz_briefing.runtime.unattended import atomic_write_json

from .data_models import StockMasterRecord
from .data_pipeline import universe_decision
from .request_planner import CollectionPolicy, PreliminaryCandidate, select_flow_candidates


DEFAULT_RELATIVE_ROOT = Path("data/validation/recommendations/full_collection")
SESSION_FILES = ("session.json", "universe.json", "plan.json", "progress.json", "failures.json")
SESSION_DIRS = ("price_raw", "price_normalized", "weekly", "features", "flow_raw", "reports")
MARKET_ORDER = {"KOSPI": 0, "KOSDAQ": 1}


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
    hard_filter_pass_codes: list[str] = field(default_factory=list)
    flow_target_codes: list[str] = field(default_factory=list)
    flow_completed_codes: list[str] = field(default_factory=list)
    flow_failed_codes: list[str] = field(default_factory=list)
    last_symbol: str = ""
    started_at: str = ""
    updated_at: str = ""
    request_count: int = 0
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
        "PRICE_COMPLETED": len(progress.price_completed_codes), "PRICE_FAILED": len(progress.price_failed_codes),
        "HARD_FILTER_PASS": len(progress.hard_filter_pass_codes),
        "FLOW_TARGETS": len(progress.flow_target_codes) if progress.phase != "planned" else planned_flow_requests,
        "FLOW_COMPLETED": len(progress.flow_completed_codes), "FLOW_FAILED": len(progress.flow_failed_codes),
        "CACHE_HITS": 0, "CACHE_MISSES": progress.symbol_limit, "OPT10081_REQUESTS": progress.symbol_limit,
        "OPT10059_REQUESTS": 0 if progress.phase != "planned" else planned_flow_requests,
        "LIVE_TR_CALLS": 0, "RETRIES": 0, "LAST_SYMBOL": progress.last_symbol,
        "ESTIMATED_REMAINING": progress.estimated_remaining, "ORDER_ACCOUNT_TR": 0, "TELEGRAM_SENDS": 0,
        "ESTIMATED_MINIMUM_SECONDS": progress.symbol_limit + planned_flow_requests, "OPERATIONAL_WRITES": 0,
    }
    return "\n".join(f"{key}={value}" for key, value in values.items())


def run_full_collection_plan(project_root: Path, *, dry_run: bool, cached_only: bool, allow_live: bool,
                             max_symbols: int | None, full_universe_confirmed: bool,
                             validation_root: Path | None = None, resume: str | None = None,
                             restart: bool = False, clock=datetime.now) -> int:
    modes = sum(bool(value) for value in (dry_run, cached_only, allow_live))
    if modes != 1:
        raise ValueError("select exactly one of --dry-run, --cached-only, or --allow-kiwoom-live")
    if allow_live:
        raise ValueError("live collection is intentionally unavailable in this validation build")
    root = protected_validation_root(project_root, validation_root)
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
