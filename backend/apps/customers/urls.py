from django.urls import path

from . import views

urlpatterns = [
    path("customers/", views.CustomerListCreateView.as_view(), name="customers"),
    path(
        "customers/<uuid:customer_id>/",
        views.CustomerDetailView.as_view(),
        name="customer-detail",
    ),
    path(
        "customers/<uuid:customer_id>/dues/",
        views.CustomerDuesView.as_view(),
        name="customer-dues",
    ),
    path(
        "customers/<uuid:customer_id>/assignments/",
        views.CustomerAssignPartnerView.as_view(),
        name="customer-assign-partner",
    ),
    path(
        "customers/<uuid:customer_id>/statement/",
        views.CustomerStatementView.as_view(),
        name="customer-statement",
    ),
    path(
        "customers/<uuid:customer_id>/documents/",
        views.CustomerDocumentsView.as_view(),
        name="customer-documents",
    ),
]
