"""Shared "push the current user's snapshot to their Google Drive" logic.

Used by the manual *Sync* button (``sync_view``) and by the lazy/automatic
auto-backup hook (``apps.drivesync.autobackup``) — both go through the same
code path so behaviour (folder name, file name, last_sync_at) never drifts.
"""

import json

from apps.drivesync import oauth
from apps.drivesync import service
from apps.drivesync.crypto import decrypt
from apps.drivesync.models import DriveAccount

FOLDER_NAME = "FinCore ERP Backup"
FILE_NAME = "fincore-backup.json"


def drive_credentials(account):
    if not account or not account.refresh_token_encrypted:
        raise oauth.DriveError("গুগল ড্রাইভ সংযুক্ত নয়।")
    try:
        refresh = decrypt(account.refresh_token_encrypted)
    except Exception:
        raise oauth.DriveError("সংরক্ষিত টকেন ডিক্রিপ্ট করা যায়নি। আবার সংযোগ করুন।")
    token = oauth.refresh_access_token(refresh)
    return token.get("access_token")


def ensure_backup_folder(token: str) -> str:
    folder = oauth.find_folder(FOLDER_NAME, token=token)
    if not folder:
        folder = oauth.create_folder(FOLDER_NAME, token=token)
    return folder["id"]


def push_user_snapshot(account: DriveAccount) -> str:
    """Upload/update the user's snapshot on their Drive.

    Returns the Drive file id. Updates ``account`` (last_sync_at etc.) on
    success. Raises on failure — callers decide how to handle it (the
    manual button surfaces the error; the auto-backup hook swallows it).
    """
    from django.utils import timezone

    token = drive_credentials(account)
    snapshot = service.build_snapshot(account.user)
    payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    folder_id = ensure_backup_folder(token)

    existing = oauth.find_file(FILE_NAME, parent=folder_id, token=token)
    if existing:
        oauth.update_file_contents(existing["id"], payload, token=token)
        file_id = existing["id"]
    else:
        up = oauth.upload_file(FILE_NAME, payload, parent=folder_id,
                               token=token)
        file_id = up.get("id", "")

    account.last_sync_at = timezone.now()
    account.last_file_id = file_id
    account.last_file_name = FILE_NAME
    account.status = DriveAccount.Status.CONNECTED
    account.save()
    return file_id
