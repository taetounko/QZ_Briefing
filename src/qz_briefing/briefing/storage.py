"""Path management and atomic UTF-8 briefing result storage."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

from .models import BriefingType


class BriefingStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def result_paths(
        self, trading_date: date, briefing_type: BriefingType
    ) -> tuple[Path, Path]:
        directory = (
            self.root
            / f"{trading_date.year:04d}"
            / f"{trading_date.month:02d}"
            / f"{trading_date.day:02d}"
        )
        return (
            directory / f"{briefing_type.value}.json",
            directory / f"{briefing_type.value}.md",
        )

    def load_json(
        self, trading_date: date, briefing_type: BriefingType
    ) -> dict[str, object] | None:
        json_path, _ = self.result_paths(trading_date, briefing_type)
        if not json_path.exists():
            return None
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Briefing JSON root must be an object: {json_path}")
        return loaded

    def result_files_exist(
        self, trading_date: date, briefing_type: BriefingType
    ) -> tuple[bool, bool]:
        """Return JSON and Markdown existence without exposing path internals."""
        json_path, markdown_path = self.result_paths(trading_date, briefing_type)
        return json_path.is_file(), markdown_path.is_file()

    def save(
        self,
        trading_date: date,
        briefing_type: BriefingType,
        result: dict[str, object],
        markdown: str,
    ) -> tuple[Path, Path]:
        json_path, markdown_path = self.result_paths(trading_date, briefing_type)
        json_text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_json: Path | None = None
        temporary_markdown: Path | None = None
        backups: dict[Path, Path] = {}
        installed: set[Path] = set()
        cleanup_backups = False
        try:
            # Finish and sync both files before changing either final path.
            temporary_json = self._write_temporary(json_path, json_text)
            temporary_markdown = self._write_temporary(markdown_path, markdown)

            for final_path in (json_path, markdown_path):
                if final_path.exists():
                    backup_path = self._reserve_backup_path(final_path)
                    os.replace(final_path, backup_path)
                    backups[final_path] = backup_path

            os.replace(temporary_json, json_path)
            temporary_json = None
            installed.add(json_path)
            os.replace(temporary_markdown, markdown_path)
            temporary_markdown = None
            installed.add(markdown_path)
            cleanup_backups = True
        except Exception:
            for installed_path in installed:
                installed_path.unlink(missing_ok=True)
            for final_path, backup_path in backups.items():
                os.replace(backup_path, final_path)
            cleanup_backups = True
            raise
        finally:
            for temporary_path in (temporary_json, temporary_markdown):
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            if cleanup_backups:
                for backup_path in backups.values():
                    backup_path.unlink(missing_ok=True)
        return json_path, markdown_path

    @staticmethod
    def _write_temporary(path: Path, content: str) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            try:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            except Exception:
                stream.close()
                temporary_path.unlink(missing_ok=True)
                raise
        return temporary_path

    @staticmethod
    def _reserve_backup_path(path: Path) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".bak",
        )
        os.close(descriptor)
        backup_path = Path(raw_path)
        backup_path.unlink()
        return backup_path
