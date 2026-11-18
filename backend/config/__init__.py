# Celery bootstrap — safe to import even when Redis is not configured.
try:
    from .celery import app as celery_app

    __all__ = ("celery_app",)
except Exception:  # pragma: no cover - celery optional in dev without Redis
    celery_app = None
