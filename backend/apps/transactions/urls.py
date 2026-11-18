from django.urls import path

from . import views

urlpatterns = [
    path("transactions/", views.TransactionListCreateView.as_view(),
         name="transactions"),
    path("transactions/<uuid:txn_id>/", views.TransactionDetailView.as_view(),
         name="transaction-detail"),
    path("transactions/<uuid:txn_id>/reversal/", views.TransactionReversalView.as_view(),
         name="transaction-reversal"),
    path("transactions/<uuid:txn_id>/receipt/", views.TransactionReceiptView.as_view(),
         name="transaction-receipt"),
    path("transactions/collections/", views.CollectionWorkflowView.as_view(),
         name="collections"),
    path("transactions/date-memory/", views.DateMemoryView.as_view(),
         name="date-memory"),
    path("transactions/summary/", views.DashboardSummaryView.as_view(),
         name="transactions-summary"),
    path("transactions/recurring/", views.RecurringListCreateView.as_view(),
         name="recurring"),
    path("transactions/recurring/<uuid:rule_id>/",
         views.RecurringDetailView.as_view(), name="recurring-detail"),
    path("transactions/recurring/<uuid:rule_id>/run/",
         views.RecurringRunView.as_view(), name="recurring-run"),
    path("transactions/recurring/run-due/",
         views.RecurringRunDueView.as_view(), name="recurring-run-due"),
    path("transactions/duplicates/",
         views.DuplicatesReviewView.as_view(), name="duplicates"),
    path("transactions/alerts/", views.BusinessAlertsView.as_view(),
         name="business-alerts"),
]
