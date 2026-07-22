# -*- coding: utf-8 -*-
from __future__ import annotations
import hashlib,json,logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date,datetime,timedelta
from pathlib import Path
from qz_briefing.runtime.unattended import atomic_write_json
from .formatter import split_messages
from .models import NotificationRequest,NotificationStatus
from .queue import PersistentNotificationQueue

class NotificationService:
    def __init__(self,adapter,queue:PersistentNotificationQueue,history_path:Path,*,clock=datetime.now,send_markdown_file=True,send_json_file=False,send_runtime_alerts=True,send_daily_summary=True,executor=None,timer_factory=None):
        self.adapter,self.queue,self.history_path,self.clock=adapter,queue,Path(history_path),clock; self.send_markdown_file,self.send_json_file=send_markdown_file,send_json_file; self.send_runtime_alerts,self.send_daily_summary=send_runtime_alerts,send_daily_summary; self.executor=executor or ThreadPoolExecutor(max_workers=1,thread_name_prefix="qz-telegram"); self.status=NotificationStatus(configured=True,enabled=True); self.stopping=False; self.history=self._load_history(); self._inflight=set(); self.timer=timer_factory() if timer_factory else None
        if self.timer is not None: self.timer.timeout.connect(self.retry_due); self.timer.start(60_000)
        self.status.pending_count=len(self.queue.items)
        if self.queue.items:self.status.next_attempt_at=min(str(item["next_attempt_at"]) for item in self.queue.items)
    def _load_history(self):
        try: value=json.loads(self.history_path.read_text(encoding="utf-8")); return value if isinstance(value,list) else []
        except (OSError,ValueError): return []
    def submit(self,request:NotificationRequest)->bool:
        if self.stopping or request.event_type=="market_close_validation": return False
        key=self._key(request)
        if key in self._inflight or any(x.get("key")==key for x in self.history) or any(self._item_key(x)==key for x in self.queue.items): return False
        item=self.queue.add(request); self._inflight.add(key); self.status.pending_count=len(self.queue.items); self.executor.submit(self._deliver,item,key); return True
    def _deliver(self,item,key):
        try:
            text=str(item["payload"]["text"])
            for chunk in split_messages(text):
                try: self.adapter.send_text(chunk,parse_mode="MarkdownV2")
                except Exception: self.adapter.send_text(chunk,parse_mode=None)
            markdown=item["payload"].get("markdown_path")
            if self.send_markdown_file and markdown and Path(markdown).is_file(): self.adapter.send_document(Path(markdown),"QZ Briefing")
            json_path=item["payload"].get("json_path")
            if self.send_json_file and json_path and Path(json_path).is_file(): self.adapter.send_document(Path(json_path),"QZ JSON")
            self.queue.remove(item); self.history.append({"key":key,"delivered_at":self.clock().isoformat(),"event_type":item["event_type"]}); self._trim_history(); self.status.last_success_at=self.clock().isoformat(); self.status.last_event=item["event_type"]; self.status.last_error=None
        except Exception as exc: self.queue.fail(item,exc); self.status.last_error=f"{type(exc).__name__}: delivery failed"; self.status.next_attempt_at=item["next_attempt_at"]
        finally: self._inflight.discard(key); self.status.pending_count=len(self.queue.items)
    def retry_due(self):
        if self.stopping:return
        for item in self.queue.due():
            age=self.clock()-datetime.fromisoformat(str(item["created_at"]))
            if age>timedelta(days=1) and item["event_type"] not in {"pre_market","intraday_10am","market_close"}:
                self.queue.remove(item); continue
            if age>timedelta(days=1) and not str(item["payload"]["text"]).startswith("[지연 전달]"):
                item["payload"]["text"]="[지연 전달]\n"+str(item["payload"]["text"]); self.queue.save()
            key=self._item_key(item)
            if key not in self._inflight:self._inflight.add(key);self.executor.submit(self._deliver,item,key)
    def stop(self):
        self.stopping=True
        if self.timer is not None:self.timer.stop()
        self.executor.shutdown(wait=False,cancel_futures=True)
    def _key(self,r): return f"telegram|{r.trading_date}|{r.event_type}|{hashlib.sha256(r.text.encode()).hexdigest()}" if not r.unique_nonce else f"test|{r.unique_nonce}"
    def _item_key(self,item): return f"telegram|{item['trading_date']}|{item['event_type']}|{item['content_hash']}" if not str(item["id"]).startswith("test-") else f"test|{item['id']}"
    def _trim_history(self):
        cutoff=self.clock()-timedelta(days=30); self.history=[x for x in self.history if datetime.fromisoformat(x["delivered_at"])>=cutoff]; atomic_write_json(self.history_path,self.history)

class DisabledNotificationService:
    status=NotificationStatus(configured=False,enabled=False)
    send_runtime_alerts=False; send_daily_summary=False
    def submit(self,request): return False
    def retry_due(self): return None
    def stop(self): return None
