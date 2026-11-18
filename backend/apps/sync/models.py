"""Offline sync models (Phase 8).

Protocol:
- Client (Flutter/drift SQLite) generates UUIDs locally and queues ops.
- Each op carries: entity, entity_id (client UUID), action, payload,
  base_updated_at (conflict token) and a client_reference.
- Server applies idempotently via the validated writers where possible,
  or via a controlled upsert for simple entities. PostgreSQL is the
  authority.
- Per-item result: applied | conflict | rejected. Conflicts store the
  server snapshot for manual resolution; nothing auto-merges money.
"""
from django.conf import settings
from django.db import models

from apps.core.models import UUIDModel


class SyncOp(models.Model):
    """A recorded client operation and its server-side outcome."""

    class Action(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        DELETE = "DELETE", "Delete"

    class Status(models.TextChoices):
        APPLIED = "applied", "Applied"
        CONFLICT = "conflict", "Conflict"
        REJECTED = "rejected", "Rejected"
        PENDING = "pending", "Pending resolution"

    id = models.UUIDField(primary_key=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="sync_ops", db_index=True)
    device_id = models.CharField(max_length=120, db_index=True)
    entity = models.CharField(max_length=60, db_index=True)
    entity_id = models.CharField(max_length=64, db_index=True)
    action = models.CharField(max_length=8, choices=Action.choices)
    client_reference = models.CharField(max_length=120, blank=True,
                                        db_index=True)
    base_updated_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    server_entity_id = models.CharField(max_length=64, blank=True)
    server_snapshot = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [
            models.Index(fields=["user", "entity", "entity_id"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        return f"{self.entity} {self.action} {self.entity_id} → {self.status}"


class SyncCursor(UUIDModel):
    """Per-user, per-device pull cursor (server-side change feed anchor)."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="sync_cursors")
    device_id = models.CharField(max_length=120)
    cursor = models.CharField(max_length=64, default="0")

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["user", "device_id"], name="uniq_sync_cursor_device")]
