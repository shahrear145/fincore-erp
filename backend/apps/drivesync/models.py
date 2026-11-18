from django.conf import settings
from django.db import models

from apps.core.models import UUIDModel


class DriveAccount(UUIDModel):
    """Per-user link to Google Drive (file scope only).

    The refresh token is stored Fernet-encrypted at rest, never in
    plain text and never in backups/dumps.
    """

    class Status(models.TextChoices):
        CONNECTED = "CONNECTED", "Connected"
        EXPIRED = "EXPIRED", "Token expired"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="drive_account")
    email = models.EmailField(blank=True)      # shown in UI; the Drive identity
    refresh_token_encrypted = models.TextField(blank=True)
    scope = models.TextField(blank=True)       # scopes Google actually granted
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.CONNECTED)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_restore_at = models.DateTimeField(null=True, blank=True)
    last_file_id = models.CharField(max_length=128, blank=True)
    last_file_name = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user"])]

    def __str__(self):
        return f"Drive<{self.email or self.user_id}>"
