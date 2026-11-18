from django.contrib import admin

from apps.ops.models import BackupRecord, SessionRecord, TOTPDevice, TrustedDevice


@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "name", "device_id", "platform", "is_active",
                    "last_seen")


@admin.register(SessionRecord)
class SessionRecordAdmin(admin.ModelAdmin):
    list_display = ("user", "device", "ip_address", "created_at",
                    "revoked_at")


@admin.register(TOTPDevice)
class TOTPDeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "is_confirmed", "created_at")


@admin.register(BackupRecord)
class BackupRecordAdmin(admin.ModelAdmin):
    list_display = ("status", "storage", "location", "size_bytes",
                    "encrypted", "created_at")
    list_filter = ("status", "storage", "encrypted")
