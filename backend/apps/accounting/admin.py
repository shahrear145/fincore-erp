from django.contrib import admin

from apps.accounting.models import Account, JournalEntry, JournalLine


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "type", "is_system", "business")
    list_filter = ("type", "is_system")


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("journal_no", "transaction_date", "memo", "source_kind")
    inlines = (JournalLineInline,)
    date_hierarchy = "transaction_date"
