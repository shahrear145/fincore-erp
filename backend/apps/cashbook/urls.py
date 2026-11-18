from django.urls import path

from . import views

urlpatterns = [
    path("cashbook/overview/", views.CashbookOverviewView.as_view(),
         name="cashbook-overview"),
    path("cashbook/day-rows/", views.CashbookDayRowsView.as_view(),
         name="cashbook-day-rows"),
    path("cashbook/months/", views.CashbookMonthsView.as_view(),
         name="cashbook-months"),
    path("cashbook/compare/", views.CashbookCompareView.as_view(),
         name="cashbook-compare"),
    path("cashbook/position-rows/", views.CashbookPositionRowsView.as_view(),
         name="cashbook-position-rows"),
    path("cashbook/counts/", views.CashCountListCreateView.as_view(),
         name="cashbook-counts"),
    path("cashbook/stock-snapshots/", views.StockSnapshotListCreateView.as_view(),
         name="cashbook-stock-snapshots"),
]
