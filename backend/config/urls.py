"""URL configuration for FinCore ERP (backend)."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", __import__("apps.ops.views", fromlist=["HealthView"]).HealthView.as_view()),
    path("api/health/", __import__("apps.ops.views", fromlist=["HealthView"]).HealthView.as_view()),
    path("api/", include("apps.users.urls")),        # /api/auth/*, /api/me/*
    path("api/", include("apps.organizations.urls")),  # /api/businesses/*
    path("api/", include("apps.partners.urls")),     # /api/partners/*
    path("api/", include("apps.customers.urls")),    # /api/customers/*
    path("api/", include("apps.transactions.urls")),  # /api/transactions/*
    path("api/", include("apps.accounting.urls")),   # /api/accounting/*
    path("api/", include("apps.personal.urls")),     # /api/personal/*
    path("api/", include("apps.loans.urls")),        # /api/loans/* (loan & investment book)
    path("api/", include("apps.cashbook.urls")),    # /api/cashbook/* (cash count, stock, calculator)
    path("api/", include("apps.assistant.urls")),    # /api/assistant/*
    path("api/", include("apps.sync.urls")),         # /api/sync/*
    path("api/", include("apps.ops.urls")),          # /api/health, /api/ops/*, /api/security/*
    path("api/", include("apps.web.api_urls")),      # /api/auth/password/*, /api/exports/*, /api/me/reset/
    path("drive/", include("apps.drivesync.urls")),  # Google Drive sync UI
    path("", include("apps.web.urls")),               # Web UI
]

# Uploaded files (profile avatars) are served by the dev server in DEBUG only;
# production serves them from the web server / CDN.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
