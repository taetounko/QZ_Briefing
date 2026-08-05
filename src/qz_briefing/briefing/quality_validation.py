# -*- coding: utf-8 -*-
"""Offline validation entry point for the pre-market quality/UI contract."""

from __future__ import annotations

from pathlib import Path


def main() -> int:
    quality_source = Path(__file__).with_name("quality.py").read_text(encoding="utf-8")
    ui_source = (Path(__file__).parents[1] / "ui" / "main_window.py").read_text(encoding="utf-8")
    quality_tokens = ("DATA_QUALITY", "BRIEFING_QUALITY", "time_consistent", "market_score_allowed")
    ui_tokens = ("코스피 주도주 TOP 10", "코스닥 주도주 TOP 10", "반등 후보", "보유종목 긴급 확인", "누락·오류·stale 자료", "판단 신뢰도")
    quality_ok = all(token in quality_source for token in quality_tokens)
    ui_ok = all(token in ui_source for token in ui_tokens)
    print(f"PRE-MARKET BRIEFING QUALITY VALIDATION: {'PASS' if quality_ok else 'FAIL'}")
    print(f"DASHBOARD FULL BRIEFING RENDER VALIDATION: {'PASS' if ui_ok else 'FAIL'}")
    return 0 if quality_ok and ui_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
