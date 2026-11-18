"""সুপার ক্যালকুলেটর — মাসভিত্তিক লাভের টেবিল ও বছর সিলেক্টর.

Pinned rules:

- the month rows are the same engine as the day rows, so a month always
  adds up to the page totals;
- every month of the chosen year is listed (empty months show zero);
- the year can be picked, and the totals follow the picked year;
- clicking a month filters the period to that month;
- a due collection is still context only — never counted as sell.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType
from apps.web.supercalc import calc_month_rows, calc_totals

NOV1 = date(2026, 11, 1)
NOV2 = date(2026, 11, 20)
DEC1 = date(2026, 12, 3)


class SuperCalcMonthTests(TestCase):
    def setUp(self):
        self.owner = make_user("scmonth")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Month Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()

    def _txn(self, ttype, amount, day):
        return Transaction.objects.create(
            business=self.biz, transaction_type=ttype,
            amount=Decimal(amount), transaction_date=day,
            payment_method="CASH", created_by=self.owner)

    # -- the month rows ---------------------------------------------------
    def test_every_month_of_the_year_is_listed(self):
        months = calc_month_rows(self.biz, 2026)
        self.assertEqual(len(months), 12)
        self.assertEqual(months[0]["month"], 1)
        self.assertEqual(months[-1]["month"], 12)
        self.assertEqual(months[0]["first_day"], date(2026, 1, 1))
        self.assertEqual(months[10]["first_day"], date(2026, 11, 1))
        self.assertEqual(months[10]["last_day"], date(2026, 11, 30))

    def test_a_month_totals_its_own_sells_costs_and_profit(self):
        self._txn(TransactionType.INCOME, "5000.00", NOV1)
        self._txn(TransactionType.CREDIT, "1000.00", NOV1)
        self._txn(TransactionType.EXPENSE, "2000.00", NOV2)
        self._txn(TransactionType.PAYMENT, "300.00", NOV2)   # collection only

        months = calc_month_rows(self.biz, 2026)
        nov = months[10]
        self.assertEqual(nov["sell_cash"], Decimal("5000.00"))
        self.assertEqual(nov["sell_due"], Decimal("1000.00"))
        self.assertEqual(nov["sell_total"], Decimal("6000.00"))
        self.assertEqual(nov["khoroch"], Decimal("2000.00"))
        self.assertEqual(nov["collection"], Decimal("300.00"))
        # the collection is never a new sale
        self.assertEqual(nov["profit"], Decimal("4000.00"))
        self.assertTrue(nov["has_data"])

        december = months[11]
        self.assertEqual(december["sell_total"], Decimal("0.00"))
        self.assertEqual(december["profit"], Decimal("0.00"))
        self.assertFalse(december["has_data"])

    def test_a_month_matches_the_period_totals(self):
        self._txn(TransactionType.INCOME, "5000.00", NOV1)
        self._txn(TransactionType.EXPENSE, "2000.00", NOV2)
        months = calc_month_rows(self.biz, 2026)
        nov = months[10]
        totals = calc_totals(self.biz, nov["first_day"], nov["last_day"])
        self.assertEqual(nov["sell_total"], totals["sell_total"])
        self.assertEqual(nov["khoroch"], totals["khoroch"])
        self.assertEqual(nov["profit"], totals["profit"])

    # -- the page ---------------------------------------------------------
    def test_page_shows_the_month_table_and_a_year_picker(self):
        self._txn(TransactionType.INCOME, "5000.00", NOV1)
        html = self.client.get("/supercalculator/").content.decode("utf-8")
        self.assertIn("মাসভিত্তিক লাভ / লস", html)
        self.assertIn("লাভ / লস", html)
        self.assertIn('name="year"', html)
        self.assertIn('value="2026" selected', html)
        # twelve month rows
        self.assertIn("Nov 2026", html)
        self.assertIn("Dec 2026", html)
        self.assertIn("2026 মোট", html)

    def test_the_year_can_be_picked(self):
        self._txn(TransactionType.INCOME, "5000.00", NOV1)
        html = self.client.get("/supercalculator/?year=2025").content.decode()
        self.assertIn('value="2025" selected', html)
        self.assertIn("2025 মোট", html)

    def test_a_broken_year_falls_back_to_the_current_one(self):
        html = self.client.get("/supercalculator/?year=abc").content.decode()
        self.assertIn("মাসভিত্তিক লাভ / লস", html)

    def test_clicking_a_month_filters_to_that_month(self):
        self._txn(TransactionType.INCOME, "5000.00", NOV1)
        html = self.client.get("/supercalculator/").content.decode()
        self.assertIn("from=2026-11-01", html)
        self.assertIn("to=2026-11-30", html)
        self.assertIn("year=2026", html)
