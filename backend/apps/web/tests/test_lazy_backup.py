"""Lazy (opportunistic) auto-backup — the free-hosting path.

Pinned rules:

- first call writes snapshots for all active users and returns True;
- a second call inside the interval returns False (rate-limited);
- sentinel mtime older than the interval re-triggers a run;
- maybe_run_auto_backup never raises, even if the disk is broken;
- home page view triggers the hook (integration smoke test).
"""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from django.test import Client, TestCase, override_settings

from apps.accounting.postings import ensure_system_accounts
from apps.drivesync import autobackup
from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user


class LazyBackupTests(TestCase):
    def setUp(self):
        self.owner = make_user("lazyowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Lazy Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)
        Partner.objects.create(business=self.biz, name="Karim",
                               status="ACTIVE", created_by=self.owner)

    def _tmp_dir(self):
        return tempfile.mkdtemp(prefix="fincore-lazy-test-")

    def test_first_run_writes_snapshots_and_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                self.assertIsNone(autobackup.last_auto_backup_at())
                ran = autobackup.maybe_run_auto_backup()
                self.assertTrue(ran)
                names = os.listdir(tmp)
                self.assertIn(autobackup.SENTINEL_NAME, names)
                json_files = [n for n in names if n.endswith(".json")]
                self.assertEqual(len(json_files), 1)  # one active user
                data = json.loads((Path(tmp) / json_files[0]).read_text(
                    encoding="utf-8"))
                self.assertEqual(data["version"], 1)
                self.assertIsNotNone(autobackup.last_auto_backup_at())

    def test_rate_limited_within_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                self.assertTrue(autobackup.maybe_run_auto_backup())
                self.assertFalse(autobackup.maybe_run_auto_backup())
                # exactly one snapshot file despite two calls
                json_files = [n for n in os.listdir(tmp)
                              if n.endswith(".json")]
                self.assertEqual(len(json_files), 1)

    def test_old_sentinel_retriggers_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                self.assertTrue(autobackup.maybe_run_auto_backup())
                sentinel = Path(tmp) / autobackup.SENTINEL_NAME
                old = sentinel.stat().st_mtime - (25 * 3600)
                os.utime(sentinel, (old, old))
                self.assertTrue(autobackup.maybe_run_auto_backup())

    def test_never_raises_when_dir_unwritable(self):
        # point at a file, not a directory — mkdir must fail inside
        with tempfile.NamedTemporaryFile(delete=False) as f:
            blocker = f.name
        try:
            with override_settings(AUTO_BACKUP_DIR=blocker + "/blocked"):
                self.assertFalse(autobackup.maybe_run_auto_backup())
        finally:
            os.unlink(blocker)

    def test_home_page_triggers_lazy_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                client = Client()
                client.force_login(self.owner)
                session = client.session
                session["active_business_id"] = str(self.biz.id)
                session.save()
                res = client.get("/")
                self.assertEqual(res.status_code, 200)
                json_files = [n for n in os.listdir(tmp)
                              if n.endswith(".json")]
                self.assertEqual(len(json_files), 1)


class DriveAutoSyncTests(TestCase):
    def setUp(self):
        from apps.organizations.models import Business
        self.owner = make_user("driveowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Drive Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)

    def _connect(self, user, encrypted="fake-enc"):
        from apps.drivesync.models import DriveAccount
        return DriveAccount.objects.create(
            user=user, email=f"{user.username}@example.com",
            refresh_token_encrypted=encrypted,
            status=DriveAccount.Status.CONNECTED,
            last_file_name="fincore-backup.json")

    def test_no_connected_accounts_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                self.assertEqual(autobackup.maybe_sync_connected_drives(), 0)
                self.assertTrue((Path(tmp) / autobackup.DRIVE_SENTINEL_NAME).exists())

    def test_push_runs_for_connected_account(self):
        from unittest import mock
        self._connect(self.owner)
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                with mock.patch(
                    "apps.drivesync.drive_push.push_user_snapshot",
                    return_value="file123",
                ) as push:
                    self.assertEqual(
                        autobackup.maybe_sync_connected_drives(), 1)
                    self.assertEqual(push.call_count, 1)
                    # second call inside the interval — no Drive hit
                    self.assertEqual(
                        autobackup.maybe_sync_connected_drives(), 0)
                    self.assertEqual(push.call_count, 1)

    def test_drive_disabled_toggle_blocks_push(self):
        from unittest import mock
        self._connect(self.owner)
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                autobackup.set_enabled(False)
                with mock.patch(
                    "apps.drivesync.drive_push.push_user_snapshot",
                ) as push:
                    self.assertEqual(
                        autobackup.maybe_sync_connected_drives(), 0)
                    push.assert_not_called()

    def test_drive_failure_does_not_raise_and_touches_sentinel(self):
        from unittest import mock
        self._connect(self.owner)
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                with mock.patch(
                    "apps.drivesync.drive_push.push_user_snapshot",
                    side_effect=RuntimeError("drive down"),
                ):
                    self.assertEqual(
                        autobackup.maybe_sync_connected_drives(), 0)
                self.assertTrue(
                    (Path(tmp) / autobackup.DRIVE_SENTINEL_NAME).exists())


class AutoBackupToggleTests(TestCase):
    def setUp(self):
        from apps.organizations.models import (
            Business, BusinessMembership,
        )
        self.owner = make_user("toggleowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Toggle Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)

        self.viewer = make_user("toggleviewer", "togglev@example.com")
        BusinessMembership.objects.create(
            user=self.viewer, business=self.biz,
            role=BusinessMembership.Role.VIEWER, is_active=True)

    def _client(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()
        return c

    # -- enable/disable mechanics ----------------------------------------
    def test_enabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                self.assertTrue(autobackup.is_enabled())

    def test_disable_blocks_lazy_run_and_enable_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                self.assertTrue(autobackup.maybe_run_auto_backup())
                autobackup.set_enabled(False)
                self.assertFalse(autobackup.is_enabled())
                # even after aging the sentinel past the interval
                sentinel = Path(tmp) / autobackup.SENTINEL_NAME
                old = sentinel.stat().st_mtime - (25 * 3600)
                os.utime(sentinel, (old, old))
                self.assertFalse(autobackup.maybe_run_auto_backup())
                autobackup.set_enabled(True)
                self.assertTrue(autobackup.is_enabled())
                self.assertTrue(autobackup.maybe_run_auto_backup())

    # -- the HTTP toggle ----------------------------------------------------
    def test_owner_can_flip_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                c = self._client(self.owner)
                self.assertTrue(autobackup.is_enabled())
                res = c.post("/drive/auto-backup/toggle/")
                self.assertEqual(res.status_code, 302)
                self.assertFalse(autobackup.is_enabled())
                # flip back on
                res = c.post("/drive/auto-backup/toggle/")
                self.assertEqual(res.status_code, 302)
                self.assertTrue(autobackup.is_enabled())

    def test_viewer_cannot_flip_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(AUTO_BACKUP_DIR=tmp):
                c = self._client(self.viewer)
                res = c.post("/drive/auto-backup/toggle/")
                self.assertEqual(res.status_code, 302)
                # unchanged — still enabled
                self.assertTrue(autobackup.is_enabled())

    def test_anonymous_redirected_to_login(self):
        with override_settings(AUTO_BACKUP_DIR=tempfile.mkdtemp()):
            res = Client().post("/drive/auto-backup/toggle/")
            self.assertEqual(res.status_code, 302)
            self.assertIn("/login", res["Location"])

    def test_toggle_requires_post(self):
        c = self._client(self.owner)
        res = c.get("/drive/auto-backup/toggle/")
        self.assertEqual(res.status_code, 405)
