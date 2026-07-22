# -*- coding: utf-8 -*-
from __future__ import annotations
import hashlib, json, uuid
from datetime import datetime, timedelta
from pathlib import Path
from qz_briefing.runtime.unattended import atomic_write_json
from .models import NotificationRequest

BACKOFF_MINUTES=(1,5,15,30,30,30,30,30)

class PersistentNotificationQueue:
    def __init__(self,path:Path,*,clock=datetime.now): self.path=Path(path); self.clock=clock; self.items=self._load()
    def _load(self):
        try: value=json.loads(self.path.read_text(encoding="utf-8")); return value if isinstance(value,list) else []
        except (OSError,ValueError): return []
    def add(self,request:NotificationRequest)->dict[str,object]:
        digest=hashlib.sha256(request.text.encode("utf-8")).hexdigest(); now=self.clock()
        item={"id":request.unique_nonce or uuid.uuid4().hex,"channel":"telegram","event_type":request.event_type,"trading_date":request.trading_date,"created_at":now.isoformat(),"attempt_count":0,"next_attempt_at":now.isoformat(),"content_hash":digest,"payload":{"text":request.text,"markdown_path":request.markdown_path,"json_path":request.json_path},"last_error":""}
        self.items.append(item); self.save(); return item
    def due(self):
        now=self.clock(); return [x for x in self.items if datetime.fromisoformat(x["next_attempt_at"])<=now and int(x["attempt_count"])<8]
    def fail(self,item,error):
        item["attempt_count"]=int(item["attempt_count"])+1; index=min(item["attempt_count"]-1,len(BACKOFF_MINUTES)-1); item["next_attempt_at"]=(self.clock()+timedelta(minutes=BACKOFF_MINUTES[index])).isoformat(); item["last_error"]=f"{type(error).__name__}: delivery failed"; self.save()
    def remove(self,item): self.items.remove(item); self.save()
    def save(self): atomic_write_json(self.path,{"items":self.items} if False else self.items)
