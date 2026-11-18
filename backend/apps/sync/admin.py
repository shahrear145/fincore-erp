from django.contrib import admin

from apps.sync.models import SyncCursor, SyncOp


@admin.register(SyncOp)
class SyncOpAdmin(admin.ModelAdmin):
    list_display = ("entity", "action", "entity_id", "status", "user",
                    "device_id", "created_at")
    list_filter = ("status", "entity", "action")


@admin.register(SyncCursor)
class SyncCursorAdmin(admin.ModelAdmin):
    list_display = ("user", "device_id", "cursor")
