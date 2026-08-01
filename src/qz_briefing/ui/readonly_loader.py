"""Defensive read-only loading for the standalone dashboard."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


NO_SAVED_RESULT = "저장된 최근 브리핑 또는 추천 결과가 없습니다."


def _object(path: Path) -> dict[str, object] | None:
    if path.name.startswith(".") or path.suffix == ".tmp":
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class ReadOnlyDashboardLoader:
    """Read operational JSON/Markdown without touching validation data."""

    def __init__(self, briefing_root: Path, recommendation_root: Path) -> None:
        self.briefing_root = Path(briefing_root)
        self.recommendation_root = Path(recommendation_root)

    def latest_briefing(self, name: str, target: date) -> dict[str, object]:
        for path in sorted(self.briefing_root.glob(f"*/*/*/{name}.json"), reverse=True):
            try:
                saved = date.fromisoformat("-".join(path.parts[-4:-1]))
            except ValueError:
                continue
            if saved > target or "validation" in path.name:
                continue
            payload = _object(path)
            if payload is None:
                continue
            try:
                markdown = path.with_suffix(".md").read_text(encoding="utf-8")
            except OSError:
                markdown = ""
            return {"json": payload, "markdown": markdown, "error": None}
        return {"json": None, "markdown": "", "error": None}

    def latest_recommendation(self) -> dict[str, object]:
        reports = self.recommendation_root / "reports"
        for directory in sorted((path for path in reports.glob("*") if path.is_dir()), reverse=True):
            loaded = self._from_pointer(directory) or self._from_versions(directory)
            if loaded is not None:
                failure = _object(directory / "failures.json")
                loaded["recent_failure_at"] = failure.get("occurred_at") if failure else None
                return loaded
        return {"report": None, "markdown": "", "metadata": None, "warning": NO_SAVED_RESULT}

    def recommendation_for_date(self, target: date) -> dict[str, object]:
        """Load one explicit operational history date without changing any pointer."""
        directory = self.recommendation_root / "reports" / target.isoformat()
        loaded = self._from_pointer(directory) or self._from_versions(directory)
        if loaded is None:
            return {"report": None, "markdown": "", "metadata": None, "warning": NO_SAVED_RESULT,
                    "selected_report_date": target.isoformat(), "historical": True}
        loaded["selected_report_date"] = target.isoformat()
        loaded["historical"] = True
        return loaded

    def _from_pointer(self, directory: Path) -> dict[str, object] | None:
        pointer = _object(directory / "latest.json")
        if pointer is None:
            return None
        version = Path(str(pointer.get("version", "")))
        if not version.parts or version.is_absolute() or ".." in version.parts:
            return None
        return self._load_version(directory / version)

    def _from_versions(self, directory: Path) -> dict[str, object] | None:
        versions = directory / "versions"
        candidates = sorted(
            (path for path in versions.glob("*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime, reverse=True,
        )
        for candidate in candidates:
            loaded = self._load_version(candidate)
            if loaded is not None:
                return loaded
        return None

    @staticmethod
    def _load_version(version: Path) -> dict[str, object] | None:
        report = _object(version / "daily_recommendations.json")
        metadata = _object(version / "metadata.json")
        if report is None or metadata is None:
            return None
        try:
            markdown = (version / "daily_recommendations.md").read_text(encoding="utf-8")
        except OSError:
            markdown = ""
        return {"report": report, "metadata": metadata, "markdown": markdown, "warning": None}
