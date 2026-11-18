"""সুপার ক্যালকুলেটর tests — মোট সেল − মোট খরচ = লাভ / লস.

Pinned rules:

- মোট সেল = নগদে সেল (INCOME) + বাকিতে সেল (CREDIT);
- a due collection (PAYMENT) is never counted as sell (no double-count);
- মোট খরচ = EXPENSE only, and profit = মোট সেল − মোট খরচ;
- reversed / non-completed rows are excluded;
- day rows add up to the period totals;
- the page renders with presets and never lists individual entries.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType
from apps.web.supercalc import calc_day_rows, calc_totals, compare_calc

D1 = date(2026, 11, 1)
D2 = date(2026, 11, 2)
D3 = date(2026, 11, 3)


class SuperCalcTests(TestCase):
    def setUp(self):
        self.owner = make_user("scowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Super Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

    # -- helpers ---------------------------------------------------------
    def _txn(self, ttype, amount, day, description="",
             status=TransactionStatus.COMPLETED):
        return Transaction.objects.create(
            business=self.biz, transaction_type=ttype,
            amount=Decimal(amount), transaction_date=day,
            payment_method="CASH", description=description,
            status=status, created_by=self.owner)

    # -- sell definition --------------------------------------------------
    def test_sell_counts_cash_and_due_but_never_a_collection(self):
        self._txn(TransactionType.INCOME, "500.00", D1, "গরুর বিক্রি")
        self._txn(TransactionType.CREDIT, "300.00", D1, "রহিম বাকিতে নিল")
        self._txn(TransactionType.PAYMENT, "200.00", D1, "রহিম ফেরত দিল")

        totals = calc_totals(self.biz, D1, D1)
        self.assertEqual(totals["sell_cash"], Decimal("500.00"))
        self.assertEqual(totals["sell_due"], Decimal("300.00"))
        self.assertEqual(totals["sell_total"], Decimal("800.00"))
        self.assertEqual(totals["collection"], Decimal("200.00"))
        # the collected ৳ belongs to an older sale — never new sell
        self.assertEqual(totals["profit"], Decimal("800.00"))

    def test_profit_is_total_sell_minus_khoroch(self):
        self._txn(TransactionType.INCOME, "1000.00", D1)
        self._txn(TransactionType.CREDIT, "500.00", D1)
        self._txn(TransactionType.EXPENSE, "300.00", D1)
        self._txn(TransactionType.EXPENSE, "200.00", D2)

        totals = calc_totals(self.biz, D1, D2)
        self.assertEqual(totals["sell_total"], Decimal("1500.00"))
        self.assertEqual(totals["khoroch"], Decimal("500.00"))
        self.assertEqual(totals["profit"], Decimal("1000.00"))

    def test_loss_when_expenses_beat_sell(self):
        self._txn(TransactionType.INCOME, "400.00", D1)
        self._txn(TransactionType.EXPENSE, "900.00", D1)
        totals = calc_totals(self.biz, D1, D1)
        self.assertEqual(totals["profit"], Decimal("-500.00"))

    def test_reversed_rows_are_excluded(self):
        self._txn(TransactionType.INCOME, "700.00", D1,
                  status=TransactionStatus.REVERSED)
        self._txn(TransactionType.EXPENSE, "600.00", D1,
                  status=TransactionStatus.REVERSED)
        totals = calc_totals(self.biz, D1, D1)
        self.assertEqual(totals["sell_total"], Decimal("0.00"))
        self.assertEqual(totals["khoroch"], Decimal("0.00"))
        self.assertEqual(totals["profit"], Decimal("0.00"))

    def test_day_rows_add_up_and_running_profit_accumulates(self):
        self._txn(TransactionType.INCOME, "1000.00", D1)
        self._txn(TransactionType.CREDIT, "500.00", D1)
        self._txn(TransactionType.EXPENSE, "300.00", D1)
        self._txn(TransactionType.INCOME, "200.00", D2)
        self._txn(TransactionType.EXPENSE, "800.00", D2)
        self._txn(TransactionType.PAYMENT, "150.00", D3)  # collection only

        rows = calc_day_rows(self.biz, D1, D3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["profit"], Decimal("1200.00"))
        self.assertEqual(rows[1]["profit"], Decimal("-600.00"))
        self.assertEqual(rows[2]["profit"], Decimal("0.00"))
        self.assertEqual(rows[0]["running_profit"], Decimal("1200.00"))
        self.assertEqual(rows[1]["running_profit"], Decimal("600.00"))
        self.assertEqual(rows[2]["running_profit"], Decimal("600.00"))

        totals = calc_totals(self.biz, D1, D3)
        self.assertEqual(sum(r["sell_total"] for r in rows),
                         totals["sell_total"])
        self.assertEqual(sum(r["khoroch"] for r in rows), totals["khoroch"])
        self.assertEqual(sum(r["profit"] for r in rows), totals["profit"])
        self.assertEqual(totals["profit"], Decimal("600.00"))

    def test_compare_calc_uses_the_previous_equal_length_period(self):
        self._txn(TransactionType.INCOME, "1000.00", D1)
        self._txn(TransactionType.EXPENSE, "400.00", D2)
        # previous period spans 2026-10-30 → 2026-10-31 (same 2-day length)
        self._txn(TransactionType.INCOME, "500.00", date(2026, 10, 1))

        data = compare_calc(self.biz, D1, D2)
        self.assertEqual(data["current"]["sell_total"], Decimal("1000.00"))
        self.assertEqual(data["current"]["profit"], Decimal("600.00"))
        self.assertEqual(data["previous"]["sell_total"], Decimal("0.00"))
        self.assertEqual(data["delta"]["profit"], Decimal("600.00"))
        self.assertEqual(data["prev_to"], date(2026, 10, 31))

    # -- the page ---------------------------------------------------------
    def test_page_renders_numbers_and_never_lists_entries(self):
        self._txn(TransactionType.INCOME, "500.00", D1, "গরুর কালা বুট")
        self._txn(TransactionType.CREDIT, "300.00", D1, "রহিম বাকিতে")
        self._txn(TransactionType.EXPENSE, "200.00", D1, "হাড়-হাড়ি ক্রয়")
        self._txn(TransactionType.PAYMENT, "100.00", D2, "রহিম ফেরত")

        html = self.client.get(
            "/supercalculator/?preset=custom&from=2026-11-01&to=2026-11-02"
        ).content.decode("utf-8")
        self.assertIn("সুপার ক্যালকুলেটর", html)
        self.assertIn("মোট সেল", html)
        self.assertIn("লাভ / লস", html)
        self.assertIn("৳800.00", html)      # মোট সেল = 500 + 300
        self.assertIn("৳200.00", html)      # মোট খরচ হলো ৳200
        self.assertIn("+৳600.00", html)     # লাভ = 800 − 200

        # collections stay out of the sell toplines (no double counting)
        self.assertNotIn("৳900.00", html)   # মোট সেল-এ পেমেন্ট যোগবে না
        self.assertNotIn("৳1000.00", html)

        for preset in ("today", "week", "month", "last_month", "year"):
            self.assertEqual(
                self.client.get(f"/supercalculator/?preset={preset}"
                                ).status_code, 200)

        # never lists individual entries
        self.assertNotIn("গরুর কালা বুট", html)
        self.assertNotIn("রহিম বাকিতে", html)
        self.assertNotIn("হাড়-হাড়ি ক্রয়", html)

    def test_clicking_a_day_filters_to_that_day(self):
        self._txn(TransactionType.INCOME, "500.00", D1)
        self._txn(TransactionType.INCOME, "150.00", D2)
        html = self.client.get(
            "/supercalculator/?preset=custom&from=2026-11-02&to=2026-11-02"
        ).content.decode("utf-8")
        self.assertIn("৳150.00", html)
        # the day table itself must not carry D1's sell (the yearly
        # month table below is a different view and may show the month)
        day_table = html.split('<table class="sc-table">')[1]
        self.assertNotIn("৳650.00", day_table)   # D1-এর সেল এসেছে না
