"""Idempotent daily recommendation generation and atomic report persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable

from .data_models import RecommendationDataBundle
from .renderer import render_recommendations
from .selector import select_integrated_recommendations


REPORT_SCHEMA_VERSION = 1
SCORING_POLICY_VERSION = "integrated-v1"
RISK_POLICY_VERSION = "risk-v1"
UNIVERSE_VERSION = "common-stock-v1"


def _json_default(value):
    if isinstance(value, (date, datetime)): return value.isoformat()
    raise TypeError(type(value).__name__)


def recommendation_input_hash(trading_date: date, as_of: datetime, bundles: list[RecommendationDataBundle]) -> str:
    payload = {
        "trading_date": trading_date.isoformat(), "as_of": as_of.isoformat(),
        "scoring_policy": SCORING_POLICY_VERSION, "risk_policy": RISK_POLICY_VERSION,
        "universe": UNIVERSE_VERSION,
        "bundles": [asdict(bundle) for bundle in sorted(bundles, key=lambda x: x.master.metadata.code)],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def report_to_dict(report, *, trading_date: date, content_hash: str, generated_at: datetime, market_status: str) -> dict[str, object]:
    def row(value):
        score=value.score; signal=score.weekly; preliminary=score.preliminary
        return {
            "rank":value.rank,"grade":value.grade,"code":score.item.code,"name":score.item.name,
            "market":score.item.market,"total_score":score.total_score,"confidence":score.confidence,
            "components":score.components,"risk_penalty":score.risk_deduction,"reasons":score.reasons[:4],
            "risks":score.risks,"missing":score.missing,"chase_buying_prohibited":score.risk_deduction>=10,
            "preferred_entry":score.features.preferred_entry,"invalidation_conditions":score.features.invalidation_conditions,
            "weekly_close":signal.weekly_close if signal else None,"weekly_ma5":signal.weekly_ma5 if signal else None,
            "weekly_distance_rate":signal.distance_rate if signal else None,
            "evaluation_status":preliminary.evaluation_status if preliminary else "not_evaluated",
        }
    return {
        "schema_version":REPORT_SCHEMA_VERSION,"content_hash":content_hash,"trading_date":trading_date.isoformat(),
        "evaluated_at":report.as_of.isoformat(),"generated_at":generated_at.isoformat(),"data_as_of":report.as_of.isoformat(),
        "market_status":market_status,"input_count":report.input_count,"hard_filter_pass_count":report.hard_filter_pass_count,
        "evaluable_count":report.hard_filter_pass_count,"strong_count":len(report.strong),"review_count":len(report.review),
        "partial_count":sum(r.score.preliminary.evaluation_status!="complete" for r in report.strong+report.review if r.score.preliminary),
        "failure_count":0,"scoring_policy_version":SCORING_POLICY_VERSION,"risk_policy_version":RISK_POLICY_VERSION,
        "universe_version":UNIVERSE_VERSION,"strong":[row(x) for x in report.strong],"review":[row(x) for x in report.review],
        "excluded":report.excluded,"warnings":report.warnings,
    }


class RecommendationReportStore:
    def __init__(self, root: Path): self.root=Path(root)
    def directory(self, trading_date: date) -> Path: return self.root/"reports"/trading_date.isoformat()
    def _atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True,exist_ok=True)
        fd, raw=tempfile.mkstemp(dir=path.parent,prefix=f".{path.name}.",suffix=".tmp")
        temporary=Path(raw)
        try:
            with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,path)
        except Exception:
            temporary.unlink(missing_ok=True); raise
    def save(self, trading_date: date, content_hash: str, payload: dict[str,object], markdown: str, *, update_latest: bool = True) -> tuple[Path,Path,Path]:
        directory=self.directory(trading_date); version=directory/"versions"/content_hash
        json_path=version/"daily_recommendations.json"; md_path=version/"daily_recommendations.md"
        metadata_path=version/"metadata.json"
        if not all(path.exists() for path in (json_path,md_path,metadata_path)):
            text=json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+"\n"
            self._atomic(json_path,text); self._atomic(md_path,markdown); self._atomic(metadata_path,text)
        if update_latest:
            self._atomic(directory/"latest.json",json.dumps({"content_hash":content_hash,"version":str(version.relative_to(directory))},ensure_ascii=False)+"\n")
        return json_path,md_path,metadata_path
    def load(self, trading_date: date) -> dict[str,object]|None:
        try:
            pointer=json.loads((self.directory(trading_date)/"latest.json").read_text(encoding="utf-8"))
            path=self.directory(trading_date)/str(pointer["version"])/"daily_recommendations.json"
            value=json.loads(path.read_text(encoding="utf-8")); return value if isinstance(value,dict) else None
        except (OSError,ValueError,KeyError): return None
    def load_latest_before(self, trading_date: date) -> dict[str,object]|None:
        for path in sorted(self.root.glob("reports/*/latest.json"),reverse=True):
            try: saved=date.fromisoformat(path.parent.name)
            except ValueError: continue
            if saved<trading_date:
                value=self.load(saved)
                if value is not None:return value
        return None
    def save_failure(self,trading_date:date,error:str,now:datetime)->None:
        self._atomic(self.directory(trading_date)/"failures.json",json.dumps({"occurred_at":now.isoformat(),"error":error},ensure_ascii=False)+"\n")


@dataclass(frozen=True)
class DailyGenerationResult:
    status: str
    report: dict[str,object]|None=None
    content_hash: str=""
    paths: tuple[str,...]=()
    telegram_registration_count: int=0


class DailyRecommendationService:
    def __init__(self,store:RecommendationReportStore,bundle_loader:Callable[[date],list[RecommendationDataBundle]],*,clock:Callable[[],datetime]=datetime.now,market_is_open:Callable[[date],bool]=lambda _:True):
        self.store=store; self.bundle_loader=bundle_loader; self.clock=clock; self.market_is_open=market_is_open
    def generate_market_close(self,trading_date:date)->DailyGenerationResult:
        now=self.clock()
        if not self.market_is_open(trading_date): return DailyGenerationResult("market_closed")
        if now.date()!=trading_date or now.time()<time(15,40): return DailyGenerationResult("not_due")
        try:
            bundles=self.bundle_loader(trading_date)
            if not bundles:return DailyGenerationResult("input_unavailable")
            data_as_of=max(bundle.master.metadata.as_of for bundle in bundles)
            digest=recommendation_input_hash(trading_date,data_as_of,bundles)
            existing=self.store.load(trading_date)
            if existing and existing.get("content_hash")==digest:return DailyGenerationResult("reused",existing,digest)
            report=select_integrated_recommendations(bundles); payload=report_to_dict(report,trading_date=trading_date,content_hash=digest,generated_at=now,market_status="open")
            paths=self.store.save(trading_date,digest,payload,render_recommendations(report))
            return DailyGenerationResult("generated",payload,digest,tuple(str(x) for x in paths),1)
        except Exception as exc:
            try:self.store.save_failure(trading_date,f"{type(exc).__name__}: recommendation generation failed",now)
            except Exception:pass
            return DailyGenerationResult("failed")
    def load_pre_market(self,trading_date:date)->dict[str,object]|None:return self.store.load_latest_before(trading_date)
    def load_intraday(self,trading_date:date)->dict[str,object]|None:return self.store.load(trading_date) or self.store.load_latest_before(trading_date)
    def briefing_result(self,kind:str,trading_date:date)->dict[str,object]|None:
        if kind=="market_close": return self.generate_market_close(trading_date).report
        if kind=="pre_market": return self.load_pre_market(trading_date)
        return self.load_intraday(trading_date)
