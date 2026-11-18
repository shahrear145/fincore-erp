"""ডাটা রিসেট — the wipe must really wipe, and only the right user's data.

The old reset page silently skipped whole sections (it imported model names
that do not exist, e.g. `customers.DueAllocation`) and swallowed every error,
so memberships disappeared while the business and its records survived,
invisible. These tests pin the new behaviour:

- every record of the signed-in user's businesses is gone afterwards,
- soft-deleted businesses are purged too,
- another user's books are untouched,
- the login account survives and the app still renders,
- running it twice is safe.
"""
from datetime import date, timedelta

from django.test import Client, TestCase
from django.utils import timezone

from apps.assistant.models import AssistantQuery
from apps.core.middleware import AuditLog
from apps.customers.models import (
    Customer,
    CustomerAssignment,
    DueEntry,
    PaymentAllocation,
)
from apps.organizations.models import Business, BusinessMembership, FinancialPeriod
from apps.partners.models import Partner
from apps.personal.models import PersonalAccount, PersonalTransaction
from apps.transactions.models import Transaction
from apps.transactions.tests.test_transactions import _bootstrap, _customer, _txn
from apps.users.models import EmailOTP
from apps.tests_common import auth_client, make_user


class ResetDataTests(TestCase):
    def setUp(self):
        self.owner, self.biz_id, self.p1, self.p2 = _bootstrap()
        self.biz = Business.objects.get(pk=self.biz_id)
        self.customer = _customer(self.owner, self.biz_id, self.p1, "5000.00")

        # a credit line and a collection against it -> PaymentAllocation rows
        self.assertEqual(_txn(self.owner, self.biz_id, self.p1, self.customer,
                              ttype="CREDIT", amount="1000.00",
                              txdate="2026-09-01").status_code, 201)
        self.assertEqual(_txn(self.owner, self.biz_id, self.p1, self.customer,
                              amount="400.00", txdate="2026-09-05").status_code, 201)

        FinancialPeriod.objects.create(
            business=self.biz, name="2026-09", start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30))

        self.personal_account = PersonalAccount.objects.create(
            user=self.owner, name="Wallet")
        PersonalTransaction.objects.create(
            user=self.owner, account=self.personal_account, amount="50.00",
            transaction_type="EXPENSE", transaction_date=date(2026, 9, 2))

        AssistantQuery.objects.create(user=self.owner, question="how much?",
                                      answer="nothing yet")
        EmailOTP.objects.create(
            user=self.owner, code_hash="x" * 16,
            purpose=EmailOTP.Purpose.REGISTER,
            expires_at=timezone.now() + timedelta(minutes=5))

        self.client = Client()
        self.client.force_login(self.owner)
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s.save()

    def _reset(self):
        return self.client.post("/reset-data/", {"confirm": "DELETE"})

    def test_reset_removes_every_record_of_the_business(self):
        self.assertTrue(PaymentAllocation.objects.exists())
        self.assertTrue(DueEntry.objects.exists())
        self.assertTrue(AuditLog.objects.exists())

        res = self._reset()
        self.assertEqual(res.status_code, 302)

        self.assertFalse(Business.all_objects.filter(pk=self.biz.id).exists())
        self.assertFalse(BusinessMembership.objects.exists())
        self.assertFalse(Partner.all_objects.exists())
        self.assertFalse(Customer.all_objects.exists())
        self.assertFalse(CustomerAssignment.objects.exists())
        self.assertFalse(DueEntry.objects.exists())
        self.assertFalse(PaymentAllocation.objects.exists())
        self.assertFalse(Transaction.all_objects.exists())
        self.assertFalse(FinancialPeriod.objects.exists())
        self.assertFalse(PersonalAccount.all_objects.exists())
        self.assertFalse(PersonalTransaction.all_objects.exists())
        self.assertFalse(AssistantQuery.objects.exists())
        self.assertFalse(EmailOTP.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_login_account_survives_and_the_app_still_renders(self):
        self._reset()
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertTrue(self.owner.check_password("S4fePass!2026"))

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        # the session no longer points at a deleted business
        self.assertNotIn("active_business_id", self.client.session)

    def test_reset_purges_soft_deleted_businesses_too(self):
        self.biz.delete()                      # soft delete
        self.assertTrue(Business.all_objects.filter(pk=self.biz.id).exists())

        self._reset()

        self.assertFalse(Business.all_objects.filter(pk=self.biz.id).exists())
        self.assertFalse(Transaction.all_objects.exists())

    def test_reset_never_touches_another_users_books(self):
        other_owner = make_user("otherowner")
        created = auth_client(other_owner).post(
            "/api/businesses/", {"name": "Other Shop"}, format="json")
        other_biz = Business.objects.get(pk=created.json()["id"])
        other_partner = Partner.objects.create(
            business=other_biz, name="Other", status="ACTIVE",
            created_by=other_owner)
        other_customer = _customer(other_owner, other_biz.id, other_partner, "900.00")
        _txn(other_owner, other_biz.id, other_partner, other_customer,
             ttype="CREDIT", amount="300.00", txdate="2026-09-03")

        self._reset()

        self.assertTrue(Business.all_objects.filter(pk=other_biz.id).exists())
        self.assertTrue(Partner.all_objects.filter(pk=other_partner.id).exists())
        self.assertTrue(Customer.all_objects.filter(pk=other_customer.id).exists())
        self.assertTrue(Transaction.all_objects.filter(business=other_biz).exists())
        self.assertTrue(BusinessMembership.objects.filter(
            user=other_owner, business=other_biz).exists())

    def test_second_reset_is_safe(self):
        self._reset()
        res = self._reset()
        self.assertEqual(res.status_code, 302)
        self.assertFalse(Business.all_objects.exists())
        self.assertFalse(Transaction.all_objects.exists())

    def test_reset_page_renders(self):
        res = self.client.get("/reset-data/")
        self.assertEqual(res.status_code, 200)
        self.assertIn('id="wipe-form"', res.content.decode())
