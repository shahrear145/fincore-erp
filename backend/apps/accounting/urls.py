from django.urls import path

from . import views

urlpatterns = [
    path("accounting/accounts/", views.AccountListCreateView.as_view()),
    path("accounting/accounts/<uuid:account_id>/ledger/",
         views.AccountLedgerView.as_view()),
    path("accounting/journal-entries/",
         views.JournalEntryListCreateView.as_view()),
    path("accounting/journal-entries/<uuid:entry_id>/",
         views.JournalEntryDetailView.as_view()),
    path("accounting/journal-entries/<uuid:entry_id>/reversal/",
         views.JournalEntryReversalView.as_view()),
    path("accounting/trial-balance/", views.TrialBalanceView.as_view()),
    path("accounting/income-statement/",
         views.IncomeStatementView.as_view()),
    path("accounting/balance-sheet/", views.BalanceSheetView.as_view()),
    path("accounting/cash-flow/", views.CashFlowView.as_view()),
]
