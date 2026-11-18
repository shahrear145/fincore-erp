"""Phase 8 tests — sync, security hardening, backups, monitoring."""
import base64
import hashlib
import hmac
import struct
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.personal.models import PersonalAccount, PersonalCategory, PersonalTransaction
from apps.sync.models import SyncOp
from apps.tests_common import auth_client, make_user


def money(v):
    return Decimal(str(v)).quantize(Decimal("0.01"))


def totp_code(secret: str, for_time: int | None = None) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    t = int(for_time or time.time()) // 30
    msg = struct.pack(">Q", t)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    pos = digest[-1] & 0x0F
    otp = (struct.unpack(">I", digest[pos:pos + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{otp:06d}"


class SyncTests(TestCase):
    def setUp(self):
        self.u1 = make_user("s8u1")
        self.u2 = make_user("s8u2")
        PersonalCategory.seed_defaults(self.u1)

    def test_push_creates_account_and_txn_idempotently(self):
        client = auth_client(self.u1)
        acc_id = str(uuid.uuid4())
        op1 = {"op_id": str(uuid.uuid4()), "entity": "personal_account",
               "entity_id": acc_id, "action": "CREATE",
               "payload": {"name": "Offline Cash", "kind": "CASH",
                           "initial_balance": "500.00"}}
        res = client.post("/api/sync/push/", {"device_id": "dev-a",
                                              "ops": [op1]}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["summary"]["applied"], 1)
        self.assertTrue(PersonalAccount.objects.filter(
            user=self.u1, pk=acc_id).exists())

        # replay same batch — must not duplicate
        res = client.post("/api/sync/push/", {"device_id": "dev-a",
                                              "ops": [op1]}, format="json")
        self.assertEqual(res.json()["summary"]["applied"], 1)
        self.assertEqual(PersonalAccount.objects.filter(
            user=self.u1, name="Offline Cash").count(), 1)

        # push a transaction against that account
        txn_op = {"op_id": str(uuid.uuid4()), "entity": "personal_transaction",
                  "entity_id": str(uuid.uuid4()), "action": "CREATE",
                  "client_reference": "sync-txn-001",
                  "payload": {"transaction_type": "EXPENSE", "amount": "120.00",
                              "transaction_date": "2026-11-05",
                              "account": acc_id, "description": "Tea"}}
        res = client.post("/api/sync/push/", {"device_id": "dev-a",
                                              "ops": [txn_op]}, format="json")
        self.assertEqual(res.json()["summary"]["applied"], 1)
        acct = PersonalAccount.objects.get(pk=acc_id)
        self.assertEqual(acct.balance(), money("380.00"))

        # replay txn — idempotent, balance unchanged
        res = client.post("/api/sync/push/", {"device_id": "dev-a",
                                              "ops": [txn_op]}, format="json")
        self.assertEqual(res.json()["results"][0]["status"], "applied")
        self.assertEqual(PersonalAccount.objects.get(pk=acc_id).balance(),
                         money("380.00"))

    def test_overdraft_rejected_by_sync(self):
        client = auth_client(self.u1)
        acc_id = str(uuid.uuid4())
        client.post("/api/sync/push/", {"device_id": "d", "ops": [
            {"op_id": str(uuid.uuid4()), "entity": "personal_account",
             "entity_id": acc_id, "action": "CREATE",
             "payload": {"name": "Empty", "kind": "CASH",
                         "initial_balance": "0.00"}}]}, format="json")
        res = client.post("/api/sync/push/", {"device_id": "d", "ops": [
            {"op_id": str(uuid.uuid4()), "entity": "personal_transaction",
             "entity_id": str(uuid.uuid4()), "action": "CREATE",
             "client_reference": "overdraft-1",
             "payload": {"transaction_type": "EXPENSE", "amount": "50.00",
                         "transaction_date": "2026-11-05",
                         "account": acc_id}}]}, format="json")
        self.assertEqual(res.json()["summary"]["rejected"], 1)
        err = res.json()["results"][0]["error"]
        self.assertIn("available", str(err).lower() + str(err))

    def test_conflict_on_stale_update(self):
        acct = PersonalAccount.objects.create(
            user=self.u1, name="Wallet", kind="CASH",
            initial_balance=money("0"))
        stale_base = (acct.updated_at.replace(microsecond=0)
                      - __import__("datetime").timedelta(seconds=60)
                      ).isoformat()
        # server moves forward
        acct.name = "Wallet v2"
        acct.save()
        client = auth_client(self.u1)
        res = client.post("/api/sync/push/", {"device_id": "d", "ops": [
            {"op_id": str(uuid.uuid4()), "entity": "personal_account",
             "entity_id": str(acct.pk), "action": "UPDATE",
             "base_updated_at": stale_base,
             "payload": {"name": "Offline rename"}}]}, format="json")
        self.assertEqual(res.json()["summary"]["conflict"], 1)
        snap = res.json()["results"][0]["server_snapshot"]
        self.assertEqual(snap["name"], "Wallet v2")

    def test_pull_returns_snapshot_and_cursor(self):
        PersonalAccount.objects.create(
            user=self.u1, name="Cash", kind="CASH",
            initial_balance=money("100"))
        res = auth_client(self.u1).get("/api/sync/pull/?device_id=d1")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(any(a["name"] == "Cash" for a in res.json()["accounts"]))
        self.assertIn("cursor", res.json())

    def test_cross_user_isolation(self):
        acct = PersonalAccount.objects.create(
            user=self.u1, name="Secret", kind="CASH",
            initial_balance=money("999"))
        # user2 trying to push a txn against user1's account must be rejected
        res = auth_client(self.u2).post("/api/sync/push/", {
            "device_id": "d", "ops": [{
                "op_id": str(uuid.uuid4()), "entity": "personal_transaction",
                "entity_id": str(uuid.uuid4()), "action": "CREATE",
                "client_reference": "steal-1",
                "payload": {"transaction_type": "EXPENSE", "amount": "1.00",
                            "transaction_date": "2026-11-05",
                            "account": str(acct.pk)}}]}, format="json")
        self.assertEqual(res.json()["summary"]["rejected"], 1)
        self.assertEqual(acct.balance(), money("999"))

    def test_txn_update_rejected_money_immutable(self):
        acc_id = str(uuid.uuid4())
        txn_id = str(uuid.uuid4())
        client = auth_client(self.u1)
        client.post("/api/sync/push/", {"device_id": "d", "ops": [
            {"op_id": str(uuid.uuid4()), "entity": "personal_account",
             "entity_id": acc_id, "action": "CREATE",
             "payload": {"name": "C", "kind": "CASH",
                         "initial_balance": "100.00"}},
            {"op_id": str(uuid.uuid4()), "entity": "personal_transaction",
             "entity_id": txn_id, "action": "CREATE",
             "client_reference": "im-1",
             "payload": {"transaction_type": "EXPENSE", "amount": "10.00",
                         "transaction_date": "2026-11-05",
                         "account": acc_id}}]}, format="json")
        res = client.post("/api/sync/push/", {"device_id": "d", "ops": [
            {"op_id": str(uuid.uuid4()), "entity": "personal_transaction",
             "entity_id": txn_id, "action": "UPDATE",
             "payload": {"amount": "999.00"}}]}, format="json")
        self.assertEqual(res.json()["summary"]["rejected"], 1)


class SecurityTests(TestCase):
    def setUp(self):
        self.u1 = make_user("s8sec")
        self.u1.is_staff = True
        self.u1.save(update_fields=["is_staff"])

    def test_health_public(self):
        from rest_framework.test import APIClient
        res = APIClient().get("/api/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")
        self.assertEqual(res.json()["checks"]["database"], "ok")

    def test_ops_status_staff_only(self):
        other = make_user("s8other")
        res = auth_client(other).get("/api/ops/status/")
        self.assertEqual(res.status_code, 403)
        res = auth_client(self.u1).get("/api/ops/status/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["database"])

    def test_device_register_and_revoke(self):
        client = auth_client(self.u1)
        res = client.post("/api/security/devices/",
                          {"device_id": "android-abc", "name": "Pixel",
                           "platform": "android"}, format="json")
        self.assertEqual(res.status_code, 201)
        dev_id = res.json()["id"]
        res = client.get("/api/security/devices/")
        self.assertEqual(len(res.json()["devices"]), 1)
        res = client.post(f"/api/security/devices/{dev_id}/revoke/")
        self.assertEqual(res.status_code, 200)
        res = client.get("/api/security/devices/")
        self.assertFalse(res.json()["devices"][0]["is_active"])

    def test_totp_lifecycle(self):
        client = auth_client(self.u1)
        res = client.get("/api/security/totp/status/")
        self.assertFalse(res.json()["enabled"])

        res = client.post("/api/security/totp/setup/")
        self.assertEqual(res.status_code, 200)
        secret = res.json()["secret"]
        self.assertIn("otpauth://", res.json()["provisioning_uri"])

        # wrong code
        res = client.post("/api/security/totp/confirm/", {"code": "000000"},
                          format="json")
        self.assertEqual(res.status_code, 400)

        # correct code
        res = client.post("/api/security/totp/confirm/",
                          {"code": totp_code(secret)}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["confirmed"])

        res = client.get("/api/security/totp/status/")
        self.assertTrue(res.json()["enabled"])

        # disable requires valid code
        res = client.post("/api/security/totp/disable/", {"code": "000000"},
                          format="json")
        self.assertEqual(res.status_code, 400)
        res = client.post("/api/security/totp/disable/",
                          {"code": totp_code(secret)}, format="json")
        self.assertEqual(res.status_code, 200)
        res = client.get("/api/security/totp/status/")
        self.assertFalse(res.json()["enabled"])

    @override_settings(BACKUP_S3_BUCKET="")
    def test_backup_create_and_verify_local(self):
        # ensure a clean local backup dir under temp
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with override_settings(BACKUP_LOCAL_DIR=td):
                client = auth_client(self.u1)
                res = client.post("/api/ops/backups/",
                                  {"note": "phase8-test"}, format="json")
                self.assertEqual(res.status_code, 201, res.content)
                body = res.json()
                self.assertEqual(body["status"], "COMPLETED")
                self.assertTrue(body["encrypted"])
                self.assertTrue(Path(body["location"]).exists())
                self.assertTrue(body["sha256"])

                res = client.post(
                    f"/api/ops/backups/{body['id']}/verify/", format="json")
                self.assertEqual(res.status_code, 200)
                self.assertTrue(res.json()["valid"])
                self.assertTrue(res.json()["sha256_match"])
                self.assertTrue(res.json()["decrypts"])


class BusinessSyncTests(TestCase):
    def setUp(self):
        from apps.tests_phase4 import build_env, add_customer
        self.env = build_env("s8biz")
        self.owner, self.biz = self.env["owner"], self.env["biz"]
        self.p1 = self.env["p1"]
        self.customer = add_customer(auth_client(self.owner), self.biz,
                                     self.p1, "Sync Cust")
        self.stranger = make_user("s8stranger")

    def test_business_txn_sync_idempotent_and_member_enforced(self):
        client = auth_client(self.owner)
        op = {"op_id": str(uuid.uuid4()), "entity": "business_transaction",
              "entity_id": str(uuid.uuid4()), "action": "CREATE",
              "client_reference": "biz-sync-1",
              "payload": {"business": str(self.biz),
                          "transaction_type": "CREDIT", "amount": "600.00",
                          "transaction_date": "2026-11-05",
                          "partner": str(self.p1.id),
                          "customer": str(self.customer.id),
                          "description": "Offline credit"}}
        res = client.post("/api/sync/push/", {"device_id": "d", "ops": [op]},
                          format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["summary"]["applied"], 1)
        from apps.customers.models import CustomerAssignment
        due = CustomerAssignment.objects.get(
            customer=self.customer, partner=self.p1).current_due()
        self.assertEqual(due, money("600.00"))

        res = client.post("/api/sync/push/", {"device_id": "d", "ops": [op]},
                          format="json")
        self.assertEqual(res.json()["results"][0]["status"], "applied")
        due = CustomerAssignment.objects.get(
            customer=self.customer, partner=self.p1).current_due()
        self.assertEqual(due, money("600.00"))

        res = auth_client(self.stranger).post("/api/sync/push/", {
            "device_id": "d", "ops": [{
                "op_id": str(uuid.uuid4()), "entity": "business_transaction",
                "entity_id": str(uuid.uuid4()), "action": "CREATE",
                "client_reference": "biz-steal",
                "payload": {"business": str(self.biz),
                            "transaction_type": "EXPENSE", "amount": "1.00",
                            "transaction_date": "2026-11-05"}}]},
            format="json")
        self.assertEqual(res.json()["summary"]["rejected"], 1)
        self.assertIn("not an active member",
                      str(res.json()["results"][0]["error"]).lower())

    def test_money_row_immutable_over_sync(self):
        res = auth_client(self.owner).post("/api/sync/push/", {
            "device_id": "d", "ops": [{
                "op_id": str(uuid.uuid4()), "entity": "business_transaction",
                "entity_id": str(uuid.uuid4()), "action": "UPDATE",
                "payload": {"business": str(self.biz),
                            "amount": "999"}}]},
            format="json")
        self.assertEqual(res.json()["summary"]["rejected"], 1)
