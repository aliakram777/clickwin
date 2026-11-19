"""Hadisə axını adapterləri: təsdiq sorğusu və proaktiv bildirişlər `EventStore`-a yazılır; Android kursorla oxuyur.
Burada təsdiq/icra imkanı yoxdur: yalnız məlumat (təsdiqin payload-ı hadisəyə düşmür)."""
from __future__ import annotations

from agents.mobile import DeliveryResult, EventStore, Notification, clean_text, speakable
from safety.approvals import Ticket


class ApprovalEventChannel:
    """`ApprovalService(channels=[...])` kanalı: yeni təsdiq sorğusunu `approval_request` hadisəsi kimi yazır."""
    name = "android_events"

    def __init__(self, events: EventStore) -> None:
        self.events = events

    def notify(self, ticket: Ticket) -> None:
        self.events.append("approval_request", {
            "approval_id": ticket.id, "agent": ticket.agent, "permission": ticket.permission,
            "cost": float(ticket.cost), "description": clean_text(ticket.description, 200),
            "speech_text": speakable(f"{ticket.agent} üçün {ticket.permission} təsdiqi gözlənilir"),
            "expires_at": ticket.expires_at})


class EventNotificationChannel:
    """`ProactiveEngine.run` kanalı: bildirişi `notification` hadisəsi kimi yazır."""
    name = "android_events"

    def __init__(self, events: EventStore) -> None:
        self.events = events

    def deliver(self, notification: Notification) -> DeliveryResult:
        try:
            self.events.append("notification", notification.to_dict())
        except ValueError as exc:
            return DeliveryResult(False, clean_text(str(exc), 100))
        return DeliveryResult(True)
