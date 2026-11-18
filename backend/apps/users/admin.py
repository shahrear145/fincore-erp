from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from apps.users.models import LoginEvent, User, UserDeviceSession


@admin.register(User)
class FinUserAdmin(UserAdmin):
    list_display = ("username", "email", "phone", "is_active", "date_joined")
    fieldsets = UserAdmin.fieldsets + (
        ("Security", {"fields": ("pin_hash", "biometric_enabled", "two_factor_enabled",
                                 "two_factor_secret")}),
        ("Preferences", {"fields": ("active_business_id", "session_auto_logout_minutes")}),
    )


@admin.register(UserDeviceSession)
class DeviceSessionAdmin(admin.ModelAdmin):
    list_display = ("user", "device_name", "platform", "ip_address", "is_active", "last_seen_at")
    list_filter = ("platform", "is_active")


@admin.register(LoginEvent)
class LoginEventAdmin(admin.ModelAdmin):
    list_display = ("user", "username_attempted", "result", "ip_address", "created_at")
    list_filter = ("result",)
