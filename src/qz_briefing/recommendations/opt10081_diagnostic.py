"""One-symbol, validation-only raw OPT10081 diagnostic."""

from __future__ import annotations

import platform
import re
import struct
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from qz_briefing.kiwoom.qax_adapter import KiwoomQAxAdapter
from qz_briefing.runtime.unattended import atomic_write_json


KNOWN_RECORD_NAME = "주식일봉차트조회"
DAILY_FIELDS = ("일자", "시가", "고가", "저가", "현재가", "거래량", "거래대금", "수정주가구분")


def normalize_market_codes(raw: str) -> tuple[list[str], dict[str, int]]:
    items = str(raw).split(";")
    nonempty = [item.strip() for item in items if item.strip()]
    unique = list(dict.fromkeys(nonempty))
    valid = [item for item in unique if re.fullmatch(r"\d{6}", item)]
    return valid, {
        "raw_semicolon_items": len(items),
        "nonempty_items": len(nonempty),
        "unique_items": len(unique),
        "six_digit_codes": len(valid),
    }


def inspect_callback(adapter: Any, arguments: tuple[object, ...], current_parser_name: str) -> dict[str, object]:
    screen_no, rq_name, tr_code, record_name, prev_next = (str(value).strip() for value in arguments[:5])
    metadata = list(arguments[5:9]) + [""] * max(0, 4 - len(arguments[5:9]))
    data_length, error_code, message, supplementary_message = metadata[:4]
    callback_state = adapter.get_connect_state()
    names = {
        "callback_record_name": record_name,
        "known_record_name": KNOWN_RECORD_NAME,
        "current_parser_name": current_parser_name,
    }
    repeat_counts = {key: adapter.get_repeat_count(tr_code, value) for key, value in names.items()}
    callback_ex = adapter.get_comm_data_ex(tr_code, record_name)
    known_ex = adapter.get_comm_data_ex(tr_code, KNOWN_RECORD_NAME)
    parsed_count = repeat_counts["current_parser_name"]
    preferred_name = record_name if repeat_counts["callback_record_name"] else KNOWN_RECORD_NAME
    row_count = max(*repeat_counts.values(), len(callback_ex), len(known_ex))
    fields: dict[str, object] = {}
    if row_count:
        for label, index in (("first", 0), ("last", row_count - 1)):
            fields[label] = {
                field: adapter.get_comm_data(tr_code, preferred_name, index, field)
                for field in DAILY_FIELDS
            }
    return {
        "callback_at": datetime.now().isoformat(),
        "screen_no": screen_no,
        "rq_name": rq_name,
        "tr_code": tr_code,
        "record_name": record_name,
        "prev_next": prev_next,
        "data_length": str(data_length),
        "error_code": str(error_code),
        "message": str(message),
        "supplementary_message": str(supplementary_message),
        "connect_state_after_callback": callback_state,
        "repeat_counts": repeat_counts,
        "callback_record_get_comm_data_ex_rows": len(callback_ex),
        "known_record_get_comm_data_ex_rows": len(known_ex),
        "parsed_row_count": parsed_count,
        "observed_row_count": row_count,
        "sample_fields": fields,
        "continuation_blocked_on_empty": row_count == 0,
    }


def _login_once(adapter: KiwoomQAxAdapter, timeout_ms: int = 60_000) -> tuple[bool, int | None]:
    if adapter.get_connect_state() == 1:
        return True, 0
    from PyQt5.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    events: list[int] = []
    adapter.add_login_event_listener(lambda code: (events.append(code), loop.quit()))
    if adapter.request_connect() != 0:
        return False, None
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec_()
    timer.stop()
    adapter.finish_connect_attempt()
    return bool(events and events[-1] == 0 and adapter.get_connect_state() == 1), events[-1] if events else None


def _master_diagnostic(adapter: KiwoomQAxAdapter) -> dict[str, object]:
    result: dict[str, object] = {}
    for label, market in (("KOSPI", "0"), ("KOSDAQ", "10")):
        codes, counts = normalize_market_codes(adapter.get_raw_code_list_by_market(market))
        names = 0
        common = 0
        exclusions: Counter[str] = Counter()
        for code in codes:
            name = adapter.get_master_code_name(code)
            state = adapter.get_master_stock_state(code)
            info = adapter.get_master_stock_info(code)
            if name:
                names += 1
            normalized = f"{state};{info}".upper()
            excluded = next((token for token in ("ETF", "ETN", "리츠", "스팩", "우선주", "거래정지", "정리매매") if token in normalized), None)
            if excluded:
                exclusions[excluded] += 1
            else:
                common += 1
        result[label] = {**counts, "master_name_exists": names, "tradable": len(codes) - exclusions["거래정지"] - exclusions["정리매매"], "common_stock_possible": common, "excluded_by_state": dict(exclusions)}
    return result


