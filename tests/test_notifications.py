# -*- coding: utf-8 -*-
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from qz_briefing.__main__ import handle_notification_cli, mask_chat_id, parse_cli_arguments
from qz_briefing.notifications import NotificationRequest, NotificationService, PersistentNotificationQueue
from qz_briefing.notifications.formatter import escape_markdown, format_briefing, format_daily_summary, format_runtime_alert, split_messages


class ImmediateExecutor:
    def submit(self, callback, *args): callback(*args)
    def shutdown(self, **kwargs): pass


class Adapter:
    def __init__(self, fail_markdown=False, fail_all=False): self.texts=[]; self.files=[]; self.fail_markdown=fail_markdown; self.fail_all=fail_all
    def send_text(self,text,parse_mode="MarkdownV2"):
        if self.fail_all or (self.fail_markdown and parse_mode): raise TimeoutError("secret detail")
        self.texts.append((text,parse_mode))
    def send_document(self,path,caption=""): self.files.append(Path(path).name)


def result(kind="pre_market", status="completed"):
    return {"briefing_type":kind,"trading_date":"2026-07-22","completed_at":"2026-07-22T09:00:00","status":status,"analysis":{"summary":"상승 시도","decision":{"headline":"상승을 시도하고 있습니다.","confidence":72,"risk_level":"medium","confirmation_conditions":["거래대금 증가"],"invalidation_conditions":["지수 반전"]}},"holdings_analysis":{"holdings":[{"priority":i,"code":f"00000{i}","name":f"종목{i}","decision":{"action_level":"observe_only"}} for i in range(1,8)]}}


def test_briefing_formats_all_sessions_and_caps_urgent_holdings():
    for kind,title in (("pre_market","장전"),("intraday_10am","오전 10시"),("market_close","장마감")):
        text=format_briefing(result(kind)); assert title in text; assert text.count("(00000") == 5
    assert "실제 외국인·기관 수급이 아닙니다" in format_briefing(result("pre_market"))
    assert "장전 예상과 실제 개장 후 수급의 차이" in format_briefing(result("intraday_10am"))


def test_no_market_runtime_alert_and_daily_summary_are_safe():
    assert "장이 개시되지 않아" in format_briefing(result(status="no_market_open"))
    assert "QZ 운영 경고" in format_runtime_alert("연결 실패", "10:14")
    summary=format_daily_summary({"overall_result":"successful","briefings":{},"connection_drop_count":0})
    assert "일일 운영 결과" in summary and "account" not in summary


def test_account_is_masked_and_markdown_escaped_and_split_on_lines():
    data=result(); data["analysis"]["decision"]["headline"]="계좌 1234567890 [주의]"
    text=format_briefing(data); assert "1234567890" not in text and "******7890" in text
    assert "\\[" in escape_markdown("[주의]")
    chunks=split_messages("첫 문장\n"+("세부 항목 "*100),limit=80); assert len(chunks)>1 and chunks[0].startswith("[1/")


def test_service_sends_after_enqueue_attaches_markdown_not_json_and_deduplicates(tmp_path):
    md=tmp_path/"x.md"; md.write_text("detail"); js=tmp_path/"x.json"; js.write_text("{}")
    adapter=Adapter(); queue=PersistentNotificationQueue(tmp_path/"queue.json")
    service=NotificationService(adapter,queue,tmp_path/"history.json",executor=ImmediateExecutor())
    request=NotificationRequest("pre_market","2026-07-22","hello",str(md),str(js))
    assert service.submit(request); assert not service.submit(request)
    assert adapter.files == ["x.md"] and not queue.items
    assert "x.json" not in adapter.files


def test_markdown_failure_retries_plain_text(tmp_path):
    adapter=Adapter(fail_markdown=True); service=NotificationService(adapter,PersistentNotificationQueue(tmp_path/"q.json"),tmp_path/"h.json",executor=ImmediateExecutor())
    assert service.submit(NotificationRequest("pre_market","2026-07-22","[hello]"))
    assert adapter.texts == [("[hello]",None)]


def test_failure_persists_without_token_and_uses_backoff(tmp_path):
    clock=[datetime(2026,7,22,9,0)]; queue=PersistentNotificationQueue(tmp_path/"q.json",clock=lambda:clock[0]); service=NotificationService(Adapter(fail_all=True),queue,tmp_path/"h.json",clock=lambda:clock[0],executor=ImmediateExecutor())
    service.submit(NotificationRequest("pre_market","2026-07-22","safe payload"))
    assert len(queue.items)==1 and queue.items[0]["attempt_count"]==1
    assert datetime.fromisoformat(queue.items[0]["next_attempt_at"])-clock[0]==timedelta(minutes=1)
    saved=(tmp_path/"q.json").read_text(); assert "token" not in saved.lower() and "secret detail" not in saved


