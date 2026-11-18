"""Custom user, device sessions, and login history (Section 18).

Authentication: JWT access + refresh tokens (SimpleJWT) with rotation and
blacklisting. Email is the primary login identifier; username also accepted.
"""
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from apps.core.models import UUIDModel


class User(AbstractUser):
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=32, blank=True)
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)

    # --- Security lock options ---
    pin_hash = models.CharField(max_length=128, blank=True)   # app-level PIN lock
    biometric_enabled = models.BooleanField(default=False)
    two_factor_enabled = models.BooleanField(default=False)
    two_factor_secret = models.CharField(max_length=64, blank=True)

    # Preferences persisted across restarts (used by the Flutter client):
    active_business_id = models.UUIDField(null=True, blank=True)
    session_auto_logout_minutes = models.PositiveIntegerField(default=15)
    # Web UI theme (see apps/web/static/web/themes.css and modern.css for the library)
    theme = models.CharField(max_length=32, default="aurora", db_index=True)

    class Meta:
        indexes = [models.Index(fields=["username"]), models.Index(fields=["email"])]

    REQUIRED_FIELDS = ["email"]

    def set_pin(self, raw_pin: str):
        from django.contrib.auth.hashers import make_password
        if not (raw_pin.isdigit() and len(raw_pin) in (4, 5, 6)):
            raise ValueError("PIN must be 4-6 digits.")
        self.pin_hash = make_password(raw_pin)

    def check_pin(self, raw_pin: str) -> bool:
        from django.contrib.auth.hashers import check_password
        if not self.pin_hash or not (raw_pin and raw_pin.isdigit()):
            return False
        return check_password(raw_pin, self.pin_hash)


class UserDeviceSession(UUIDModel):
    """Device/session history so users can audit where they are logged in."""

    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             related_name="device_sessions")
    device_name = models.CharField(max_length=128, blank=True)
    device_id = models.CharField(max_length=128, blank=True, db_index=True)
    platform = models.CharField(max_length=32, blank=True)  # android/web/desktop
    refresh_jti = models.CharField(max_length=128, blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-last_seen_at",)

    def revoke(self):
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=["is_active", "revoked_at"])


class LoginEvent(UUIDModel):
    class Result(models.TextChoices):
        SUCCESS = "success"
        FAILED = "failed"

    user = models.ForeignKey(User, null=True, on_delete=models.CASCADE,
                             related_name="login_events")
    username_attempted = models.CharField(max_length=150, blank=True)
    result = models.CharField(max_length=8, choices=Result.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)


class EmailOTP(UUIDModel):
    """One-time code for email verification (registration) — hashed, expiring."""

    class Purpose(models.TextChoices):
        REGISTER = "register", "Register"
        EMAIL_CHANGE = "email_change", "Email change"
        PASSWORD_RESET = "password_reset", "Password reset"

    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             related_name="email_otps")
    code_hash = models.CharField(max_length=128)
    purpose = models.CharField(max_length=16, choices=Purpose.choices,
                               default=Purpose.REGISTER)
    expires_at = models.DateTimeField(db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "purpose", "expires_at"])]

    def is_valid(self):
        return self.consumed_at is None and timezone.now() <= self.expires_at and self.attempts < 5

    @staticmethod
    def hash_code(raw):
        from django.contrib.auth.hashers import make_password
        return make_password(str(raw).strip())

    def check_code(self, raw):
        from django.contrib.auth.hashers import check_password
        return check_password(str(raw).strip(), self.code_hash)
