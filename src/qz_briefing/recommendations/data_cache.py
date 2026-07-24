"""Versioned atomic JSON cache for offline recommendation inputs."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CacheRead:
    data: object | None
    fresh: bool
    stale: bool
    warning: str | None = None


class RecommendationDataCache:
    KINDS = ("master", "daily", "weekly", "flow", "features", "snapshots", "failures")

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, kind: str, key: str) -> Path:
        if kind not in self.KINDS: raise ValueError(f"unsupported cache kind: {kind}")
        return self.root / kind / f"{key}.json"

    def save(self, kind: str, key: str, data: object, *, as_of: datetime, source: str) -> Path:
        path=self.path(kind,key); path.parent.mkdir(parents=True,exist_ok=True)
        payload={"schema_version":SCHEMA_VERSION,"as_of":as_of.isoformat(),"updated_at":datetime.now().isoformat(),"source":source,"data":data}
        descriptor,raw=tempfile.mkstemp(dir=path.parent,prefix=f".{path.name}.",suffix=".tmp")
        temporary=Path(raw)
        try:
            with os.fdopen(descriptor,"w",encoding="utf-8",newline="\n") as stream:
                json.dump(payload,stream,ensure_ascii=False,sort_keys=True); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,path)
        except Exception:
            temporary.unlink(missing_ok=True); raise
        return path

    def load(self, kind: str, key: str, *, now: datetime, max_age: timedelta) -> CacheRead:
        path=self.path(kind,key)
        if not path.exists(): return CacheRead(None,False,False,"cache miss")
        try:
            payload=json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload,dict) or payload.get("schema_version")!=SCHEMA_VERSION: raise ValueError("cache schema mismatch")
            updated=datetime.fromisoformat(str(payload["updated_at"])); stale=now-updated>max_age
            return CacheRead(payload.get("data"),not stale,stale,"stale cache" if stale else None)
        except (OSError,ValueError,KeyError,json.JSONDecodeError) as exc:
            quarantine=path.with_name(f"{path.name}.corrupt-{now.strftime('%Y%m%d%H%M%S')}")
            try: os.replace(path,quarantine)
            except OSError: pass
            return CacheRead(None,False,False,f"corrupt cache ignored: {type(exc).__name__}")
