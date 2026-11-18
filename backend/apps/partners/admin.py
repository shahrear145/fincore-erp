from django.contrib import admin

from apps.partners.models import Partner


@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "business", "status", "initial_capital", "linked_user")
    list_filter = ("status", "business")
    search_fields = ("name", "phone", "email", "business__name")
