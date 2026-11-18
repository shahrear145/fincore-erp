from django.contrib import admin

from apps.assistant.models import AssistantQuery


@admin.register(AssistantQuery)
class AssistantQueryAdmin(admin.ModelAdmin):
    list_display = ("question", "intent", "scope", "user", "business",
                    "created_at")
    list_filter = ("scope", "intent")
    search_fields = ("question", "answer")
