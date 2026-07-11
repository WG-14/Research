from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class ResearchNotifier(Protocol):
    """Minimal notification boundary for research command completion."""

    def notify(self, *, event: str, status: str, fields: Mapping[str, object]) -> None:
        ...


class DisabledResearchNotifier:
    """Default notifier: research execution never requires network delivery."""

    def notify(self, *, event: str, status: str, fields: Mapping[str, object]) -> None:
        return None


class OperationalCompatibilityResearchNotifier:
    """Notifier adapter used only by the legacy integrated CLI."""

    def notify(self, *, event: str, status: str, fields: Mapping[str, object]) -> None:
        from bithumb_bot.notifier import AlertSeverity, notify

        result = notify(
            f"{event} status={status} command={fields.get('command')} exit_code={fields.get('exit_code')}",
            severity=AlertSeverity.INFO if status == "success" else AlertSeverity.WARN,
            event_name=event,
            policy=str(fields.get("notification_policy") or "best_effort"),
            source_command=str(fields.get("command") or "research"),
        )
        if fields.get("notification_policy") == "require_delivery" and result.final_status != "delivered":
            raise RuntimeError("research_notification_delivery_failed")
