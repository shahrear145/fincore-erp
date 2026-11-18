"""FinCore web-app models — event-based notifications.

Why a table (not a computed-only list):

- the unread badge and read/unread state must survive reload and follow the
  user to other devices (list your own rows only);
- event rows are the shop's audit trail ("when did we last chase Karim?").

Live time-based alerts (upcoming/overdue installments, budgets already over)
are intentionally **not** stored — they are computed fresh on every render so
the list can never drift from reality.
"""
from decimal import Decimal
import uuid

from django.conf import settings
from django.db import models

from apps.organizations.models import Business


class Notification(models.Model):
    """One per-user notification event. Rows are append-only; nothing here
    ever changes the ledger — this is a hand-off layer, like DueReminder."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Kind(models.TextChoices):
        DUE_INCREASED = "DUE_INCREASED", "বাকি বাড়ল"
        REMINDER_SENT = "REMINDER_SENT", "রিমাইন্ডার পাঠানো হলো"
        BUDGET_OVER = "BUDGET_OVER", "বাজেট শেষ"
        TEAM_UPDATE = "TEAM_UPDATE", "টিম পরিবর্তন"

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="notifications")
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="notifications")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    title = models.CharField(max_length=160)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=400, blank=True)
    amount = models.DecimalField(
        max_digits=18, decimal_places=2,
        default=Decimal("0.00"))
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["recipient", "is_read"]),
            models.Index(fields=["recipient", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.recipient.username} · {self.get_kind_display()} · {self.title[:40]}"
