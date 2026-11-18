from django.urls import path

from . import views

urlpatterns = [
    path("partners/", views.PartnerListCreateView.as_view(), name="partners"),
    path(
        "partners/<uuid:partner_id>/",
        views.PartnerDetailView.as_view(),
        name="partner-detail",
    ),
    path(
        "partners/<uuid:partner_id>/status/",
        views.PartnerStatusView.as_view(),
        name="partner-status",
    ),
    path(
        "partners/<uuid:partner_id>/dashboard/",
        views.PartnerDashboardView.as_view(),
        name="partner-dashboard",
    ),
]
