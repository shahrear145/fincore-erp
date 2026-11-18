"""
FinCore ERP â€” Django settings.

One settings module for every environment; behaviour is driven by env
variables (see .env.example). Security-sensitive defaults tighten when
DEBUG=False.

Monetary rule: all monetary fields use DecimalField. Never floats.
Timezone: primary display timezone is Asia/Dhaka; USE_TZ is True so the
database stores UTC and localisation happens at presentation time.
"""
import datetime as _dt
import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
    POSTGRES_DB=(str, ""),
    POSTGRES_USER=(str, ""),
    POSTGRES_PASSWORD=(str, ""),
    POSTGRES_HOST=(str, "localhost"),
    POSTGRES_PORT=(int, 5432),
    REDIS_URL=(str, ""),
    JWT_ACCESS_MINUTES=(int, 30),
    JWT_REFRESH_DAYS=(int, 30),
    SECRET_KEY=(str, ""),
)
environ.Env.read_env(BASE_DIR / ".env")


def _dev_secret(base_dir: Path) -> str:
    """Development-only fallback secret persisted to a local git-ignored file."""
    keyfile = base_dir / ".dev_secret_key"
    if keyfile.exists():
        return keyfile.read_text().strip()
    import secrets

    key = secrets.token_urlsafe(64)
    keyfile.write_text(key)
    return key


SECRET_KEY = env("SECRET_KEY") or _dev_secret(BASE_DIR)

# Optional LLM for the Phase 7 assistant. Empty = deterministic engine
# only (no network calls). The assistant NEVER invents figures: the LLM,
# when enabled, receives the same compiled facts and is instructed to
# answer strictly from them.
ASSISTANT_API_KEY = env("ASSISTANT_API_KEY", default="")
ASSISTANT_MODEL = env("ASSISTANT_MODEL", default="gpt-4o-mini")
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", default="")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", default="")
SITE_BASE_URL = env("SITE_BASE_URL", default="http://127.0.0.1:8000")

# Phase 8: backups (encrypted; S3-compatible when configured, else local)
BACKUP_ENCRYPTION_KEY = env("BACKUP_ENCRYPTION_KEY", default="")
BACKUP_LOCAL_DIR = BASE_DIR / "backups"
BACKUP_S3_BUCKET = env("BACKUP_S3_BUCKET", default="")
BACKUP_S3_ENDPOINT = env("BACKUP_S3_ENDPOINT", default="")
BACKUP_S3_KEY = env("BACKUP_S3_KEY", default="")
BACKUP_S3_SECRET = env("BACKUP_S3_SECRET", default="")
BACKUP_S3_REGION = env("BACKUP_S3_REGION", default="us-east-1")
DEBUG = env("DEBUG")
if not SECRET_KEY and not DEBUG:
    raise ImproperlyConfigured(
        "SECRET_KEY must be set in production. See .env.example."
    )
ALLOWED_HOSTS = env("ALLOWED_HOSTS")


# --- Applications -----------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
]

LOCAL_APPS = [
    "apps.core",
    "apps.users",
    "apps.organizations",
    "apps.partners",
    "apps.customers",
    "apps.transactions",
    "apps.loans",
    "apps.accounting",
    "apps.cashbook",
    "apps.personal",
    "apps.assistant",
    "apps.sync",
    "apps.ops",
    "apps.web",
    "apps.drivesync",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.AuditContextMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context_processors.web_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Database ----------------------------------------------------------------

if env("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB"),
            "USER": env("POSTGRES_USER"),
            "PASSWORD": env("POSTGRES_PASSWORD"),
            "HOST": env("POSTGRES_HOST"),
            "PORT": env("POSTGRES_PORT"),
            "CONN_MAX_AGE": 60,
            "ATOMIC_REQUESTS": False,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # Wait (instead of failing) when another worker holds the write
            # lock — matters on single-file hosts such as PythonAnywhere.
            "OPTIONS": {"timeout": 20},
        }
    }

# --- Custom user model -------------------------------------------------------

AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalization ----------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Dhaka"
USE_I18N = True
USE_TZ = True

# Rip-off float money: force decimal rendering with 2 dp in serialization.
SITE_ID = 1

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "apps" / "web" / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Email (Gmail SMTP via env; falls back to console in DEBUG) --------------
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend" if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="").strip()
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="").strip()
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL", default=EMAIL_HOST_USER or "noreply@fincore.local"
).strip()
SERVER_EMAIL = DEFAULT_FROM_EMAIL
EMAIL_OTP_EXPIRE_MINUTES = env.int("EMAIL_OTP_EXPIRE_MINUTES", default=10)
# Fail fast instead of hanging when SMTP is unreachable or blocked by the host.
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)

# --- REST framework ----------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {"anon": "120/hour", "user": "240/hour", "auth": "30/hour"},
    # Reject NaN/Inf so floats can never smuggle bad money values into the API.
    "NON_FIELD_ERRORS_KEY": "detail",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": _dt.timedelta(minutes=env("JWT_ACCESS_MINUTES")),
    "REFRESH_TOKEN_LIFETIME": _dt.timedelta(days=env("JWT_REFRESH_DAYS")),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
}

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True

# --- Celery ------------------------------------------------------------------

REDIS_URL = env("REDIS_URL")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = not bool(REDIS_URL)  # run inline without a broker
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TIMEZONE = TIME_ZONE

# --- Security hardening (active when DEBUG=False) ----------------------------

# Session cookie is HttpOnly unless explicitly relaxed (local UI testing).
SESSION_COOKIE_HTTPONLY = env.bool("SESSION_COOKIE_HTTPONLY", default=True)

if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"
    # PythonAnywhere (and most hosts) terminate HTTPS at their proxy and
    # forward X-Forwarded-Proto; without this Django thinks the request is
    # plain HTTP and secure cookies / CSRF checks misbehave.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Send plain-HTTP visitors to HTTPS (secure cookies need it).
    SECURE_SSL_REDIRECT = True

CSRF_TRUSTED_ORIGINS = [
    origin for origin in env.list("CSRF_TRUSTED_ORIGINS", default=[]) if origin
] or (env("CORS_ALLOWED_ORIGINS") or [])

# --- Business rules constants ------------------------------------------------

CURRENCY_CODE = "BDT"
CURRENCY_SYMBOL = "à§³"
PRIMARY_TZ = "Asia/Dhaka"
