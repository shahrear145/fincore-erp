"""অটো-ব্যাকআপ ও ক্লাউড সিঙ্ক (#১২) — download/upload/command tests.

Pinned rules:

- /drive/download/ returns a .json snapshot with the expected schema;
- the management command writes per-user files and prunes old ones;
- upload-restore parses files and reports failures safely;
- unauthenticated access is blocked.
"""
import json
import os
import tempfile
from datetime import date

from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from apps.accounting.postings import ensure_system_accounts
from apps.customers.models import Customer
from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user


class AutoBackupTests(TestCase):
    def setUp(self):
        self.owner = make_user("backupbotowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Backup Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)

        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": "Hosen Ali", "partner_id": str(self.partner.id),
             "phone": "01712345678", "email": "hosen@example.com"},
            format="json")
        self.customer = Customer.objects.get(pk=res.json()["id"])

        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()

    # -- local download ---------------------------------------------------
    def test_download_backup_returns_snapshot(self):
        res = self.client.get("/drive/download/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/json")
        self.assertIn("attachment;", res["Content-Disposition"])
        body = json.loads(res.content.decode("utf-8"))
        self.assertEqual(body.get("version"), 1)
        self.assertEqual(len(body.get("businesses", [])), 1)
        self.assertEqual(body["businesses"][0]["name"], "Backup Traders")
        self.assertIn("personal", body)
        rows = body["businesses"][0]["rows"]
        self.assertIn("customers", rows)

    def test_download_requires_login(self):
        res = Client().get("/drive/download/")
        self.assertEqual(res.status_code, 302)

    # -- management command -------------------------------------------------
    def test_auto_backup_command_writes_and_prunes(self):
        with tempfile.TemporaryDirectory() as out:
            for stamp in ("20260101", "20260201"):
                call_command("auto_backup", output=out, keep=2, verbosity=0)
            json_files = [f for f in os.listdir(out) if f.endswith(".json")]
            self.assertGreaterEqual(len(json_files), 1)
            # after many runs it caps at --keep
            for _ in range(5):
                call_command("auto_backup", output=out, keep=2, verbosity=0)
            json_files = [f for f in os.listdir(out) if f.endswith(".json")]
            self.assertEqual(len(json_files), 2)

    def test_auto_backup_command_with_no_business_does_not_crash(self):
        """A fresh user with no data should still get a valid snapshot."""
        alone = make_user("autobackupx", "autobackupx@example.com")
        from apps.drivesync.service import build_snapshot
        snap = build_snapshot(alone)
        self.assertEqual(snap["version"], 1)
        self.assertEqual(snap["businesses"], [])

    # -- upload/restore -----------------------------------------------------
    def test_upload_restore_requires_file(self):
        res = self.client.post("/drive/upload-restore/", {"confirm": "RESTORE"})
        # no file -> redirect with error message
        self.assertEqual(res.status_code, 302)

    def test_upload_restore_bad_json_fails_safely(self):
        bad = SimpleUploadedFile("bad.json", b"this is not json",
                                 content_type="application/json")
        res = self.client.post(
            "/drive/upload-restore/",
            {"backup_file": bad, "confirm": "RESTORE"})
        # reports the error as a message, does not explode
        self.assertEqual(res.status_code, 302)

    def test_upload_restore_with_real_snapshot(self):
        # first build the snapshot in memory
        res = self.client.get("/drive/download/")
        payload = res.content
        # upload it back to trigger restore path
        up = SimpleUploadedFile("backup.json", payload,
                                content_type="application/json")
        res = self.client.post(
            "/drive/upload-restore/",
            {"backup_file": up, "confirm": "RESTORE"})
        self.assertEqual(res.status_code, 302)
