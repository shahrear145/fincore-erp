"""নগদ (cash) web page tests.

The cash book shows only money RECEIVED in cash (INCOME). These tests pin:

- a From–To date filter narrows the history list and the range total;
- the all-time and month totals stay visible next to the filtered ones;
- the cash book never mixes in du�� book, expenses or loans;
- reversed entries never appear.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType


class CashBookTests(TestCase):
    def setUp(self):
        self.owner = make_user("cashbookowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Cash Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

    def _add(self, name, amount, tx_date):
        self.client.post("/transactions/", {
            "amount": amount, "transaction_date": tx_date, "name": name})
        return Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.INCOME,
            description=name).first()

    def test_cash_entries_are_listed_with_a_total(self):
        self._add("নভে এক", "1000.00", "2026-11-01")
        self._add("নভে পাঁচ", "2000.00", "2026-11-05")
        html = self.client.get("/transactions/").content.decode("utf-8")
        self.assertIn("নভে এক", html)
        self.assertIn("নভে পাঁচ", html)
        self.assertIn("৳3,000.00", html)

    def test_date_range_filter_narrows_rows_and_total(self):
        self._add("নভে এক", "1000.00", "2026-11-01")
        self._add("নভে পাঁচ", "2000.00", "2026-11-05")
        self._add("অক্টো", "3000.00", "2026-10-20")

        html = self.client.get(
            "/transactions/?from=2026-11-01&to=2026-11-30").content.decode(
                "utf-8")
        self.assertIn("নভে এক", html)
        self.assertIn("নভে পাঁচ", html)
        self.assertNotIn(">অক্টো<", html)
        # filtered range total = 3000, all-time total = 6000
        self.assertIn("৳3,000.00", html)
        self.assertIn("৳6,000.00", html)
        self.assertIn("এই রেঞ্জে নগদ", html)

    def test_single_day_filter(self):
        self._add("নভে এক", "1000.00", "2026-11-01")
        self._add("নভে পাঁচ", "2000.00", "2026-11-05")
        html = self.client.get(
            "/transactions/?from=2026-11-05&to=2026-11-05").content.decode(
                "utf-8")
        self.assertIn("নভে পাঁচ", html)
        self.assertNotIn(">নভে এক<", html)
        self.assertIn("৳2,000.00", html)

    def test_filter_range_is_auto_swapped_and_bad_date_ignored(self):
        self._add("নভে এক", "1000.00", "2026-11-01")
        self._add("অক্টো", "3000.00", "2026-10-20")
        swapped = self.client.get(
            "/transactions/?from=2026-11-30&to=2026-11-01").content.decode(
                "utf-8")
        self.assertIn("নভে এক", swapped)
        self.assertNotIn(">অক্টো<", swapped)
        broken = self.client.get(
            "/transactions/?from=not-a-date").content.decode("utf-8")
        self.assertIn("নভে এক", broken)
        self.assertIn("অক্টো", broken)

    def test_cash_book_never_shows_due_expense_or_loan_rows(self):
        self._add("নগদ এন্ট্রি", "1000.00", "2026-11-01")
        # A due, an expense and a loan must not appear in the cash book.
        for txn_type in (TransactionType.CREDIT, TransactionType.EXPENSE,
                         TransactionType.BORROW):
            Transaction.objects.create(
                business=self.biz, transaction_type=txn_type,
                amount=Decimal("9999.00"), transaction_date=date(2026, 11, 1),
                payment_method="CASH", description="অন্য বইয়ের এন্ট্রি")
        html = self.client.get("/transactions/").content.decode("utf-8")
        self.assertNotIn("অন্য বইয়ের এন্ট্রি", html)
        self.assertNotIn("৳9,999.00", html)

    def test_reversed_entries_are_hidden(self):
        txn = self._add("বাতিল এন্ট্রি", "500.00", "2026-11-02")
        txn.status = TransactionStatus.REVERSED
        txn.save(update_fields=["status"])
        html = self.client.get("/transactions/").content.decode("utf-8")
        self.assertNotIn("বাতিল এন্ট্রি", html)
