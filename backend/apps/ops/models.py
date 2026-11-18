"""Operations & security models (Phase 8).

- TrustedDevice / SessionRecord: device/session hardening, revocation.
- TOTPDevice: stdlib RFC6238 TOTP (no extra dep) for 2FA prep.
- BackupRecord: encrypted backup metadata (S3 key or local path).
"""
import base64
import hashlib
import hmac
import os
import struct
import time

from django.conf import settings
from django.db import models

from apps.core.models import UUIDModel


class TrustedDevice(UUIDModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="trusted_devices")
    device_id = models.CharField(max_length=120)
    name = models.CharField(max_length=160, blank=True)
    platform = models.CharField(max_length=40, blank=True)  # android/web
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    last_seen = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["user", "device_id"], name="uniq_trusted_device")]
        ordering = ("-last_seen",)

    def __str__(self):
        return f"{self.user_id} {self.name or self.device_id}"


class SessionRecord(UUIDModel):
    """Server-side view of issued sessions for listing/revocation.
    Actual token invalidation uses SimpleJWT's blacklist (by JTI) when
    a refresh token is revoked; access tokens expire naturally."""
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="session_records")
    device = models.ForeignKey(
        TrustedDevice, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sessions")
    refresh_jti = models.CharField(max_length=255, blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def is_active(self):
        return self.revoked_at is None


class TOTPDevice(UUIDModel):
    """RFC6238 TOTP 2FA — stdlib only (HMAC-SHA1, 30s, 6 digits)."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="totp_device")
    secret = models.CharField(max_length=64)  # base32, no padding
    is_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def generate_secret():
        return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")

    def provisioning_uri(self, issuer="FinCore ERP"):
        label = getattr(self.user, "email", "") or str(self.user_id)
        return (f"otpauth://totp/{issuer}:{label}?secret={self.secret}"
                f"&issuer={issuer}&digits=6&period=30")

    def verify(self, code: str, window: int = 1) -> bool:
        try:
            code = str(code).strip().zfill(6)
        except Exception:
            return False
        key = base64.b32decode(self.secret + "=" * (-len(self.secret) % 8))
        now = int(time.time()) // 30
        for offset in range(-window, window + 1):
            msg = struct.pack(">Q", now + offset)
            digest = hmac.new(key, msg, hashlib.sha1).digest()
            pos = digest[-1] & 0x0F
            otp = (struct.unpack(">I", digest[pos:pos + 4])[0]
                   & 0x7FFFFFFF) % 1_000_000
            if f"{otp:06d}" == code:
                return True
        return False


class BackupRecord(UUIDModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="backups_created")
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    storage = models.CharField(max_length=10, default="local")  # local/s3
    location = models.CharField(max_length=512, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    encrypted = models.BooleanField(default=True)
    note = models.TextField(blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"Backup {self.status} {self.location}"
