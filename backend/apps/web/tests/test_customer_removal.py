"""Deleting a name from a partner's khata must also remove the name itself.

Pinned rules:

- "খাতা থেকে সরান" reverses the partner's history, closes that partner's
  book, and — when it was the customer's last open book and nothing is owed
  anywhere — soft-deletes the customer row so the name stops appearing in
  searches, exports and the audit;
- a customer shared with another partner stays alive (only that partner's
  book closes);
- a name with money still due is kept (never hide a due);
- the one-off cleanup command removes old leftovers, but only in --apply
  mode and only when no book is open, nothing is due and no live
  transaction points at the customer.
"""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase

from apps.customers.models import Customer, CustomerAssignment
from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import (
    Transaction, TransactionStatus, TransactionType,
)


class CustomerRemovalTests(TestCase):
    def setUp(self):
        self.owner = make_user("removal")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Removal Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner)
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()

    def _customer(self, name):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(self.partner.id)},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return Customer.objects.get(pk=res.json()["id"])

    def _txn(self, ttype, amount, customer):
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}",
            {"transaction_type": ttype, "amount": amount,
             "transaction_date": "2026-11-10", "payment_method": "CASH",
             "partner": str(self.partner.id),
             "customer": str(customer.id)},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["id"]

    def _remove(self, customer, partner=None):
        partner = partner or self.partner
        return self.client.post("/customers/", {
            "action": "delete_name",
            "partner_id": str(partner.id),
            "customer_id": str(customer.id),
        })

    # -- the web action ---------------------------------------------------
    def test_removing_the_last_khata_removes_the_name(self):
        customer = self._customer("Rakib")
        self._txn(TransactionType.CREDIT, "4000.00", customer)
        res = self._remove(customer)
        self.assertEqual(res.status_code, 302)

        self.assertFalse(Customer.objects.filter(pk=customer.pk).exists())
        archived = Customer.all_objects.get(pk=customer.pk)
        self.assertIsNotNone(archived.deleted_at)
        assignment = CustomerAssignment.objects.get(customer=archived)
        self.assertFalse(assignment.is_active)
        self.assertEqual(
            list(Transaction.objects.filter(customer=archived)
                 .values_list("status", flat=True)),
            [TransactionStatus.REVERSED])

        from apps.web.position_statement import build_statement
        statement = build_statement(self.biz)
        keys = {f["key"] for f in statement["audit"]["findings"]}
        self.assertNotIn("no_partner", keys)

    def test_shared_customer_stays_alive(self):
        customer = self._customer("Shared")
        self._txn(TransactionType.CREDIT, "1000.00", customer)
        other = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE")
        CustomerAssignment.objects.create(customer=customer, partner=other)

        res = self._remove(customer)
        self.assertEqual(res.status_code, 302)
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())
        self.assertFalse(CustomerAssignment.objects.get(
            customer=customer, partner=self.partner).is_active)
        self.assertTrue(CustomerAssignment.objects.get(
            customer=customer, partner=other).is_active)

    def test_a_name_that_still_owes_money_is_kept(self):
        customer = self._customer("Owed")
        assignment = CustomerAssignment.objects.get(
            customer=customer, partner=self.partner)
        assignment.opening_due = Decimal("500.00")
        assignment.save(update_fields=["opening_due"])

        res = self._remove(customer)
        self.assertEqual(res.status_code, 302)
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())
        self.assertFalse(CustomerAssignment.objects.get(
            customer=customer, partner=self.partner).is_active)
        page = self.client.get(
            f"/customers/?partner={self.partner.id}").content.decode("utf-8")
        self.assertIn("মুছিনি", page)

    # -- the one-off cleanup command -------------------------------------
    def _leftover(self, name):
        customer = self._customer(name)
        assignment = CustomerAssignment.objects.get(
            customer=customer, partner=self.partner)
        assignment.is_active = False
        assignment.save(update_fields=["is_active"])
        return customer

    def test_cleanup_command_removes_old_leftovers(self):
        leftover = self._leftover("OldName")
        out = StringIO()
        call_command("cleanup_removed_customers", "--apply", stdout=out)

        self.assertFalse(Customer.objects.filter(pk=leftover.pk).exists())
        self.assertIsNotNone(
            Customer.all_objects.get(pk=leftover.pk).deleted_at)
        self.assertIn("deleted: 1", out.getvalue())

    def test_cleanup_command_is_a_dry_run_and_skips_money(self):
        leftover = self._leftover("OldName")
        owed = self._leftover("OwedName")
        assignment = CustomerAssignment.objects.get(
            customer=owed, partner=self.partner)
        assignment.opening_due = Decimal("250.00")
        assignment.save(update_fields=["opening_due"])

        out = StringIO()
        call_command("cleanup_removed_customers", stdout=out)
        text = out.getvalue()
        self.assertTrue(Customer.objects.filter(pk=leftover.pk).exists())
        self.assertTrue(Customer.objects.filter(pk=owed.pk).exists())
        self.assertIn("Dry run only", text)
        self.assertIn("still due: 1", text)

        call_command("cleanup_removed_customers", "--apply", stdout=StringIO())
        self.assertFalse(Customer.objects.filter(pk=leftover.pk).exists())
        self.assertTrue(Customer.objects.filter(pk=owed.pk).exists())
