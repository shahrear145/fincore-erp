from django.urls import path

from . import views

urlpatterns = [
    path("ops/status/", views.OpsStatusView.as_view()),
    path("ops/backups/", views.BackupListCreateView.as_view()),
    path("ops/backups/<uuid:backup_id>/verify/",
         views.BackupVerifyView.as_view()),
    path("security/devices/", views.DeviceListView.as_view()),
    path("security/devices/<uuid:device_id>/revoke/",
         views.DeviceRevokeView.as_view()),
    path("security/sessions/", views.SessionListView.as_view()),
    path("security/sessions/<uuid:session_id>/revoke/",
         views.SessionRevokeView.as_view()),
    path("security/totp/setup/", views.TOTPSetupView.as_view()),
    path("security/totp/confirm/", views.TOTPConfirmView.as_view()),
    path("security/totp/disable/", views.TOTPDisableView.as_view()),
    path("security/totp/status/", views.TOTPStatusView.as_view()),
]

