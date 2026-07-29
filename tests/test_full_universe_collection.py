from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import pytest

from qz_briefing.__main__ import run
from qz_briefing.recommendations.data_models import DataMetadata, StockMasterRecord
from qz_briefing.recommendations.full_universe_collection import (
    FullCollectionSession, deterministic_universe, protected_validation_root,
    select_flow_targets, should_abort_for_failures, validate_scope,
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
    assert "live collection is intentionally unavailable" in capsys.readouterr().out
