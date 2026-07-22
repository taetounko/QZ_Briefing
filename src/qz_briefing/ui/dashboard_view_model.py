# -*- coding: utf-8 -*-
"""Defensive, read-only projection of saved briefing JSON/Markdown."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from qz_briefing.briefing.rules import derivatives_values, index_rates, spot_flows, stock_rates
from .formatters import mask_account


RESULT_NAMES = {
    "pre_market": ("pre_market.json", "pre_market.md", "09:00"),
    "intraday_10am": ("intraday_10am.json", "intraday_10am.md", "10:00"),
    "market_close": ("market_close.json", "market_close.md", "15:40"),
    "market_close_validation": ("market_close_validation.json", "market_close_validation.md", "수동 실행"),
}


class DashboardViewModel:
    def __init__(self, root: Path, *, clock=datetime.now) -> None:
        self.root, self._clock = Path(root), clock

    def load_today(self, target_date: date | None = None) -> dict[str, object]:
        target = target_date or self._clock().date()
        directory = self.root / f"{target.year:04d}" / f"{target.month:02d}" / f"{target.day:02d}"
        results = {name: self._load_pair(directory, *paths[:2], next_time=paths[2]) for name, paths in RESULT_NAMES.items()}
        valid = [value for value in results.values() if isinstance(value.get("json"), dict)]
        latest = max(valid, key=lambda value: self._completed_key(value["json"]), default=None)
        runtime = self._load_runtime()
        return {
            "date": target.isoformat(), "results": results,
            "latest": latest.get("json") if latest else {},
            "summary": self.summary(latest.get("json") if latest else {}),
            "holdings": self.holdings(latest.get("json") if latest else {}),
            "leadership": self.leadership(latest.get("json") if latest else {}),
            "watchlist": self.watchlist(latest.get("json") if latest else {}),
            "messages": self.messages(results),
            "runtime": runtime,
        }

    def _load_runtime(self) -> dict[str, object]:
        path = self.root.parent / "runtime" / "heartbeat.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load_pair(self, directory: Path, json_name: str, markdown_name: str, *, next_time: str) -> dict[str, object]:
        json_path, markdown_path = directory / json_name, directory / markdown_name
        error = None; payload: dict[str, object] | None = None
        try:
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict): raise ValueError("JSON root is not an object")
            payload = loaded
        except FileNotFoundError:
            pass
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = f"{json_name}: {type(exc).__name__}: {exc}"
        try: markdown = markdown_path.read_text(encoding="utf-8")
        except OSError: markdown = ""
        return {"json": payload, "markdown": markdown, "error": error, "next_time": next_time}

    @staticmethod
    def _completed_key(payload: dict[str, object]) -> str:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return str(payload.get("completed_at") or metadata.get("generated_at") or "")

    @staticmethod
    def summary(result: dict[str, object]) -> dict[str, object]:
        analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
        decision = analysis.get("decision") if isinstance(analysis.get("decision"), dict) else {}
        close = result.get("market_close_analysis") if isinstance(result.get("market_close_analysis"), dict) else {}
        indices, stocks, flows, derivatives = index_rates(result), stock_rates(result), spot_flows(result), derivatives_values(result)
        return {
            "conclusion": decision.get("headline") or close.get("market_conclusion") or analysis.get("summary") or "아직 완료된 브리핑이 없습니다",
            "decision_confidence": decision.get("confidence"),
            "market_risk": decision.get("risk_level"),
            "action_guidance": decision.get("action_guidance"),
            "confirmation_conditions": decision.get("confirmation_conditions", []),
            "invalidation_conditions": decision.get("invalidation_conditions", []),
            "KOSPI": indices.get("KOSPI"), "KOSDAQ": indices.get("KOSDAQ"), "KOSPI200": indices.get("KOSPI200"),
            "삼성전자": stocks.get("005930"), "SK하이닉스": stocks.get("000660"),
            "외국인": flows.get("foreigner"), "기관": flows.get("institution"),
            "프로그램": derivatives.get("program_total"),
            "risk": close.get("risk_summary") or "저장된 위험 요약 없음",
            "guidance": close.get("next_session_summary") or "확정적 매매 지시가 아닌 관찰용 정보입니다.",
        }

    @staticmethod
    def holdings(result: dict[str, object]) -> dict[str, object]:
        data = result.get("holdings_analysis") if isinstance(result.get("holdings_analysis"), dict) else {}
        accounts = data.get("accounts") if isinstance(data.get("accounts"), list) else []
        portfolio = data.get("portfolio") if isinstance(data.get("portfolio"), dict) else {}
        rows = []
        for item in data.get("holdings", []):
            if not isinstance(item, dict): continue
            account_ids = item.get("account_ids") if isinstance(item.get("account_ids"), list) else []
            projected = {key: value for key, value in item.items() if key != "account_ids"}
            rows.append({**projected, "account": ", ".join(mask_account(value) for value in account_ids) or "-"})
        rows.sort(key=lambda item: (item.get("priority", 8), str(item.get("code", ""))))
        return {"account_count": len(accounts), "holding_count": len(rows), "source": data.get("source", "-"), "portfolio": portfolio, "rows": rows}

    @staticmethod
    def leadership(result: dict[str, object]) -> list[dict[str, object]]:
        data = result.get("leadership") if isinstance(result.get("leadership"), dict) else {}
        rows = []
        for key, market in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ"), ("rebound_candidates", "반등 후보")):
            for item in data.get(key, []):
                if isinstance(item, dict): rows.append({**item, "market": market})
        return rows

    @staticmethod
    def watchlist(result: dict[str, object]) -> list[dict[str, object]]:
        rows = result.get("next_session_watchlist")
        return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []

    @staticmethod
    def messages(results: dict[str, object]) -> list[str]:
        output = []
        for name, wrapper in results.items():
            if wrapper.get("error"): output.append(str(wrapper["error"]))
            payload = wrapper.get("json")
            if not isinstance(payload, dict): continue
            output.extend(f"{name} warning: {item}" for item in payload.get("warnings", []) if item)
            output.extend(f"{name} error: {item}" for item in payload.get("errors", []) if item)
        return output
