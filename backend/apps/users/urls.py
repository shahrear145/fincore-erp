from django.urls import path

from . import views

app_name = "users"

urlpatterns = [
    path("auth/register/", views.RegisterView.as_view(), name="register"),
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("auth/logout/", views.LogoutView.as_view(), name="logout"),
    path("auth/password/change/", views.ChangePasswordView.as_view(), name="change-password"),
    path("auth/pin/", views.PinManageView.as_view(), name="pin"),
    path("auth/sessions/", views.DeviceSessionsAPIView.as_view(), name="sessions"),
    path("auth/login-history/", views.LoginHistoryView.as_view(), name="login-history"),
    path("me/", views.MeView.as_view(), name="me"),
    path("me/update/", views.MeUpdateView.as_view(), name="me-update"),
]
