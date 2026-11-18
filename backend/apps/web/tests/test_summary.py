"""হিসাব সারাংশ (Summary) tests — day-wise and month-wise totals.

The summary must never disagree with the individual book pages, so these
tests pin:

- the cash-book totals match the raw INCOME / EXPENSE transactions;
- the due-book totals come from the DueEntry ledger (signed amount);
- a due collection never shows up as cash, and cash never shows as a due;
- day rows and month rows add up to the same period total;
- reversed entries are excluded;
- the period presets work and the page never lists individual entries.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.customers.models import Customer, CustomerAssignment, DueEntry
from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType
from apps.web.summary import (
    compare_periods, day_rows, month_rows, period_totals,
)


class SummaryTests(TestCase):
    def setUp(self):
        self.owner = make_user("sumowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Summary Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)
        self.customer = Customer.objects.create(
            business=self.biz, name="Rahim Uddin", status="ACTIVE")
        self.assignment = CustomerAssignment.objects.create(
            customer=self.customer, partner=self.partner,
            opening_due=Decimal("0.00"))
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

    # -- helpers ---------------------------------------------------------
    def _txn(self, ttype, amount, day, description=""):
        return Transaction.objects.create(
            business=self.biz, transaction_type=ttype,
            amount=Decimal(amount), transaction_date=day,
            payment_method="CASH", description=description,
            created_by=self.owner)

    def _due(self, kind, amount, day, direction=DueEntry.Direction.UP):
        return DueEntry.objects.create(
            assignment=self.assignment, kind=kind, direction=direction,
            amount=Decimal(amount), transaction_date=day)

    # -- tests -----------------------------------------------------------
    def test_cash_totals_match_the_raw_transactions(self):
        self._txn(TransactionType.INCOME, "1000.00", date(2026, 11, 1))
        self._txn(TransactionType.INCOME, "2000.00", date(2026, 11, 2))
        self._txn(TransactionType.EXPENSE, "500.00", date(2026, 11, 2))
        totals = period_totals(self.biz, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(totals["cash_in"], Decimal("3000.00"))
        self.assertEqual(totals["cash_out"], Decimal("500.00"))
        self.assertEqual(totals["net_cash"], Decimal("2500.00"))

    def test_due_book_never_shows_up_as_cash(self):
        # A charge on credit (no cash) and a later collection (cash in the
        # due book, but the app keeps the cash book = INCOME only).
        self._txn(TransactionType.CREDIT, "4000.00", date(2026, 11, 3))
        self._due(DueEntry.Kind.CREDIT, "4000.00", date(2026, 11, 3))
        self._txn(TransactionType.PAYMENT, "1500.00", date(2026, 11, 4))
        self._due(DueEntry.Kind.COLLECTION, "1500.00", date(2026, 11, 4))

        totals = period_totals(self.biz, date(2026, 11, 1), date(2026, 11, 30))
        # Cash book untouched by the due movements.
        self.assertEqual(totals["cash_in"], Decimal("0.00"))
        self.assertEqual(totals["cash_out"], Decimal("0.00"))
        self.assertEqual(totals["net_cash"], Decimal("0.00"))
        # Due book: 4000 added, 1500 collected, 2500 still due.
        self.assertEqual(totals["due_added"], Decimal("4000.00"))
        self.assertEqual(totals["due_collected"], Decimal("1500.00"))
        self.assertEqual(totals["due_net"], Decimal("2500.00"))

    def test_loan_and_investment_moves_cash(self):
        self._txn(TransactionType.BORROW, "8000.00", date(2026, 11, 5))
        self._txn(TransactionType.REPAY, "3000.00", date(2026, 11, 6))
        self._txn(TransactionType.INVEST, "5000.00", date(2026, 11, 7))
        self._txn(TransactionType.WITHDRAW, "1000.00", date(2026, 11, 8))
        totals = period_totals(self.biz, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(totals["loan_in"], Decimal("8000.00"))
        self.assertEqual(totals["loan_out"], Decimal("3000.00"))
        self.assertEqual(totals["invest_in"], Decimal("5000.00"))
        self.assertEqual(totals["invest_out"], Decimal("1000.00"))
        self.assertEqual(totals["cash_money_in"], Decimal("13000.00"))
        self.assertEqual(totals["cash_money_out"], Decimal("4000.00"))
        self.assertEqual(totals["net_cash"], Decimal("9000.00"))

    def test_day_rows_add_up_to_the_period_total(self):
        self._txn(TransactionType.INCOME, "1000.00", date(2026, 11, 1))
        self._txn(TransactionType.INCOME, "2000.00", date(2026, 11, 5))
        self._txn(TransactionType.EXPENSE, "500.00", date(2026, 11, 9))
        totals = period_totals(self.biz, date(2026, 11, 1), date(2026, 11, 30))
        rows = day_rows(self.biz, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(len(rows), 30)
        self.assertEqual(sum((r["net_cash"] for r in rows), Decimal("0.00")),
                         totals["net_cash"])
        self.assertEqual(rows[-1]["running_cash"], totals["net_cash"])
        # A day without movement is still listed, with zeros.
        quiet = next(r for r in rows if r["date"] == date(2026, 11, 2))
        self.assertFalse(quiet["has_data"])
        self.assertEqual(quiet["net_cash"], Decimal("0.00"))

    def test_month_rows_add_up_to_the_year_total(self):
        self._txn(TransactionType.INCOME, "1000.00", date(2026, 11, 1))
        self._txn(TransactionType.INCOME, "2000.00", date(2026, 12, 5))
        months = month_rows(self.biz, 2026)
        self.assertEqual(len(months), 12)
        november = next(m for m in months if m["month"] == 11)
        december = next(m for m in months if m["month"] == 12)
        self.assertEqual(november["net_cash"], Decimal("1000.00"))
        self.assertEqual(december["net_cash"], Decimal("2000.00"))
        self.assertEqual(sum((m["net_cash"] for m in months), Decimal("0.00")),
                         Decimal("3000.00"))

    def test_reversed_entries_are_excluded(self):
        txn = self._txn(TransactionType.INCOME, "1000.00", date(2026, 11, 1))
        txn.status = TransactionStatus.REVERSED
        txn.save(update_fields=["status"])
        totals = period_totals(self.biz, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(totals["cash_in"], Decimal("0.00"))

    def test_comparison_uses_the_previous_period_of_equal_length(self):
        self._txn(TransactionType.INCOME, "1000.00", date(2026, 10, 5))
        self._txn(TransactionType.INCOME, "3000.00", date(2026, 11, 5))
        result = compare_periods(self.biz, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(result["previous"]["cash_in"], Decimal("1000.00"))
        self.assertEqual(result["current"]["cash_in"], Decimal("3000.00"))
        self.assertEqual(result["delta"]["cash_in"], Decimal("2000.00"))

    def test_page_renders_with_presets_and_no_entry_list(self):
        self._txn(TransactionType.INCOME, "1000.00", date(2026, 11, 10),
                  "নগদ বিক্রয়")
        html = self.client.get("/summary/").content.decode("utf-8")
        self.assertIn("হিসাব সারাংশ", html)
        self.assertIn("নগদের চলাচল", html)
        for preset in ("আজ", "এই সপ্তাহ", "এই মাস", "গত মাস", "এই বছর"):
            self.assertIn(preset, html)
        self.assertIn("দিনভিত্তিক নগদের চলাচল", html)
        self.assertNotIn("দিনভিত্তিক মোট হিসাব", html)
        self.assertIn("মাসভিত্তিক হিসাব", html)

        for preset in ("today", "week", "month", "last_month", "year"):
            self.assertEqual(
                self.client.get(f"/summary/?preset={preset}").status_code, 200)

        # The summary never lists individual entries — only totals.
        self.assertNotIn("নগদ বিক্রয়", html)
        self.assertNotIn("এই দিনের এন্ট্রি", html)
        self.assertNotIn("drill", html)

    def test_clicking_a_day_filters_to_that_day(self):
        self._txn(TransactionType.INCOME, "1000.00", date(2026, 11, 10),
                  "নগদ বিক্রয়")
        html = self.client.get(
            "/summary/?preset=custom&from=2026-11-10&to=2026-11-10"
        ).content.decode("utf-8")
        self.assertIn("৳1,000.00", html)
        # Still no individual entries on the summary page.
        self.assertNotIn("নগদ বিক্রয়", html)
