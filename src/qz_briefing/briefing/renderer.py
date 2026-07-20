"""Human-readable rendering for briefing execution results."""

from __future__ import annotations


def render_markdown(result: dict[str, object]) -> str:
    calendar = result["market_calendar"]
    collectors = result["collectors"]
    lines = [
        "# QZ Briefing Execution Result",
        "",
        f"- Briefing type: {result['briefing_type']}",
        f"- Trading date: {result['trading_date']}",
        f"- Completed at: {result['completed_at']}",
        f"- Overall status: {result['status']}",
        f"- Market calendar status: {calendar['status']}",  # type: ignore[index]
        f"- Market calendar reason: {calendar['reason']}",  # type: ignore[index]
        "",
        "## Collectors",
        "",
    ]
    for name, collector_result in collectors.items():  # type: ignore[union-attr]
        lines.append(f"- {name}: {collector_result['status']}")

    lines.extend(["", "## Warnings", ""])
    warnings = result["warnings"]
    lines.extend(f"- {warning}" for warning in warnings)  # type: ignore[union-attr]
    if not warnings:
        lines.append("- None")

    lines.extend(["", "## Errors", ""])
    errors = result["errors"]
    lines.extend(f"- {error}" for error in errors)  # type: ignore[union-attr]
    if not errors:
        lines.append("- None")

    lines.extend(
        [
            "",
            "> Market analysis and narrative generation are not connected yet.",
            "",
        ]
    )
    return "\n".join(lines)