def test_shutdown_blocks_new_delivery(tmp_path):
    service=NotificationService(Adapter(),PersistentNotificationQueue(tmp_path/"q.json"),tmp_path/"h.json",executor=ImmediateExecutor()); service.stop()
    assert not service.submit(NotificationRequest("pre_market","2026-07-22","x"))


def test_old_runtime_alert_is_discarded_before_retry(tmp_path):
    clock=[datetime(2026,7,22,9)]; queue=PersistentNotificationQueue(tmp_path/"q.json",clock=lambda:clock[0]); item=queue.add(NotificationRequest("briefing_failed","2026-07-22","old warning")); item["next_attempt_at"]=clock[0].isoformat(); item["created_at"]=(clock[0]-timedelta(days=2)).isoformat(); queue.save()
    service=NotificationService(Adapter(),queue,tmp_path/"h.json",clock=lambda:clock[0],executor=ImmediateExecutor()); service.retry_due()
    assert queue.items==[]


class SecretStore:
    value=None; removed=False
    def __init__(self,path): self.path=path
    def save(self,value): SecretStore.value=value
    def load(self): return SecretStore.value
    def remove(self): SecretStore.removed=True


def test_cli_configure_and_disable_never_put_token_in_json(tmp_path):
    made=[]
    class CliAdapter:
        def __init__(self,token,chat): made.append((token,chat))
        def send_text(self,*args,**kwargs): pass
    secrets=iter(("BOT_SECRET", "1234567890"))
    options=SimpleNamespace(configure_telegram=True,disable_telegram=False,test_notification=False,notification_status=False,remove_secret=False)
    assert handle_notification_cli(options,tmp_path,input_secret=lambda prompt:next(secrets),adapter_factory=CliAdapter,secret_store_factory=SecretStore)==0
    config=(tmp_path/"config"/"notifications.json").read_text()
    assert "BOT_SECRET" not in config and "1234567890" not in config and json.loads(config)["telegram"]["enabled"]
    assert json.loads(SecretStore.value) == {"token":"BOT_SECRET", "chat_id":"1234567890"}
    options=SimpleNamespace(configure_telegram=False,disable_telegram=True,test_notification=False,notification_status=False,remove_secret=True)
    handle_notification_cli(options,tmp_path,secret_store_factory=SecretStore)
    assert SecretStore.removed and not json.loads((tmp_path/"config"/"notifications.json").read_text())["telegram"]["enabled"]


def test_cli_parsing_and_chat_mask():
    assert parse_cli_arguments(["--configure-telegram"]).configure_telegram
    assert parse_cli_arguments(["--disable-telegram"]).disable_telegram
    assert parse_cli_arguments(["--test-notification"]).test_notification
    assert parse_cli_arguments(["--notification-status"]).notification_status
    assert mask_chat_id("1234567890")=="******7890"


def test_cli_test_records_delivery_and_status_hides_credentials(tmp_path, capsys):
    (tmp_path/"config").mkdir()
    (tmp_path/"config"/"notifications.json").write_text(
        json.dumps({"telegram":{"enabled":True}}), encoding="utf-8"
    )
    SecretStore.value=json.dumps({"token":"BOT_SECRET", "chat_id":"1234567890"})

    class CliAdapter:
        def __init__(self, token, chat): assert (token, chat)==("BOT_SECRET", "1234567890")
        def send_text(self, *args, **kwargs): pass

    options=SimpleNamespace(configure_telegram=False,disable_telegram=False,test_notification=True,notification_status=False,remove_secret=False)
    assert handle_notification_cli(options,tmp_path,adapter_factory=CliAdapter,secret_store_factory=SecretStore)==0
    history=json.loads((tmp_path/"data"/"runtime"/"notification_delivery_history.json").read_text(encoding="utf-8"))
    assert history[-1]["event_type"]=="test_notification"

    options=SimpleNamespace(configure_telegram=False,disable_telegram=False,test_notification=False,notification_status=True,remove_secret=False)
    assert handle_notification_cli(options,tmp_path,secret_store_factory=SecretStore)==0
    output=capsys.readouterr().out
    assert "DPAPI credentials restored: True" in output
    assert "Pending messages: 0" in output
    assert "BOT_SECRET" not in output and "1234567890" not in output
