"""Testing settings: same as base but with throttling neutralized.

Run tests with:
    python manage.py test apps --settings=config.testing
"""
import datetime as _dt

from config.settings import *  # noqa: F401,F403
from config.settings import REST_FRAMEWORK  # rebind to copy

# Tests must not write backup snapshot files into the repo — redirect them
# to a fresh temp dir for this test run.
import tempfile as _tempfile
AUTO_BACKUP_DIR = _tempfile.mkdtemp(prefix="fincore-test-backups-")

THROTTLES_OFF = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_CLASSES": (),
}

REST_FRAMEWORK = THROTTLES_OFF

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": _dt.timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": _dt.timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
}
