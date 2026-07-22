# -*- coding: utf-8 -*-
"""UI-only labels and safe human-readable numeric formatting."""

STATUS_LABELS = {
    "CONNECTED": "연결 정상", "RECHECKING": "연결 재확인 중",
    "RECONNECT_WAIT": "재연결 대기", "RECONNECTING": "재연결 시도 중",
    "FAILED": "연결 복구 실패", "CONNECTING": "연결 중", "DISCONNECTED": "연결 끊김",
    "SHUTTING_DOWN": "종료 정리 중", "STOPPED": "연결 종료",
    "strong_downtrend": "강한 하락추세", "downtrend": "하락추세",
    "sideways": "횡보", "uptrend": "상승추세", "strong_uptrend": "강한 상승추세",
    "confirmed": "바닥 확인", "partially_confirmed": "부분 확인",
    "not_confirmed": "미확인", "attempting_bottom": "바닥 확인 시도",
    "averaging_down_candidate": "물타기 검토 후보",
    "averaging_down_high_risk": "물타기 고위험",
    "adding_to_winner_candidate": "불타기 검토 후보",
    "add_on_strength_candidate": "불타기 검토 후보",
    "reduce_position_review": "비중 축소 검토", "reduce_risk": "비중 축소 검토",
    "exit_review": "탈출 검토", "wait": "대기", "no_action": "관망",
    "insufficient_data": "자료 부족",
    "do_not_add": "추가매수 금지", "observe_only": "관찰만",
    "conditional_add_review": "조건 충족 시 추가매수 검토",
    "hold_and_monitor": "보유하며 관찰", "protect_profit": "수익 보호",
    "reduce_risk": "위험 축소 검토", "exit_condition_check": "이탈 조건 확인",
}


def status_label(value: object) -> str:
    if value is None or value == "": return "자료 없음"
    return STATUS_LABELS.get(str(value), str(value))


def money(value: object) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "-"


def number(value: object) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else "-"


def percent(value: object) -> str:
    return f"{value:+,.2f}%" if isinstance(value, (int, float)) else "-"


def mask_account(value: object) -> str:
    text = str(value or "")
    if not text: return "-"
    if "*" in text: return text
    return "*" * max(0, len(text) - 4) + text[-4:]
