"""Additive API routes consumed by the Flutter app.

Mounted at /api/ — never shadows an existing /api/ route and never changes
what the web UI does.
"""
from django.urls import path

from . import api

app_name = "web_api"

urlpatterns = [
    path("auth/password/forgot/", api.PasswordForgotView.as_view(), name="auth-forgot"),
    path("auth/password/reset/", api.PasswordResetView.as_view(), name="auth-reset"),
    path("auth/password/reset/resend/", api.PasswordResetResendView.as_view(), name="auth-reset-resend"),
    path("exports/khata/<str:fmt>/", api.KhataExportView.as_view(), name="export-khata"),
    path("exports/summary/<str:fmt>/", api.SummaryExportView.as_view(), name="export-summary"),
    path("exports/supercalc/<str:fmt>/", api.SuperCalcExportView.as_view(), name="export-supercalc"),
    path("customers/<uuid:customer_id>/reminder/",
         api.DueReminderView.as_view(), name="customer-reminder"),
    path("me/reset/", api.ResetDataView.as_view(), name="me-reset"),
]
