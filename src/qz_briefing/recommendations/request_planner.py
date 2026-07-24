"""Deterministic two-stage planning for read-only recommendation collection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CollectionMode(str, Enum):
    BOOTSTRAP = "bootstrap"
    DAILY_INCREMENTAL = "daily_incremental"
    REPAIR = "repair"


@dataclass(frozen=True)
class CollectionPolicy:
    investor_candidate_limit: int = 120
    minimum_request_interval_seconds: float = 1.0
    overload_backoff_seconds: tuple[float, ...] = (3.0, 7.0, 15.0)
    retry_limit: int = 2
    minimum_confidence: float = 0.4


@dataclass(frozen=True)
class CacheState:
    code: str
    kind: str
    status: str  # fresh, stale, missing, failed
    retry_count: int = 0
    new_listing: bool = False


@dataclass(frozen=True)
class PreliminaryCandidate:
    code: str
    score: float
    confidence: float
    weekly_close_above_ma5: bool
    tradable: bool = True
    liquidity: float | None = None
    trading_value: float | None = None


@dataclass(frozen=True)
class PlannedRequest:
    code: str
    kind: str
    priority: int
    operation: str
    retry_limit: int
    network_tr: bool


@dataclass
class RequestPlan:
    mode: CollectionMode
    universe_count: int
    requests: list[PlannedRequest]
    cached_requests_skipped: int
    new_count: int
    stale_count: int
    preliminary_candidate_count: int
    detailed_flow_candidate_count: int
    policy: CollectionPolicy
    checkpoint: int = 0
    retry_requests: int = 0

    @property
    def local_master_operations(self) -> int:
        return sum(not item.network_tr for item in self.requests)

    @property
    def network_tr_requests(self) -> int:
        return sum(item.network_tr for item in self.requests)

    @property
    def price_tr_requests(self) -> int:
        return sum(item.kind == "daily" for item in self.requests)

    @property
    def investor_flow_tr_requests(self) -> int:
        return sum(item.kind == "flow" for item in self.requests)

    @property
    def total_planned_operations(self) -> int:
        return len(self.requests)

    @property
    def estimated_minimum_seconds(self) -> float:
        backoff = sum(self.policy.overload_backoff_seconds[:self.retry_requests])
        return self.network_tr_requests * self.policy.minimum_request_interval_seconds + backoff

    @property
    def counts_by_kind(self) -> dict[str, int]:
        return {kind: sum(item.kind == kind for item in self.requests) for kind in ("master", "daily", "flow")}

    # Compatibility aliases for the first offline implementation.
    @property
    def skipped_fresh(self) -> int: return self.cached_requests_skipped
    @property
    def estimated_total_requests(self) -> int: return self.total_planned_operations
    @property
    def minimum_interval_ms(self) -> int: return int(self.policy.minimum_request_interval_seconds * 1000)
    @property
    def overload_backoff_ms(self) -> tuple[int, ...]: return tuple(int(value * 1000) for value in self.policy.overload_backoff_seconds)


def select_flow_candidates(candidates: list[PreliminaryCandidate], policy: CollectionPolicy) -> list[PreliminaryCandidate]:
    eligible = [item for item in candidates if item.tradable and item.weekly_close_above_ma5 and item.confidence >= policy.minimum_confidence and (item.liquidity is None or item.liquidity > 0)]
    eligible.sort(key=lambda item: (-item.score, -item.confidence, -(item.trading_value or 0), item.code))
    return eligible[:max(0, policy.investor_candidate_limit)]


def build_request_plan(
    states: list[CacheState], *, mode: CollectionMode = CollectionMode.BOOTSTRAP,
    candidates: list[PreliminaryCandidate] | None = None,
    universe_codes: list[str] | None = None, checkpoint: int = 0,
    retry_limit: int | None = None, policy: CollectionPolicy | None = None,
) -> RequestPlan:
    policy = policy or CollectionPolicy(retry_limit=retry_limit if retry_limit is not None else 2)
    unique: dict[tuple[str, str], CacheState] = {}
    for state in states:
        key = (state.code, state.kind); current = unique.get(key)
        if current is None or (current.status == "fresh" and state.status != "fresh"): unique[key] = state
    codes = sorted(set(universe_codes or [state.code for state in states]))
    requests: list[PlannedRequest] = [PlannedRequest(code, "master", 0, "GetMaster", policy.retry_limit, False) for code in codes]
    skipped = 0
    for state in unique.values():
        if state.kind != "daily": continue
        include = state.status in {"missing", "stale"} if mode is not CollectionMode.REPAIR else state.status == "failed"
        if state.status == "fresh": skipped += 1
        if include and state.retry_count < policy.retry_limit:
            requests.append(PlannedRequest(state.code, "daily", 10, "OPT10081", policy.retry_limit, True))
    selected = select_flow_candidates(candidates or [], policy)
    flow_states = {state.code: state for state in unique.values() if state.kind == "flow"}
    for candidate in selected:
        state = flow_states.get(candidate.code)
        status = state.status if state else "missing"
        include = status in {"missing", "stale"} if mode is not CollectionMode.REPAIR else status == "failed"
        if status == "fresh": skipped += 1
        if include and (state is None or state.retry_count < policy.retry_limit):
            requests.append(PlannedRequest(candidate.code, "flow", 20, "OPT10059", policy.retry_limit, True))
    requests.sort(key=lambda item: (item.priority, item.code, item.kind))
    local = [item for item in requests if not item.network_tr]
    network = [item for item in requests if item.network_tr][max(0, checkpoint):]
    requests = local + network
    return RequestPlan(mode, len(codes), requests, skipped, sum(state.new_listing for state in unique.values()), sum(state.status == "stale" for state in unique.values()), len(candidates or []), len(selected), policy, checkpoint)
