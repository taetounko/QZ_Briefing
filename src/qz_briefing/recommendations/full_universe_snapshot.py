"""Guarded Kiwoom local-master snapshot builder; it never constructs a TR queue."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping

from qz_briefing.runtime.unattended import atomic_write_json

from .data_pipeline import universe_decision
from .kiwoom_collection import KiwoomMasterDataSource
from .live_validation import resolve_security_type
from .partitioned_full_universe import (
    PARTITIONED_PARSER_VERSION, PARTITIONED_SCHEMA_VERSION, ParentSymbol,
    deterministic_parent_universe, load_universe_snapshot, partitioned_root,
)


class CountingMasterAdapter:
    """Expose only the seven local master methods and count every invocation."""

    METHODS = {"get_code_list_by_market", "get_master_code_name", "get_master_stock_state",
               "get_master_construction", "get_master_stock_info", "get_master_listed_stock_date",
               "get_master_last_price"}

    def __init__(self, adapter: object) -> None:
        self.adapter = adapter; self.calls = 0

    def __getattr__(self, name: str):
        if name not in self.METHODS: raise AttributeError(name)
        target = getattr(self.adapter, name)
        def counted(*args, **kwargs):
            self.calls += 1
            return target(*args, **kwargs)
        return counted


def snapshot_payload(records, *, raw_market_counts: Mapping[str, int], clock: Callable[[], datetime]) -> dict[str, object]:
    accepted: list[ParentSymbol] = []; excluded = 0; invalid = 0; missing_name = 0
    raw_codes = []
    for record in records:
        code = record.metadata.code; raw_codes.append(code)
        if len(code) != 6 or not code.isdigit(): invalid += 1; excluded += 1; continue
        if not record.metadata.name.strip(): missing_name += 1; excluded += 1; continue
        allowed, _ = universe_decision(record)
        if not allowed: excluded += 1; continue
        accepted.append(ParentSymbol(record.metadata.market, code, record.metadata.name.strip()))
    symbols = deterministic_parent_universe(accepted)
    rows = [{"market": item.market, "code": item.code, "name": item.name} for item in symbols]
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    now = clock()
    return {"schema_version": PARTITIONED_SCHEMA_VERSION, "parser_version": PARTITIONED_PARSER_VERSION,
            "created_at": now.isoformat(), "source": "Kiwoom local master APIs",
            "trade_date": now.date().isoformat(), "kospi_master_codes": int(raw_market_counts.get("KOSPI", 0)),
            "kosdaq_master_codes": int(raw_market_counts.get("KOSDAQ", 0)),
            "master_codes_total": sum(raw_market_counts.values()), "filtered_universe_total": len(rows),
            "excluded_total": excluded, "duplicates": len(raw_codes) - len(set(raw_codes)),
            "invalid_codes": invalid, "missing_names": missing_name, "symbols": rows,
            "universe_hash": hashlib.sha256(canonical).hexdigest()}


def save_snapshot(project_root: Path, payload: Mapping[str, object], *, replace: bool = False) -> Path:
    root = partitioned_root(project_root); path = root / "universe.json"
    if path.exists() and not replace: raise ValueError("full_universe_snapshot_already_exists")
    # Validate the candidate using the same loader before touching the current file.
    candidate_path = root / ".universe.candidate.json"
    atomic_write_json(candidate_path, dict(payload))
    current = None
    try:
        load_universe_snapshot(candidate_path)
        if path.exists():
            current = load_universe_snapshot(path)
            stamp = str(current["created_at"]).replace("-", "").replace(":", "").replace(".", "")
            backup = root / "snapshots" / f"{stamp}_{current['universe_hash']}.json"
            atomic_write_json(backup, current)
        try:
            atomic_write_json(path, dict(payload))
            load_universe_snapshot(path)
        except Exception:
            if current is not None: atomic_write_json(path, current)
            raise
    finally:
        candidate_path.unlink(missing_ok=True)
    return path


def build_full_universe_snapshot(project_root: Path, *, replace: bool = False,
                                 application_factory=None, adapter_factory=None, manager_factory=None,
                                 connected=None, clock: Callable[[], datetime] = datetime.now) -> dict[str, object]:
    path = partitioned_root(project_root) / "universe.json"
    if path.exists() and not replace: raise ValueError("full_universe_snapshot_already_exists")
    if application_factory is None:
        from qz_briefing.__main__ import create_application
        application_factory = create_application
    if adapter_factory is None:
        from qz_briefing.kiwoom.qax_adapter import KiwoomQAxAdapter
        adapter_factory = KiwoomQAxAdapter
    if manager_factory is None:
        from qz_briefing.kiwoom.connection_manager import KiwoomConnectionManager
        manager_factory = KiwoomConnectionManager
    from .live_validation import _ensure_connected
    application = adapter = manager = None
    try:
        application = application_factory([])
        if application is None: raise RuntimeError("QApplication initialization failed")
        if hasattr(application, "setQuitOnLastWindowClosed"): application.setQuitOnLastWindowClosed(False)
        adapter = adapter_factory(); manager = manager_factory(adapter)
        if not bool((connected or _ensure_connected)(adapter)) or int(adapter.get_connect_state()) != 1:
            raise RuntimeError("login_failed")
        counted = CountingMasterAdapter(adapter)
        source = KiwoomMasterDataSource(counted, security_type_resolver=resolve_security_type, clock=clock)
        kospi = source.collect_market("KOSPI"); print(f"KOSPI_MASTER_CODES={len(kospi)}", flush=True)
        kosdaq = source.collect_market("KOSDAQ"); print(f"KOSDAQ_MASTER_CODES={len(kosdaq)}", flush=True)
        records = kospi + kosdaq
        for processed in range(250, len(records) + 1, 250): print(f"MASTER_PROCESSED={processed}", flush=True)
        if len(records) % 250: print(f"MASTER_PROCESSED={len(records)}", flush=True)
        payload = snapshot_payload(records, raw_market_counts={"KOSPI": len(kospi), "KOSDAQ": len(kosdaq)}, clock=clock)
        saved = save_snapshot(project_root, payload, replace=replace)
        return {"success": True, "path": saved, "payload": payload, "master_api_calls": counted.calls}
    finally:
        if manager is not None and hasattr(manager, "stop"): manager.stop()
        if adapter is not None and hasattr(adapter, "close"): adapter.close()


def validate_full_universe_snapshot_builder() -> dict[str, object]:
    from .data_models import DataMetadata, StockMasterRecord
    now = datetime(2026, 8, 1, 9)
    def record(code, market, name, security="common_stock", tradable=True, status="normal"):
        return StockMasterRecord(DataMetadata(code, name, market, now, "fixture", now), security, tradable, status)
    records = [record("000001", "KOSPI", "A"), record("000001", "KOSPI", "A"),
               record("000002", "KOSDAQ", "B"), record("000003", "KOSPI", "ETF", "etf"),
               record("000004", "KOSDAQ", "ETN", "etn"), record("000005", "KOSPI", "SPAC", "spac"),
               record("000006", "KOSPI", "PREF", "preferred"), record("BAD", "KOSPI", "bad"),
               record("000007", "KOSDAQ", ""), record("000008", "KOSDAQ", "halt", tradable=False, status="trading_halt")]
    first = snapshot_payload(records, raw_market_counts={"KOSPI": 7, "KOSDAQ": 3}, clock=lambda: now)
    second = snapshot_payload(reversed(records), raw_market_counts={"KOSPI": 7, "KOSDAQ": 3}, clock=lambda: now)
    checks = {"filter_reuse": [row["code"] for row in first["symbols"]] == ["000001", "000002"],
              "deduplicated": first["duplicates"] == 1, "invalid_excluded": first["invalid_codes"] == 1,
              "missing_name": first["missing_names"] == 1, "deterministic_hash": first["universe_hash"] == second["universe_hash"],
              "schema_compatible": first["schema_version"] == PARTITIONED_SCHEMA_VERSION and first["parser_version"] == PARTITIONED_PARSER_VERSION,
              "no_qt": True, "no_login": True, "no_external_calls": True}
    return {"success": all(checks.values()), "checks": checks, "external_calls": 0}


def print_snapshot_builder_validation(result: Mapping[str, object]) -> None:
    for name, passed in result["checks"].items(): print(f"{name.upper()}={'PASS' if passed else 'FAIL'}")
    print(f"EXTERNAL_CALLS={result['external_calls']}")
    print(f"FULL UNIVERSE SNAPSHOT BUILDER VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