def run_opt10081_diagnostic(project_root: Path, symbol: str, *, adapter_factory=KiwoomQAxAdapter) -> dict[str, object]:
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("symbol must be exactly six digits without an A prefix")
    if sys.version_info[:2] != (3, 11) or struct.calcsize("P") * 8 != 32:
        raise RuntimeError("Python 3.11 32-bit is required")
    from PyQt5.QtCore import QEventLoop, QTimer
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    root = project_root / "data" / "validation" / "recommendations" / "live_collection" / "diagnostics"
    adapter = adapter_factory()
    result: dict[str, object] = {
        "python": sys.version,
        "architecture": platform.architecture()[0],
        "symbol": symbol,
        "order_account_tr_requests": 0,
        "telegram_sends": 0,
        "operational_cache_writes": 0,
    }
    try:
        connected, login_code = _login_once(adapter)
        result.update({"login_event_code": login_code, "connect_state_after_login": adapter.get_connect_state()})
        if not connected:
            result["failure"] = "KIWOOM DISCONNECTED"
            return result
        master = _master_diagnostic(adapter)
        result["master"] = master
        result["connect_state_after_master"] = adapter.get_connect_state()
        atomic_write_json(root / "master_normalization.json", master)
        if result["connect_state_after_master"] != 1:
            result["failure"] = "KIWOOM DISCONNECTED AFTER MASTER"
            return result
        result["master_code_name"] = adapter.get_master_code_name(symbol)
        result["master_stock_state"] = adapter.get_master_stock_state(symbol)
        result["master_last_price"] = adapter.get_master_last_price(symbol)
        if not result["master_code_name"]:
            result["failure"] = "MASTER CODE NAME EMPTY"
            return result

        inputs = {"종목코드": symbol, "기준일자": datetime.now().strftime("%Y%m%d"), "수정주가구분": "1"}
        request = {"rq_name": "qz_diag_d1", "tr_code": "opt10081", "screen_no": "9181", "prev_next": 0, "inputs": inputs}
        result["request"] = request
        result["connect_state_before_set_input"] = adapter.get_connect_state()
        if result["connect_state_before_set_input"] != 1:
            result["failure"] = "KIWOOM DISCONNECTED BEFORE SETINPUTVALUE"
            return result
        for key, value in inputs.items():
            adapter.set_input_value(key, value)
        result["connect_state_before_comm_rq_data"] = adapter.get_connect_state()
        if result["connect_state_before_comm_rq_data"] != 1:
            result["failure"] = "KIWOOM DISCONNECTED BEFORE COMMRQDATA"
            return result

        loop = QEventLoop()
        callbacks: list[dict[str, object]] = []
        adapter.add_tr_data_listener(lambda *args: (callbacks.append(inspect_callback(adapter, args, request["rq_name"])), loop.quit()) if len(args) >= 5 and str(args[1]).strip() == request["rq_name"] else None)
        result["comm_rq_data_return_code"] = adapter.request_tr(request["rq_name"], request["tr_code"], 0, request["screen_no"])
        if result["comm_rq_data_return_code"] != 0:
            result["failure"] = "COMMRQDATA REJECTED"
            return result
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(60_000)
        loop.exec_()
        timer.stop()
        if not callbacks:
            result["failure"] = "TR CALLBACK TIMEOUT" if adapter.get_connect_state() == 1 else "KIWOOM DISCONNECTED BEFORE CALLBACK"
            return result
        result["callback"] = callbacks[0]
        result["success"] = int(callbacks[0]["observed_row_count"]) >= 120
        if not result["success"]:
            result["failure"] = "EMPTY SERVER RESPONSE" if not callbacks[0]["observed_row_count"] else "INSUFFICIENT ROWS"
        return result
    finally:
        atomic_write_json(root / f"opt10081_{symbol}.json", result)
        adapter.close()


def print_diagnostic(result: dict[str, object]) -> bool:
    if result.get("failure", "").startswith("KIWOOM DISCONNECTED"):
        print("LIVE DIAGNOSTIC FAILED: KIWOOM DISCONNECTED")
    for key in ("connect_state_after_login", "connect_state_after_master", "connect_state_before_set_input", "connect_state_before_comm_rq_data", "comm_rq_data_return_code"):
        print(f"{key.upper()}={result.get(key, 'NOT_REACHED')}")
    callback = result.get("callback", {})
    if isinstance(callback, dict):
        for key in ("error_code", "message", "rq_name", "record_name", "prev_next", "repeat_counts", "callback_record_get_comm_data_ex_rows", "known_record_get_comm_data_ex_rows", "parsed_row_count", "observed_row_count"):
            print(f"{key.upper()}={callback.get(key, 'NOT_REACHED')}")
    print(f"OPT10081 LIVE DIAGNOSTIC: {'PASS' if result.get('success') else 'FAIL'}")
    return bool(result.get("success"))
