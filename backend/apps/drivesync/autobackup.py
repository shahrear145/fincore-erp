"""Opportunistic (lazy) auto-backup — works on free hosting.

PythonAnywhere's *scheduled tasks* are a paid-plan feature, so we do not
rely on one. Instead, ordinary page views call ``maybe_run_auto_backup``;
at most once per ``min_interval_seconds`` it writes fresh per-user JSON
snapshots — exactly like the ``auto_backup`` management command.

Guarantees
    - never raises — a failed backup must never break a page;
    - rate-limited by a sentinel file (``backups/.last_auto_backup``);
    - reuses ``build_snapshot`` from ``apps.drivesync.service``;
    - prunes to the newest ``keep`` files per user, same as the command;
    - runs only when traffic exists — there is no background daemon.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

DEFAULT_INTERVAL_SECONDS = 24 * 3600  # once per day
DEFAULT_KEEP = 30
SENTINEL_NAME = ".last_auto_backup"
DISABLED_NAME = ".auto_backup_disabled"
DRIVE_SENTINEL_NAME = ".last_drive_sync"
DRIVE_INTERVAL_SECONDS = 24 * 3600


def is_enabled() -> bool:
    """True unless the site-wide auto-backup toggle has been switched off."""
    try:
        return not (backups_dir() / DISABLED_NAME).exists()
    except OSError:
        # cannot tell — stay enabled, snapshots are harmless
        return True


def set_enabled(enabled: bool) -> None:
    sentinel = backups_dir() / DISABLED_NAME
    try:
        if enabled:
            sentinel.unlink(missing_ok=True)
        else:
            sentinel.write_text("0", encoding="utf-8")
    except OSError:
        # read-only filesystem? toggle just won't stick — never crash
        pass


def backups_dir() -> Path:
    # AUTO_BACKUP_DIR lets tests redirect the output elsewhere
    d = Path(getattr(settings, "AUTO_BACKUP_DIR", None)
             or (Path(settings.BASE_DIR) / "backups"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def last_auto_backup_at():
    """Timestamp of the last lazy/command backup, or None."""
    try:
        mtime = (backups_dir() / SENTINEL_NAME).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.get_current_timezone())


def write_all_snapshots(out_dir: Path, *, keep: int = DEFAULT_KEEP) -> int:
    """Write one snapshot per active user and prune old files.

    Returns the number of users backed up. Raises on hard failure — the
    callers decide whether to swallow it (the page hook does; the
    management command wants the error visible).
    """
    from django.contrib.auth import get_user_model
    from apps.drivesync.service import build_snapshot

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S-%f")

    User = get_user_model()
    created = 0
    for user in User.objects.filter(is_active=True):
        payload = json.dumps(
            build_snapshot(user), ensure_ascii=False, indent=1).encode("utf-8")
        filename = f"{user.id}-{user.username}-{stamp}.json"
        (out_dir / filename).write_bytes(payload)
        created += 1

    _prune(out_dir, max(1, int(keep)))
    return created


def _prune(out_dir: Path, keep: int) -> None:
    per_user: dict[str, list[Path]] = {}
    for p in out_dir.glob("*.json"):
        per_user.setdefault(p.name.split("-", 1)[0], []).append(p)
    for files in per_user.values():
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass


def maybe_run_auto_backup(
    *, min_interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    keep: int = DEFAULT_KEEP,
) -> bool:
    """Run the backup if the interval has elapsed. Never raises.

    Returns True when a backup was written during this call.
    """
    try:
        if not is_enabled():
            return False
        d = backups_dir()
        sentinel = d / SENTINEL_NAME
        try:
            if time.time() - sentinel.stat().st_mtime < min_interval_seconds:
                return False
        except OSError:
            pass  # first run ever — no sentinel yet

        write_all_snapshots(d, keep=keep)
        # touch sentinel AFTER a successful write so a failure retries
        sentinel.write_text(str(int(time.time())), encoding="utf-8")
        return True
    except Exception:
        return False


def maybe_sync_connected_drives(
    *, min_interval_seconds: int = DRIVE_INTERVAL_SECONDS,
) -> int:
    """Push each connected user's snapshot to their Google Drive.

    At most once per ``min_interval_seconds`` (tracked in its own sentinel,
    ``backups/.last_drive_sync``) — independent of the local snapshot's
    cadence, so a Drive outage doesn't hold back local backups and vice
    versa. Gated by the same site-wide toggle as the local backup.

    Never raises. Returns the number of accounts successfully synced.
    On failure the sentinel is still touched so a sick Drive API cannot
    be hammered on every page view (the manual Sync button exists for a
    forced retry).
    """
    try:
        if not is_enabled():
            return 0
        d = backups_dir()
        sentinel = d / DRIVE_SENTINEL_NAME
        try:
            if time.time() - sentinel.stat().st_mtime < min_interval_seconds:
                return 0
        except OSError:
            pass  # first run ever — no sentinel yet

        from apps.drivesync.drive_push import push_user_snapshot
        from apps.drivesync.models import DriveAccount

        synced = 0
        accounts = (DriveAccount.objects
                    .filter(status=DriveAccount.Status.CONNECTED)
                    .exclude(refresh_token_encrypted="")
                    .select_related("user"))
        for account in accounts:
            if not account.user.is_active:
                continue
            try:
                push_user_snapshot(account)
                synced += 1
            except Exception:
                # token expired / Drive down / quota — skip this user,
                # retry another day
                continue

        # touch AFTER the attempt so a broken Drive API doesn't get
        # hit on every single page view
        try:
            sentinel.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass
        return synced
    except Exception:
        return 0
