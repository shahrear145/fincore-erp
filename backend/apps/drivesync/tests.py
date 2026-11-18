"""Restore tests for the Google Drive backup feature.

The bugs these lock down:

* a row the user deleted from the UI is only *soft*-deleted, and the restore
  used to look it up through ``objects`` (alive only) — so it hit a duplicate
  primary key, skipped the row, and reported "0 rows restored";
* a soft-deleted *business* was missing from the snapshot entirely, so none
  of its rows could be put back;
* the overwrite path called ``business.hard_delete()`` and blew up with
  ``ProtectedError`` whenever the business had loans/partners (PROTECT FKs).
"""
import uuid
from decimal import Decimal

from django.test import TestCase

from apps.accounting.postings import ensure_system_accounts
from apps.core.reset import wipe_user_data
from apps.customers.models import Customer
from apps.drivesync import service
from apps.loans.models import LoanEntry, LoanParty
from apps.organizations.models import Business, BusinessMembership
from apps.personal.models import (
    PersonalAccount,
    PersonalCategory,
    PersonalTransaction,
)
from apps.tests_common import make_user


class DriveRestoreTests(TestCase):
    def setUp(self):
        self.owner = make_user("drivetest")
        self.biz = Business.objects.create(
            owner=self.owner, name="Drive Biz", slug="drive-biz",
            currency="BDT", timezone="Asia/Dhaka")
        BusinessMembership.objects.create(
            business=self.biz, user=self.owner, role="ADMIN", is_active=True)
        ensure_system_accounts(self.biz, self.owner)

    def _customer(self, name):
        return Customer.objects.create(business=self.biz, name=name)

    # -- soft-deleted rows come back ---------------------------------------
    def test_soft_deleted_customer_is_restored_in_shared_business(self):
        partner = make_user("drivepartner")
        BusinessMembership.objects.create(
            business=self.biz, user=partner, role="PARTNER", is_active=True)
        self._customer("Customer A")
        self._customer("Customer B")

        snapshot = service.build_snapshot(self.owner)
        self.assertFalse(snapshot["businesses"][0]["sole_owner"])

        Customer.objects.filter(business=self.biz, name="Customer B").delete()
        self.assertEqual(Customer.objects.filter(business=self.biz).count(), 1)

        report = service.restore_snapshot(self.owner, snapshot)

        self.assertEqual(report["businesses"][0]["failed"], 0)
        self.assertEqual(
            sorted(Customer.objects.filter(business=self.biz)
                   .values_list("name", flat=True)),
            ["Customer A", "Customer B"])

    # -- soft-deleted business + its loans --------------------------------
    def test_soft_deleted_business_with_loans_is_restored(self):
        self._customer("Customer C")
        party = LoanParty.objects.create(
            business=self.biz, name="Lender", kind=LoanParty.Kind.LOAN)
        LoanEntry.objects.create(
            business=self.biz, party=party, kind=LoanEntry.Kind.RECEIVED,
            amount=Decimal("190000"), contract_total=Decimal("210000"),
            transaction_date="2026-11-01", status=LoanEntry.Status.POSTED)

        snapshot = service.build_snapshot(self.owner)
        # the business itself must be in the snapshot even once soft-deleted
        self.assertTrue(snapshot["businesses"][0]["rows"]["businesses"])

        self.biz.delete()   # soft delete
        self.assertFalse(Business.objects.filter(pk=self.biz.pk).exists())

        report = service.restore_snapshot(self.owner, snapshot)

        self.assertTrue(report["businesses"][0]["overwrite"])
        self.assertEqual(report["businesses"][0]["failed"], 0)
        business = Business.objects.filter(pk=self.biz.pk).first()
        self.assertIsNotNone(business)
        self.assertEqual(Customer.objects.filter(business=business).count(), 1)
        entry = LoanEntry.objects.filter(business=business).first()
        self.assertEqual(entry.amount, Decimal("190000.00"))
        self.assertEqual(entry.contract_total, Decimal("210000.00"))

    # -- reset then restore ------------------------------------------------
    def test_restore_after_full_reset(self):
        self._customer("Customer D")
        category = PersonalCategory.objects.create(user=self.owner, name="Food")
        account = PersonalAccount.objects.create(user=self.owner, name="Wallet")
        PersonalTransaction.objects.create(
            user=self.owner, account=account, category=category,
            amount=Decimal("100"), transaction_type="EXPENSE",
            transaction_date="2026-11-01")

        snapshot = service.build_snapshot(self.owner)
        wipe_user_data(self.owner)
        self.assertFalse(Business.objects.filter(pk=self.biz.pk).exists())

        report = service.restore_snapshot(self.owner, snapshot)

        self.assertEqual(report["businesses"][0]["failed"], 0)
        business = Business.objects.filter(pk=self.biz.pk).first()
        self.assertIsNotNone(business)
        self.assertEqual(Customer.objects.filter(business=business).count(), 1)
        self.assertEqual(
            PersonalTransaction.objects.filter(user=self.owner).count(), 1)

    # -- overwrite path keeps data exact ----------------------------------
    def test_overwrite_restore_drops_rows_added_after_backup(self):
        self._customer("Customer A")
        snapshot = service.build_snapshot(self.owner)
        self._customer("Customer B (extra)")

        report = service.restore_snapshot(self.owner, snapshot)

        self.assertTrue(report["businesses"][0]["overwrite"])
        self.assertEqual(
            list(Customer.objects.filter(business=self.biz)
                 .values_list("name", flat=True)),
            ["Customer A"])

    # -- snapshots made before the fix ------------------------------------
    def test_old_snapshot_without_business_row_after_reset(self):
        self._customer("Customer E")
        snapshot = service.build_snapshot(self.owner)
        snapshot["businesses"][0]["rows"]["businesses"] = []   # old snapshot

        wipe_user_data(self.owner)
        report = service.restore_snapshot(self.owner, snapshot)

        self.assertEqual(report["businesses"][0]["failed"], 0)
        business = Business.objects.filter(pk=self.biz.pk).first()
        self.assertIsNotNone(business)
        self.assertEqual(Customer.objects.filter(business=business).count(), 1)

    def test_old_snapshot_without_business_row_undeletes_business(self):
        self._customer("Customer F")
        snapshot = service.build_snapshot(self.owner)
        snapshot["businesses"][0]["rows"]["businesses"] = []

        self.biz.delete()   # soft delete
        report = service.restore_snapshot(self.owner, snapshot)

        self.assertEqual(report["businesses"][0]["failed"], 0)
        business = Business.objects.filter(pk=self.biz.pk).first()
        self.assertIsNotNone(business)
        self.assertEqual(Customer.objects.filter(business=business).count(), 1)

    # -- journal lines must roundtrip through backup/restore ---------------
    def test_journal_lines_survive_backup_and_restore(self):
        """The live failure (2026-11-15): the snapshot silently dropped every
        JournalLine row (business_qs returned .none() for line-only models),
        so after a restore the ledger was empty and হাতে নগদ read ৳0 while
        the books showed money. The whole double-entry chain must roundtrip.
        """
        from apps.transactions.models import Transaction, TransactionStatus
        from apps.transactions.services import create_transaction
        from apps.accounting.models import JournalEntry, JournalLine

        class FakeRequest:
            pass

        req = FakeRequest()
        req.user = self.owner
        req.business_membership = BusinessMembership.objects.get(
            business=self.biz, user=self.owner)

        tx, _created = create_transaction(
            req, business=self.biz, amount=Decimal("10000"),
            transaction_type="INCOME",
            transaction_date="2026-11-10",
            payment_method="CASH",
            description="Export test income")
        self.assertEqual(JournalLine.objects.filter(entry__business=self.biz).count(), 2)

        snapshot = service.build_snapshot(self.owner)
        self.assertTrue(snapshot["businesses"][0]["rows"]["journal_lines"],
                        "journal lines must be in the snapshot")
        self.assertTrue(snapshot["businesses"][0]["rows"]["journal_entries"],
                        "journal entries must be in the snapshot")

        wipe_user_data(self.owner)
        report = service.restore_snapshot(self.owner, snapshot)

        self.assertEqual(report["businesses"][0]["failed"], 0)
        business = Business.objects.filter(pk=self.biz.pk).first()
        self.assertIsNotNone(business)
        self.assertEqual(JournalEntry.objects.filter(business=business).count(), 1)
        self.assertEqual(JournalLine.objects.filter(entry__business=business).count(), 2)
        from apps.cashbook.services import business_position
        self.assertEqual(business_position(business)["cash"], Decimal("10000.00"))

    # -- unposted transactions are picked up by the rebuild ----------------
    def test_unposted_transactions_are_rebuilt(self):
        from apps.accounting.postings import ensure_system_accounts
        from apps.cashbook.services import (
            rebuild_ledger_from_transactions,
            unposted_transactions,
        )
        from apps.transactions.models import Transaction

        ensure_system_accounts(self.biz, self.owner)   # fresh business has no journals yet
        Transaction.objects.create(
            business=self.biz, transaction_type="BORROW",
            amount=Decimal("50000"), transaction_date="2026-11-10",
            status="COMPLETED", payment_method="CASH")
        self.assertEqual(len(unposted_transactions(self.biz)), 1)

        fixed = rebuild_ledger_from_transactions(self.biz, user=self.owner)
        self.assertEqual(fixed, 1)
        self.assertEqual(len(unposted_transactions(self.biz)), 0)

    # -- the live failure: soft-deleted PersonalAccount --------------------
    def test_restore_with_soft_deleted_personal_account(self):
        """The bug seen on the live site (2026-11-15).

        The personal reset used ``qs.delete()`` — a *soft* delete for
        PersonalAccount. The row stayed in the table, re-inserting it broke
        UNIQUE (user, name), and that IntegrityError poisoned the atomic
        block so every one of the 75 business rows failed too.
        """
        account = PersonalAccount.objects.create(user=self.owner, name="Wallet")
        category = PersonalCategory.objects.create(user=self.owner, name="Food")
        txn = PersonalTransaction.objects.create(
            user=self.owner, account=account, category=category,
            amount=Decimal("100"), transaction_type="EXPENSE",
            transaction_date="2026-11-01")

        snapshot = service.build_snapshot(self.owner)
        account.delete()   # soft delete — leaves the row in the table
        txn.delete()

        report = service.restore_snapshot(self.owner, snapshot)

        self.assertEqual(report["personal_failed"], 0)
        self.assertEqual(report["personal_created"], 3)
        self.assertTrue(PersonalAccount.objects.filter(
            user=self.owner, name="Wallet").exists())
        self.assertEqual(
            PersonalTransaction.objects.filter(user=self.owner).count(), 1)
        # and the business data must not be poisoned either
        self.assertEqual(report["businesses"][0]["failed"], 0)

    # -- one bad row must not poison the whole restore ---------------------
    def test_one_bad_row_does_not_break_the_rest(self):
        self._customer("Customer G")
        account = PersonalAccount.objects.create(user=self.owner, name="Cash")
        PersonalTransaction.objects.create(
            user=self.owner, account=account, amount=Decimal("50"),
            transaction_type="INCOME", transaction_date="2026-11-01")

        snapshot = service.build_snapshot(self.owner)
        rows = snapshot["personal"]["personal_transactions"]
        self.assertTrue(rows)
        bad = dict(rows[0])
        bad["id"] = str(uuid.uuid4())
        bad["amount"] = "0"   # violates the amount > 0 CHECK constraint
        rows.append(bad)

        report = service.restore_snapshot(self.owner, snapshot)

        self.assertEqual(report["personal_failed"], 1)
        self.assertEqual(report["personal_created"], 2)
        # without the savepoint fix these would all fail on the poisoned
        # transaction instead of restoring
        self.assertEqual(report["businesses"][0]["failed"], 0)
        self.assertEqual(Customer.objects.filter(business=self.biz).count(), 1)
