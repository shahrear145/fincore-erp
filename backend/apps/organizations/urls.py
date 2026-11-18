from django.urls import path

from . import views

urlpatterns = [
    path("businesses/", views.BusinessListCreateView.as_view(), name="businesses"),
    path("businesses/mine/", views.MyBusinessesView.as_view(), name="my-businesses"),
    path(
        "businesses/<uuid:pk>/members/",
        views.BusinessMembersListAddView.as_view(),
        name="business-members",
    ),
    path(
        "businesses/<uuid:pk>/members/<uuid:member_pk>/activation/",
        views.BusinessMemberActivateDeactivateView.as_view(),
        name="business-member-activation",
    ),
    path(
        "businesses/<uuid:pk>/periods/",
        views.FinancialPeriodListCreateView.as_view(),
        name="financial-periods",
    ),
    path(
        "businesses/<uuid:pk>/periods/<int:period_pk>/lock/",
        views.FinancialPeriodLockView.as_view(),
        name="financial-period-lock",
    ),
]
