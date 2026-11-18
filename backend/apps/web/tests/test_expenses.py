"""Web UI tests for the খরচ / ক্রয় (expenses) page.

The supply-chain modules (sales / purchases / inventory / suppliers) were
removed, so the only remaining "purchase" surface is this small expense book:
what was bought, when, how much. These tests pin that contract:

- an expense entry is an EXPENSE cash transaction for the active business;
- it never touches the customer due book (বাকি) or the cash-in book (নগদ);
- edit and delete are ledger-safe (reverse + repost / reverse);
- only ADMINs may edit or delete;
- the removed pages are gone (404).
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.organizations.models import Business, BusinessMembership
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType


class ExpenseBookTests(TestCase):
    def setUp(self):
        self.owner = make_user("wexpowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Expense Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.membership = BusinessMembership.objects.filter(
            business=self.biz, user=self.owner).first()
        self.partner_user = make_user("wexppartner")
        self.partner_membership = BusinessMembership.objects.create(
            business=self.biz, user=self.partner_user, role="PARTNER")
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

    # -- helpers ---------------------------------------------------------
    def _add(self, name="চাল ৫ কেজি", amount="750.00",
             tx_date="2026-11-10", description="দোকান থেকে"):
        return self.client.post("/expenses/", {
            "action": "add_expense",
            "name": name,
            "amount": amount,
            "transaction_date": tx_date,
            "description": description,
        })

    def _expenses(self):
        return Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.EXPENSE)

    # -- tests -----------------------------------------------------------
    def test_expense_entry_is_created_as_expense_cash(self):
        response = self._add()
        self.assertEqual(response.status_code, 302)
        txn = self._expenses().get()
        self.assertEqual(txn.amount, Decimal("750.00"))
        self.assertEqual(txn.description, "চাল ৫ কেজি")
        self.assertEqual(txn.payment_method, "CASH")
        self.assertEqual(txn.status, TransactionStatus.COMPLETED)
        self.assertEqual(txn.transaction_date, date(2026, 11, 10))

    def test_expense_does_not_touch_due_or_cash_books(self):
        self._add()
        # The cash page (নগদ) lists INCOME only, so an expense never shows.
        html = self.client.get("/transactions/").content.decode("utf-8")
        self.assertNotIn("চাল ৫ কেজি", html)
        # The due book (বাকি) is untouched.
        self.assertFalse(Transaction.objects.filter(
            business=self.biz,
            transaction_type__in=[TransactionType.CREDIT,
                                  TransactionType.PAYMENT]).exists())
        # The expense book lists it.
        html = self.client.get("/expenses/").content.decode("utf-8")
        self.assertIn("চাল ৫ কেজি", html)
        self.assertIn("৳750.00", html)

    def test_edit_reverses_and_reposts(self):
        self._add()
        original = self._expenses().get()
        response = self.client.post("/expenses/", {
            "action": "edit_expense",
            "tx_id": str(original.id),
            "amount": "800.00",
            "transaction_date": "2026-11-11",
            "name": "চাল ৫ কেজি (সংশোধিত)",
            "description": "",
            "reason": "wrong amount",
        })
        self.assertEqual(response.status_code, 302)
        original.refresh_from_db()
        self.assertEqual(original.status, TransactionStatus.REVERSED)
        new = self._expenses().exclude(status=TransactionStatus.REVERSED).get()
        self.assertEqual(new.amount, Decimal("800.00"))
        self.assertEqual(new.transaction_date, date(2026, 11, 11))
        self.assertIn("সংশোধিত", new.description)

    def test_delete_reverses_and_hides_entry(self):
        self._add()
        txn = self._expenses().get()
        response = self.client.post("/expenses/", {
            "action": "delete_expense",
            "tx_id": str(txn.id),
            "reason": "cleanup",
        })
        self.assertEqual(response.status_code, 302)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.REVERSED)
        html = self.client.get("/expenses/").content.decode("utf-8")
        self.assertNotIn("চাল ৫ কেজি", html)

    def test_partner_cannot_edit_or_delete(self):
        self._add()
        txn = self._expenses().get()
        self.client.force_login(self.partner_user)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()
        response = self.client.post("/expenses/", {
            "action": "delete_expense",
            "tx_id": str(txn.id),
            "reason": "nope",
        })
        self.assertEqual(response.status_code, 302)
        txn.refresh_from_db()
        self.assertEqual(txn.status, TransactionStatus.COMPLETED)

    def test_removed_supply_chain_pages_are_gone(self):
        for url in ("/sales/", "/purchases/", "/inventory/", "/suppliers/"):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_nav_shows_expenses_and_not_removed_modules(self):
        html = self.client.get("/expenses/").content.decode("utf-8")
        self.assertIn("খরচ / ক্রয়", html)
        self.assertNotIn("/sales/", html)
        self.assertNotIn("/inventory/", html)
        self.assertNotIn("/suppliers/", html)

    # -- date-wise filter -------------------------------------------------
    def _seed_three(self):
        self._add(name="নভে এক", amount="100.00", tx_date="2026-11-01")
        self._add(name="নভে পাঁচ", amount="200.00", tx_date="2026-11-05")
        self._add(name="অক্টো", amount="300.00", tx_date="2026-10-20")

    def test_date_range_filter_narrows_rows_and_total(self):
        self._seed_three()
        html = self.client.get(
            "/expenses/?from=2026-11-01&to=2026-11-30").content.decode("utf-8")
        self.assertIn("নভে এক", html)
        self.assertIn("নভে পাঁচ", html)
        self.assertNotIn("অক্টো", html)
        # filtered total 300, all-time total 600
        self.assertIn("৳300.00", html)
        self.assertIn("৳600.00", html)
        self.assertIn("ফিল্টার মোট", html)

    def test_single_day_filter(self):
        self._seed_three()
        html = self.client.get(
            "/expenses/?from=2026-11-05&to=2026-11-05").content.decode("utf-8")
        self.assertIn("নভে পাঁচ", html)
        self.assertNotIn("নভে এক", html)
        self.assertNotIn("অক্টো", html)

    def test_filter_range_is_auto_swapped_and_bad_date_ignored(self):
        self._seed_three()
        swapped = self.client.get(
            "/expenses/?from=2026-11-30&to=2026-11-01").content.decode("utf-8")
        self.assertIn("নভে এক", swapped)
        self.assertNotIn("অক্টো", swapped)
        broken = self.client.get(
            "/expenses/?from=not-a-date").content.decode("utf-8")
        self.assertIn("অক্টো", broken)
        self.assertIn("নভে এক", broken)

    def test_filter_bar_and_chips_are_rendered(self):
        html = self.client.get("/expenses/").content.decode("utf-8")
        self.assertIn('name="from"', html)
        self.assertIn('name="to"', html)
        self.assertIn("খুঁজুন", html)
        self.assertIn("এই মাস", html)
        self.assertIn("সব", html)
