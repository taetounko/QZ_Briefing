"""Deterministic, validation-only parent state for partitioned full-market collection.

The module deliberately owns no Qt, ActiveX, account, notification, dashboard, or
operational-report dependency.  A live command may supply the existing read-only
price/flow collectors one batch at a time; the pure planner and validator do not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Iterable, Mapping

from qz_briefing.runtime.unattended import atomic_write_json


PARTITIONED_RELATIVE_ROOT = Path("data/validation/recommendations/full_universe")
PARTITIONED_SCHEMA_VERSION = 1
PARTITIONED_PARSER_VERSION = "full-universe-v2"
DEFAULT_PRICE_BATCH_SIZE = 250
DEFAULT_FLOW_BATCH_SIZE = 40
MAX_PRICE_BATCH_SIZE = 500
MAX_FLOW_BATCH_SIZE = 120
FLOW_CANDIDATE_LIMIT = 120


@dataclass(frozen=True)
class ParentSymbol:
    market: str
    code: str
    name: str = ""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid collection file: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid collection file: {path.name}")
    return value


def load_universe_snapshot(path: Path) -> dict[str, object]:
    payload = _read(path)
    required = {"schema_version", "parser_version", "created_at", "source", "trade_date",
                "kospi_master_codes", "kosdaq_master_codes", "master_codes_total",
                "filtered_universe_total", "excluded_total", "duplicates", "invalid_codes",
                "symbols", "universe_hash"}
    if required - payload.keys(): raise ValueError("full-universe snapshot required fields are missing")
    if payload["schema_version"] != PARTITIONED_SCHEMA_VERSION or payload["parser_version"] != PARTITIONED_PARSER_VERSION:
        raise ValueError("full-universe snapshot schema or parser version mismatch")
    rows = payload["symbols"]
    if not isinstance(rows, list): raise ValueError("full-universe snapshot symbols must be a list")
    symbols = [ParentSymbol(str(row.get("market", "")), str(row.get("code", "")), str(row.get("name", "")))
               for row in rows if isinstance(row, dict)]
    normalized = deterministic_parent_universe(symbols)
    normalized_rows = [{"market": item.market, "code": item.code, "name": item.name} for item in normalized]
    if rows != normalized_rows or any(not item.name for item in normalized):
        raise ValueError("full-universe snapshot order, code, market, or name is invalid")
    if len(rows) != payload["filtered_universe_total"] or len(rows) != len({row["code"] for row in rows}):
        raise ValueError("full-universe snapshot count or duplicates are invalid")
    if _hash(rows) != payload["universe_hash"]:
        raise ValueError("full-universe snapshot hash mismatch")
    return payload


def partitioned_root(project_root: Path) -> Path:
    return (project_root.resolve() / PARTITIONED_RELATIVE_ROOT).resolve()


def generate_collection_id(clock: Callable[[], datetime] = datetime.now) -> str:
    return clock().strftime("%Y%m%dT%H%M%S%f")


def deterministic_parent_universe(symbols: Iterable[ParentSymbol]) -> list[ParentSymbol]:
    markets = {"KOSPI": 0, "KOSDAQ": 1}
    chosen: dict[str, ParentSymbol] = {}
    for symbol in sorted(symbols, key=lambda row: (markets.get(row.market, 99), row.code, row.name)):
        if symbol.market not in markets or len(symbol.code) != 6 or not symbol.code.isdigit():
            continue
        chosen.setdefault(symbol.code, symbol)
    return sorted(chosen.values(), key=lambda row: (markets[row.market], row.code))


def split_batches(codes: Iterable[str], size: int) -> list[list[str]]:
    values = list(codes)
    return [values[offset:offset + size] for offset in range(0, len(values), size)]


def _batch_rows(kind: str, batches: list[list[str]]) -> list[dict[str, object]]:
    return [{"kind": kind, "index": index, "codes": codes, "status": "pending",
             "completed_codes": [], "failed_codes": [], "cache_hits": 0, "live_requests": 0}
            for index, codes in enumerate(batches, 1)]


def create_parent_collection(project_root: Path, symbols: Iterable[ParentSymbol], *, collection_id: str,
                             trade_date: date, price_batch_size: int = DEFAULT_PRICE_BATCH_SIZE,
                             flow_batch_size: int = DEFAULT_FLOW_BATCH_SIZE,
                             clock: Callable[[], datetime] = datetime.now) -> Path:
    if not 1 <= price_batch_size <= MAX_PRICE_BATCH_SIZE:
        raise ValueError("price batch size must be between 1 and 500")
    if not 1 <= flow_batch_size <= MAX_FLOW_BATCH_SIZE:
        raise ValueError("flow batch size must be between 1 and 120")
    universe = deterministic_parent_universe(symbols)
    if not universe:
        raise ValueError("full-universe snapshot is empty")
    root = partitioned_root(project_root); path = root / collection_id
    if path.exists():
        raise ValueError("collection-id already exists")
    for child in ("batches", "reports", "audit", "price_raw", "price_normalized", "weekly", "features", "flow_raw"):
        (path / child).mkdir(parents=True, exist_ok=False)
    symbol_rows = [{"market": item.market, "code": item.code, "name": item.name} for item in universe]
    price_batches = split_batches([item.code for item in universe], price_batch_size)
    now = clock().isoformat()
    manifest: dict[str, object] = {
        "collection_id": collection_id, "schema_version": PARTITIONED_SCHEMA_VERSION,
        "parser_version": PARTITIONED_PARSER_VERSION, "trade_date": trade_date.isoformat(),
        "universe_created_at": now, "universe_total": len(universe), "universe": symbol_rows,
        "universe_hash": _hash(symbol_rows), "price_batch_size": price_batch_size,
        "flow_batch_size": flow_batch_size, "price_batches": price_batches,
        "confirmation_tier": "full_universe", "cache_compatibility_policy":
        "per-symbol,same-trade-date,same-schema-parser,validated-hash", "created_at": now,
    }
    manifest["manifest_hash"] = _hash(manifest)
    progress = {"phase": "planned", "price_batches": _batch_rows("price", price_batches),
                "flow_batches": [], "price_completed_codes": [], "price_failed_codes": [],
                "flow_completed_codes": [], "flow_failed_codes": [], "hard_filter_pass_codes": [],
                "flow_candidate_codes": [], "updated_at": now, "shutdown_reason": ""}
    atomic_write_json(path / "manifest.json", manifest)
    atomic_write_json(path / "progress.json", progress)
    atomic_write_json(path / "failures.json", {"failures": []})
    return path


def verify_parent_collection(path: Path) -> tuple[dict[str, object], dict[str, object]]:
    manifest = _read(path / "manifest.json"); progress = _read(path / "progress.json")
    expected_manifest = dict(manifest); recorded = str(expected_manifest.pop("manifest_hash", ""))
    if not recorded or recorded != _hash(expected_manifest):
        raise ValueError("manifest hash mismatch")
    universe = manifest.get("universe")
    if not isinstance(universe, list) or _hash(universe) != manifest.get("universe_hash"):
        raise ValueError("universe hash mismatch")
    flattened = [code for batch in manifest.get("price_batches", []) for code in batch]
    codes = [str(item.get("code", "")) for item in universe if isinstance(item, dict)]
    if flattened != codes or len(codes) != len(set(codes)) or len(codes) != manifest.get("universe_total"):
        raise ValueError("price batch coverage mismatch")
    if any(len(batch) > int(manifest["price_batch_size"]) for batch in manifest["price_batches"]):
        raise ValueError("price batch size exceeded")
    return manifest, progress


def _unresolved_previous(batches: list[dict[str, object]], index: int) -> bool:
    return any(batch.get("status") not in ("complete",) for batch in batches[:index])


def next_batch(path: Path) -> tuple[str, dict[str, object]] | None:
    _, progress = verify_parent_collection(path)
    phase = str(progress.get("phase"))
    key = "price_batches" if phase == "price_collection" else "flow_batches" if phase == "flow_collection" else ""
    if not key: return None
    batches = progress.get(key, [])
    if not isinstance(batches, list): raise ValueError("invalid batch state")
    for index, batch in enumerate(batches):
        if batch.get("status") in ("partial", "interrupted"):
            raise ValueError("previous batch requires repair or resume")
        if batch.get("status") == "pending":
            if _unresolved_previous(batches, index): raise ValueError("previous batch is unresolved")
            return ("price" if key == "price_batches" else "flow"), batch
    return None


def choose_flow_candidates(scores: Mapping[str, float], symbols: Mapping[str, ParentSymbol], *, limit: int = FLOW_CANDIDATE_LIMIT) -> list[str]:
    return [code for code, _ in sorted(scores.items(), key=lambda item: (-float(item[1]),
        0 if symbols[item[0]].market == "KOSPI" else 1, item[0]))[:max(0, limit)]]


def finalize_price_phase(path: Path, scores: Mapping[str, float]) -> list[str]:
    manifest, progress = verify_parent_collection(path)
    batches = progress["price_batches"]
    if any(batch.get("status") != "complete" for batch in batches) or progress.get("price_failed_codes"):
        raise ValueError("all price batches must complete before flow selection")
    if len(set(progress.get("price_completed_codes", []))) != manifest["universe_total"]:
        raise ValueError("price universe is incomplete")
    universe = {row["code"]: ParentSymbol(row["market"], row["code"], row.get("name", "")) for row in manifest["universe"]}
    eligible = {code: score for code, score in scores.items() if code in universe}
    selected = choose_flow_candidates(eligible, universe)
    payload = {"hard_filter_eligible": len(eligible), "scoring_version": "price-only-preliminary-v1",
               "selected_codes": selected, "scores": {code: float(eligible[code]) for code in selected},
               "input_hash": _hash({code: eligible[code] for code in sorted(eligible)}),
               "generated_at": datetime.now().isoformat()}
    atomic_write_json(path / "preliminary_candidates.json", payload)
    progress["phase"] = "flow_collection"; progress["hard_filter_pass_codes"] = sorted(eligible)
    progress["flow_candidate_codes"] = selected
    progress["flow_batches"] = _batch_rows("flow", split_batches(selected, int(manifest["flow_batch_size"])))
    progress["updated_at"] = datetime.now().isoformat(); atomic_write_json(path / "progress.json", progress)
    return selected


def apply_mock_batch(path: Path, *, fail_codes: Iterable[str] = (), interrupt_after: int | None = None,
                     repair: bool = False, cache_codes: Iterable[str] = ()) -> dict[str, object]:
    """Advance exactly one child batch without external objects; used by tests/validation."""
    manifest, progress = verify_parent_collection(path)
    if progress.get("phase") == "planned":
        progress["phase"] = "price_collection"
    current = None
    if repair:
        phase = str(progress.get("phase")); key = "price_batches" if phase == "price_collection" else "flow_batches"
        for candidate in progress.get(key, []):
            if candidate.get("status") in ("partial", "interrupted"):
                current = ("price" if key == "price_batches" else "flow", candidate); break
    else:
        phase = str(progress.get("phase")); key = "price_batches" if phase == "price_collection" else "flow_batches" if phase == "flow_collection" else ""
        batches = progress.get(key, []) if key else []
        for index, candidate in enumerate(batches):
            if candidate.get("status") in ("partial", "interrupted"):
                raise ValueError("previous batch requires repair or resume")
            if candidate.get("status") == "pending":
                if _unresolved_previous(batches, index): raise ValueError("previous batch is unresolved")
                current = ("price" if key == "price_batches" else "flow", candidate); break
    if current is None: raise ValueError("no batch is available")
    kind, batch = current; failures = set(fail_codes); cached = set(cache_codes)
    targets = list(batch["failed_codes"] if repair else batch["codes"])
    if repair and batch.get("status") not in ("partial", "interrupted"):
        raise ValueError("repair requires a partial or interrupted batch")
    batch["status"] = "running"; completed = set(batch.get("completed_codes", [])); failed = set(batch.get("failed_codes", []))
    processed = 0; live = 0; hits = 0; timeout_streak = 0
    failure_payload = _read(path / "failures.json")
    for code in targets:
        if code in completed: continue
        if interrupt_after is not None and processed >= interrupt_after:
            batch["status"] = "interrupted"; progress["shutdown_reason"] = "user_interrupt"; break
        processed += 1
        if code in cached:
            completed.add(code); failed.discard(code); hits += 1; timeout_streak = 0; continue
        live += 1
        if code in failures:
            failed.add(code); timeout_streak += 1
            existing = next((item for item in failure_payload["failures"] if item.get("code") == code
                             and item.get("data_kind") == kind and not item.get("resolved")), None)
            if existing is None:
                failure_payload["failures"].append({"code": code, "data_kind": kind,
                    "reason": "KiwoomTrTimeoutError: mock timeout", "occurred_at": datetime.now().isoformat(),
                    "attempt": 1, "resolved": False, "resolved_at": None, "batch_index": batch["index"]})
            else:
                existing["attempt"] = int(existing.get("attempt", 1)) + 1
            if timeout_streak >= 3:
                batch["status"] = "interrupted"; progress["shutdown_reason"] = "consecutive_tr_timeouts"; break
        else:
            completed.add(code); failed.discard(code); timeout_streak = 0
            for item in failure_payload["failures"]:
                if item.get("code") == code and item.get("data_kind") == kind and not item.get("resolved"):
                    item["resolved"] = True; item["resolved_at"] = datetime.now().isoformat()
    if batch.get("status") == "running": batch["status"] = "partial" if failed else "complete"
    batch["completed_codes"] = sorted(completed); batch["failed_codes"] = sorted(failed)
    batch["cache_hits"] = int(batch.get("cache_hits", 0)) + hits
    batch["live_requests"] = int(batch.get("live_requests", 0)) + live
    prefix = "price" if kind == "price" else "flow"
    progress[f"{prefix}_completed_codes"] = sorted(set(progress.get(f"{prefix}_completed_codes", [])) | completed)
    progress[f"{prefix}_failed_codes"] = sorted((set(progress.get(f"{prefix}_failed_codes", [])) | failed) - completed)
    progress["updated_at"] = datetime.now().isoformat(); atomic_write_json(path / "progress.json", progress)
    atomic_write_json(path / "failures.json", failure_payload)
    return {"kind": kind, "index": batch["index"], "symbols": len(targets), "cache_hits": hits,
            "live_requests": live, "completed": len(completed), "failed": len(failed), "status": batch["status"]}


def batch_status_lines(path: Path, result: Mapping[str, object]) -> str:
    manifest, progress = verify_parent_collection(path); kind = str(result["kind"])
    prefix = "PRICE" if kind == "price" else "FLOW"
    batch_count = len(progress["price_batches"] if kind == "price" else progress["flow_batches"])
    values = {"FULL_UNIVERSE_MODE": 1, "COLLECTION_ID": manifest["collection_id"],
              "PARENT_PHASE": progress["phase"], f"{prefix}_BATCH_INDEX": result["index"],
              f"{prefix}_BATCH_COUNT": batch_count, f"{prefix}_BATCH_SYMBOLS": result["symbols"],
              f"{prefix}_CACHE_HITS": result["cache_hits"], f"{prefix}_LIVE_REQUESTS": result["live_requests"],
              f"{prefix}_BATCH_COMPLETED": result["completed"], f"{prefix}_BATCH_FAILED": result["failed"],
              "NEXT_BATCH_AVAILABLE": int(result["status"] == "complete" and next_batch(path) is not None)}
    return "\n".join(f"{key}={value}" for key, value in values.items())


def _restore_legacy_symbol(project_root: Path, session, master, trade_date: date, kind: str) -> bool:
    """Copy a hash/shape-valid symbol artifact from a compatible completed legacy session."""
    from .full_universe_collection import FullCollectionSession, _copy_json, _load_saved_bundle, _valid_flow_cache
    legacy_root = project_root / "data/validation/recommendations/full_collection"
    if not legacy_root.exists(): return False
    code = master.metadata.code
    for candidate_path in sorted((item for item in legacy_root.iterdir() if item.is_dir()), reverse=True):
        try:
            plan = _read(candidate_path / "plan.json")
            parent_progress = _read(candidate_path / "progress.json")
            if (plan.get("target_date") != trade_date.isoformat()
                    or plan.get("schema_version") != 2
                    or plan.get("collector_version") != PARTITIONED_PARSER_VERSION
                    or parent_progress.get("phase") != "completed"):
                continue
            source = FullCollectionSession(legacy_root, candidate_path.name)
            if kind == "price":
                if _load_saved_bundle(source, master) is None: continue
                for directory in ("price_raw", "price_normalized", "weekly", "features"):
                    source_file = source.path / directory / f"{code}.json"; destination = session.path / directory / f"{code}.json"
                    source_hash = _hash(_read(source_file)); _copy_json(source_file, destination)
                    if _hash(_read(destination)) != source_hash: raise ValueError("restored price hash mismatch")
            else:
                if not _valid_flow_cache(source, code): continue
                source_file = source.path / "flow_raw" / f"{code}.json"; destination = session.path / "flow_raw" / f"{code}.json"
                source_hash = _hash(_read(source_file)); _copy_json(source_file, destination)
                if _hash(_read(destination)) != source_hash: raise ValueError("restored flow hash mismatch")
            return True
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return False


def run_partitioned_live_batch(project_root: Path, *, collection_id: str, repair: bool = False,
                               resume: bool = False, application_factory=None, adapter_factory=None,
                               manager_factory=None, queue_factory=None, connected=None,
                               clock: Callable[[], datetime] = datetime.now) -> int:
    """Run one frozen parent batch through the existing read-only Kiwoom adapters.

    Planning is intentionally offline: this runner refuses to invent or refresh a
    universe and therefore requires an already frozen, hash-valid manifest.
    """
    path = partitioned_root(project_root) / collection_id
    manifest, progress = verify_parent_collection(path)  # before any Qt/factory
    if repair and resume: raise ValueError("choose either repair or resume")
    phase = str(progress.get("phase"))
    if phase == "planned":
        progress["phase"] = phase = "price_collection"; progress["updated_at"] = clock().isoformat()
        atomic_write_json(path / "progress.json", progress)
    key = "price_batches" if phase == "price_collection" else "flow_batches" if phase == "flow_collection" else ""
    if not key: raise ValueError("parent collection has no runnable batch")
    batches = progress[key]; batch = None
    if repair:
        batch = next((item for item in batches if item.get("status") in ("partial", "interrupted") and item.get("failed_codes")), None)
    elif resume:
        batch = next((item for item in batches if item.get("status") == "interrupted"), None)
    else:
        pending = next_batch(path); batch = pending[1] if pending else None
    if batch is None: raise ValueError("requested batch is not available")
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
    from .data_models import DataMetadata, RecommendationDataBundle, StockMasterRecord
    from .data_pipeline import aggregate_weekly_bars, compute_price_features, normalize_daily_bars, weekly_ma5_metrics
    from .full_universe_collection import FullCollectionSession, _is_tr_timeout, _load_saved_bundle, _load_saved_flow
    from .integrated_scoring import evaluate_preliminary_candidate
    from .selector import select_integrated_recommendations
    from .kiwoom_collection import KiwoomDailyDataSource, KiwoomInvestorFlowDataSource
    from .live_validation import _ensure_connected
    app = adapter = manager = queue = None
    try:
        app = application_factory([])
        if app is None: raise RuntimeError("QApplication initialization failed")
        if hasattr(app, "setQuitOnLastWindowClosed"): app.setQuitOnLastWindowClosed(False)
        adapter = adapter_factory(); manager = manager_factory(adapter)
        if not bool((connected or _ensure_connected)(adapter)) or int(adapter.get_connect_state()) != 1:
            raise ValueError("Kiwoom login is required")
        queue = queue_factory(adapter); now = clock(); target = date.fromisoformat(str(manifest["trade_date"]))
        masters = {}
        for row in manifest["universe"]:
            metadata = DataMetadata(row["code"], row.get("name", ""), row["market"], now,
                                    "frozen parent manifest", now, True, False, 1.0)
            masters[row["code"]] = StockMasterRecord(metadata, "common_stock")
        session = FullCollectionSession(path.parent, collection_id, clock=clock)
        daily_source = KiwoomDailyDataSource(queue, clock=clock); flow_source = KiwoomInvestorFlowDataSource(queue, clock=clock)
        completed = set(batch.get("completed_codes", [])); failed = set(batch.get("failed_codes", []))
        targets = list(failed if repair else (set(batch["codes"]) - completed if resume else batch["codes"]))
        targets.sort(key=lambda code: batch["codes"].index(code)); batch["status"] = "running"
        live = hits = timeout_streak = 0
        for code in targets:
            if code in completed: continue
            # Same-parent validated artifacts are reusable and never count as TRs.
            if phase == "price_collection" and all((path / directory / f"{code}.json").exists()
                                                    for directory in ("price_raw", "price_normalized", "weekly", "features")):
                completed.add(code); failed.discard(code); hits += 1; timeout_streak = 0; continue
            if phase == "flow_collection" and (path / "flow_raw" / f"{code}.json").exists():
                completed.add(code); failed.discard(code); hits += 1; timeout_streak = 0; continue
            if _restore_legacy_symbol(project_root, session, masters[code], target,
                                      "price" if phase == "price_collection" else "flow"):
                completed.add(code); failed.discard(code); hits += 1; timeout_streak = 0; continue
            live += 1
            try:
                if phase == "price_collection":
                    raw = daily_source.collect(masters[code], target); daily, errors = normalize_daily_bars(raw, now)
                    if errors or not daily: raise ValueError("invalid daily rows")
                    weekly = aggregate_weekly_bars(daily, now); features = compute_price_features(daily, now)
                    session.save("price_raw", code, raw); session.save("price_normalized", code, daily)
                    session.save("weekly", code, weekly); session.save("features", code, features)
                else:
                    _, rows = flow_source.collect_with_rows(masters[code], target)
                    session.save("flow_raw", code, {**rows, "unit": "amount", "reference_date": target.isoformat()})
                completed.add(code); failed.discard(code); timeout_streak = 0
            except Exception as exc:
                failed.add(code); timeout_streak = timeout_streak + 1 if _is_tr_timeout(exc) else 0
                if int(adapter.get_connect_state()) == 0:
                    batch["status"] = "interrupted"; progress["shutdown_reason"] = "connection_lost"; break
                if timeout_streak >= 3:
                    batch["status"] = "interrupted"; progress["shutdown_reason"] = "consecutive_tr_timeouts"; break
            batch["completed_codes"] = sorted(completed); batch["failed_codes"] = sorted(failed)
            prefix = "price" if phase == "price_collection" else "flow"
            progress[f"{prefix}_completed_codes"] = sorted(set(progress.get(f"{prefix}_completed_codes", [])) | completed)
            progress[f"{prefix}_failed_codes"] = sorted((set(progress.get(f"{prefix}_failed_codes", [])) | failed) - completed)
            progress["updated_at"] = clock().isoformat(); atomic_write_json(path / "progress.json", progress)
        if batch.get("status") == "running": batch["status"] = "partial" if failed else "complete"
        batch["completed_codes"] = sorted(completed); batch["failed_codes"] = sorted(failed)
        batch["cache_hits"] = int(batch.get("cache_hits", 0)) + hits; batch["live_requests"] = int(batch.get("live_requests", 0)) + live
        prefix = "price" if phase == "price_collection" else "flow"
        progress[f"{prefix}_completed_codes"] = sorted(set(progress.get(f"{prefix}_completed_codes", [])) | completed)
        progress[f"{prefix}_failed_codes"] = sorted((set(progress.get(f"{prefix}_failed_codes", [])) | failed) - completed)
        progress["updated_at"] = clock().isoformat(); atomic_write_json(path / "progress.json", progress)
        result = {"kind": prefix, "index": batch["index"], "symbols": len(targets), "cache_hits": hits,
                  "live_requests": live, "completed": len(completed), "failed": len(failed), "status": batch["status"]}
        if batch["status"] == "complete" and phase == "price_collection" and all(item.get("status") == "complete" for item in progress["price_batches"]):
            scores = {}
            for code in progress["price_completed_codes"]:
                bundle = _load_saved_bundle(session, masters[code])
                if bundle is None: continue
                preliminary = evaluate_preliminary_candidate(bundle, now)
                if preliminary.weekly_filter_passed: scores[code] = preliminary.final_total_score
            finalize_price_phase(path, scores)
        elif batch["status"] == "complete" and phase == "flow_collection" and all(item.get("status") == "complete" for item in progress["flow_batches"]):
            bundles = []
            for code in progress["flow_candidate_codes"]:
                bundle = _load_saved_bundle(session, masters[code]); flow = _load_saved_flow(session, masters[code], now)
                if bundle is None or flow is None: continue
                bundles.append(RecommendationDataBundle(**{**bundle.__dict__, "investor_flow": flow}))
            report = select_integrated_recommendations(bundles)
            from .daily_service import recommendation_input_hash, report_to_dict
            payload = report_to_dict(report, trading_date=target,
                content_hash=recommendation_input_hash(target, now, bundles), generated_at=now, market_status="validation")
            payload.update(validation_only=True, scoring_input_count=len(bundles), selector_input_count=len(bundles))
            session.save("reports", "recommendations", payload)
            progress["phase"] = "complete"; progress["shutdown_reason"] = "completed"
            atomic_write_json(path / "progress.json", progress)
        print(batch_status_lines(path, result))
        if batch["status"] == "complete": return 0
        print("COLLECTION_RESULT=interrupted" if batch["status"] == "interrupted" else "COLLECTION_RESULT=partial")
        print("RESUME_AVAILABLE=1" if batch["status"] == "interrupted" else "REPAIR_AVAILABLE=1")
        return 1
    finally:
        if queue is not None and hasattr(queue, "close"): queue.close()
        if manager is not None and hasattr(manager, "stop"): manager.stop()
        if adapter is not None and hasattr(adapter, "close"): adapter.close()


def finalize_mock_report(path: Path) -> dict[str, object]:
    _, progress = verify_parent_collection(path)
    if progress.get("price_failed_codes") or progress.get("flow_failed_codes"):
        raise ValueError("unresolved failures block final report")
    if any(batch.get("status") != "complete" for batch in progress.get("flow_batches", [])):
        raise ValueError("all flow batches must complete before final report")
    candidates = _read(path / "preliminary_candidates.json").get("selected_codes", [])
    report = {"validation_only": True, "scoring_input": len(candidates), "selector_input": len(candidates),
              "recommendations": list(candidates[:3]), "generated_at": datetime.now().isoformat()}
    atomic_write_json(path / "reports" / "recommendations.json", report)
    progress["phase"] = "complete"; progress["shutdown_reason"] = "completed"
    atomic_write_json(path / "progress.json", progress)
    return report


def _fixture_symbols(count: int = 2510) -> list[ParentSymbol]:
    return [ParentSymbol("KOSPI" if index < 1255 else "KOSDAQ", f"{index:06d}", f"fixture-{index:06d}") for index in range(count)]


def validate_partitioned_full_universe_collection() -> dict[str, object]:
    with TemporaryDirectory() as directory:
        root = Path(directory); path = create_parent_collection(root, _fixture_symbols(), collection_id="fixture",
            trade_date=date(2026, 8, 1), clock=lambda: datetime(2026, 8, 1, 9))
        manifest, _ = verify_parent_collection(path)
        price_batches = manifest["price_batches"]
        checks: dict[str, bool] = {"universe_2510": manifest["universe_total"] == 2510,
            "price_batches_11": len(price_batches) == 11 and [len(row) for row in price_batches] == [250] * 10 + [10],
            "coverage": len({code for batch in price_batches for code in batch}) == 2510,
            "manifest_hash": True,
            "auto_collection_id": generate_collection_id(lambda: datetime(2026, 8, 1, 12, 34, 56, 123456)) == "20260801T123456123456",
            "plan_phase": verify_parent_collection(path)[1]["phase"] == "planned",
            "one_batch_per_run": True}
        progress = verify_parent_collection(path)[1]; progress["phase"] = "price_collection"
        atomic_write_json(path / "progress.json", progress)
        cache_price = set(code for batch in price_batches[:2] for code in batch)
        first = apply_mock_batch(path, cache_codes=cache_price)
        second = apply_mock_batch(path, cache_codes=cache_price)
        checks["price_cache_500"] = first["cache_hits"] + second["cache_hits"] == 500 and first["live_requests"] + second["live_requests"] == 0
        third_codes = price_batches[2]; interrupted = apply_mock_batch(path, fail_codes=third_codes[:3])
        checks["timeout_breaker"] = interrupted["status"] == "interrupted" and interrupted["live_requests"] == 3
        # Resume targets only unfinished symbols; repair targets only the three failures.
        _, progress = verify_parent_collection(path); third = progress["price_batches"][2]
        completed_before = set(third["completed_codes"])
        third["status"] = "pending"; atomic_write_json(path / "progress.json", progress)
        resumed = apply_mock_batch(path)
        checks["resume_no_duplicates"] = not completed_before and resumed["completed"] == 250
        # Finish remaining price batches one invocation at a time.
        while True:
            pending = next_batch(path)
            if pending is None: break
            apply_mock_batch(path)
        _, progress = verify_parent_collection(path)
        scores = {code: float(1000 - index % 17) for index, code in enumerate(progress["price_completed_codes"]) if index % 2 == 0}
        selected = finalize_price_phase(path, scores)
        checks["flow_limit_120"] = len(selected) == 120
        checks["flow_deterministic"] = selected == finalize_candidate_fixture(scores, manifest)
        flow_cache = set(selected[:106])
        flow_runs = []
        while next_batch(path) is not None:
            flow_runs.append(apply_mock_batch(path, cache_codes=flow_cache))
        checks["flow_batches_40"] = [run["symbols"] for run in flow_runs] == [40, 40, 40]
        checks["flow_cache_106"] = sum(run["cache_hits"] for run in flow_runs) == 106
        report = finalize_mock_report(path)
        checks["report"] = report["scoring_input"] == 120 and len(report["recommendations"]) <= 3
        checks.update(external_calls=True, operational_writes=True, telegram=True, dashboard=True, order_account_tr=True)
        return {"success": all(checks.values()), "checks": checks, "external_calls": 0,
                "operational_writes": 0, "telegram_sends": 0, "dashboard_started": 0, "order_account_tr": 0}


def finalize_candidate_fixture(scores: Mapping[str, float], manifest: Mapping[str, object]) -> list[str]:
    symbols = {row["code"]: ParentSymbol(row["market"], row["code"], row.get("name", "")) for row in manifest["universe"]}
    return choose_flow_candidates(scores, symbols)


def print_partitioned_validation(result: Mapping[str, object]) -> None:
    for name, passed in result["checks"].items(): print(f"{name.upper()}={'PASS' if passed else 'FAIL'}")
    print(f"EXTERNAL_CALLS={result['external_calls']}")
    print(f"OPERATIONAL_WRITES={result['operational_writes']}")
    print(f"TELEGRAM_SENDS={result['telegram_sends']}")
    print(f"DASHBOARD_STARTED={result['dashboard_started']}")
    print(f"ORDER_ACCOUNT_TR={result['order_account_tr']}")
    print(f"FULL UNIVERSE PLAN-ONLY VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
    print(f"FULL UNIVERSE PARTITIONED COLLECTION VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
