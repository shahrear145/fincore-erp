"""Cash-book tests — the real money position, the drawer count, real profit.

Pinned rules:

- the cash in the drawer is the ledger's own cash balance (not a guess),
  and it starts from the real opening balance — never from zero;
- bKash/Nagad/bank money is wallet money, never cash in the drawer;
- a baki collected in cash IS in the drawer (the money really is there);
- the drawer's physical count is compared with the book, and the
  difference (গরমিল / ঘাটতি) is stored with the count;
- unsold stock is not a loss: profit = sales − (goods − stock increase);
- the ledger check finds a posting that belongs to no transaction and can
  balance it without deleting anything.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.cashbook import services as cashbook
from apps.cashbook.models import CashCount, StockSnapshot
from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import ExpenseKind, Transaction, TransactionStatus

D1 = date(2026, 11, 9)
D2 = date(2026, 11, 10)


class CashbookTestBase(TestCase):
    def setUp(self):
        self.owner = make_user("cbowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Cashbook Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner)
        self.client = Client()
        self.client.force_login(self.owner)
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _txn(self, ttype, amount, day=D1, payment_method="CASH",
             description="", expense_kind=None, customer=None):
        payload = {
            "transaction_type": ttype, "amount": amount,
            "transaction_date": str(day), "partner": str(self.partner.id),
            "payment_method": payment_method, "description": description,
        }
        if customer is not None:
            payload["customer"] = str(customer.id)
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}", payload, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return Transaction.objects.get(pk=res.json()["id"])

    def _customer(self, name):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(self.partner.id)},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        from apps.customers.models import Customer
        return Customer.objects.get(pk=res.json()["id"])


class CashPositionTests(CashbookTestBase):
    def test_cash_position_comes_from_the_ledger(self):
        self._txn("INCOME", "5000.00", D1)
        self._txn("EXPENSE", "1200.00", D2)
        self.assertEqual(cashbook.cash_balance(self.biz), Decimal("3800.00"))
        self.assertEqual(cashbook.wallet_balance(self.biz), Decimal("0.00"))

    def test_wallet_money_is_never_cash_in_the_drawer(self):
        self._txn("INCOME", "3000.00", D1, payment_method="BKASH")
        self.assertEqual(cashbook.cash_balance(self.biz), Decimal("0.00"))
        self.assertEqual(cashbook.wallet_balance(self.biz), Decimal("3000.00"))

    def test_day_rows_start_from_the_real_opening_balance(self):
        self._txn("INCOME", "5000.00", D1)
        rows = cashbook.position_rows(self.biz, D1, D2)
        self.assertEqual(rows[0]["closing_cash"], Decimal("5000.00"))
        self.assertEqual(rows[1]["closing_cash"], Decimal("5000.00"))

    def test_a_collection_in_cash_is_in_the_drawer(self):
        # a customer pays their baki in cash: the money is in the drawer
        customer = self._customer("Hosen Ali")
        self._txn("CREDIT", "4000.00", D1, customer=customer)
        self._txn("PAYMENT", "1500.00", D2, customer=customer)
        self.assertEqual(cashbook.cash_balance(self.biz), Decimal("1500.00"))
        rows = cashbook.position_rows(self.biz, D1, D2)
        self.assertEqual(rows[1]["closing_cash"], Decimal("1500.00"))


class RealCashDailyTests(CashbookTestBase):
    """The dashboard chart must show money that really moved."""

    def test_only_real_movement_is_shown(self):
        self._txn("INCOME", "5000.00", D1)
        self._txn("EXPENSE", "1200.00", D2)
        rows = cashbook.real_cash_daily(self.biz, D1, D2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["inflow"], Decimal("5000.00"))
        self.assertEqual(rows[0]["outflow"], Decimal("0.00"))
        self.assertEqual(rows[1]["outflow"], Decimal("1200.00"))
        self.assertEqual(rows[1]["net"], Decimal("-1200.00"))

    def test_a_reversed_entry_is_not_counted(self):
        kept = self._txn("INCOME", "5000.00", D1)
        voided = self._txn("INCOME", "1000.00", D1)
        voided.status = TransactionStatus.REVERSED
        voided.save(update_fields=["status"])
        rows = cashbook.real_cash_daily(self.biz, D1, D2)
        self.assertEqual(rows[0]["inflow"], Decimal("5000.00"))
        # the drawer balance is unchanged either way
        self.assertEqual(cashbook.cash_balance(self.biz), Decimal("6000.00"))
        self.assertTrue(kept.pk)


class DrawerCountTests(CashbookTestBase):
    def test_count_is_compared_with_the_book(self):
        self._txn("INCOME", "5000.00", D1)
        res = self.client.post("/summary/", {
            "action": "count_cash", "counted_on": str(D2),
            "counted_amount": "4800.00"})
        self.assertEqual(res.status_code, 302)
        count = CashCount.objects.get()
        self.assertEqual(count.counted_amount, Decimal("4800.00"))
        self.assertEqual(count.book_amount, Decimal("5000.00"))
        self.assertEqual(count.difference, Decimal("-200.00"))   # ঘাটতি

        drawer = cashbook.drawer_cash(self.biz)
        self.assertTrue(drawer["has_count"])
        self.assertEqual(drawer["counted_amount"], Decimal("4800.00"))
        self.assertEqual(drawer["projected"], Decimal("4800.00"))

    def test_count_uses_the_same_figure_the_page_shows(self):
        # a future-dated entry is part of the drawer, so the comparison
        # must not use an "up to today" balance
        self._txn("INCOME", "5000.00", D1)
        future = date(2099, 1, 1)
        self._txn("INCOME", "1000.00", future)
        res = self.client.post("/summary/", {
            "action": "count_cash", "counted_on": str(D2),
            "counted_amount": "6000.00"})
        self.assertEqual(res.status_code, 302)
        count = CashCount.objects.get()
        self.assertEqual(count.book_amount, Decimal("6000.00"))
        self.assertEqual(count.difference, Decimal("0.00"))

    def test_over_count_is_reported_as_goromil(self):
        self._txn("INCOME", "5000.00", D1)
        self.client.post("/summary/", {
            "action": "count_cash", "counted_on": str(D2),
            "counted_amount": "5200.00"})
        count = CashCount.objects.get()
        self.assertEqual(count.difference, Decimal("200.00"))

    def test_projection_follows_the_money_movement(self):
        self._txn("INCOME", "5000.00", D1)
        self.client.post("/summary/", {
            "action": "count_cash", "counted_on": str(D1),
            "counted_amount": "5000.00"})
        self._txn("INCOME", "1000.00", D2)
        drawer = cashbook.drawer_cash(self.biz)
        # counted 5000 + the 1000 that came in afterwards
        self.assertEqual(drawer["projected"], Decimal("6000.00"))
        self.assertEqual(drawer["book_cash"], Decimal("6000.00"))

    def test_count_without_amount_is_refused(self):
        res = self.client.post("/summary/", {
            "action": "count_cash", "counted_on": str(D1),
            "counted_amount": ""})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(CashCount.objects.count(), 0)

    def test_the_page_no_longer_shows_the_count_form(self):
        # the drawer-count card was removed on request — pin its absence
        self._txn("INCOME", "5000.00", D1)
        resp = self.client.get("/summary/")
        self.assertNotContains(resp, 'name="counted_amount"')
        self.assertNotContains(resp, 'name="counted_on"')

    def test_the_page_no_longer_shows_the_last_count(self):
        self._txn("INCOME", "5000.00", D1)
        self.client.post("/summary/", {
            "action": "count_cash", "counted_on": str(D1),
            "counted_amount": "4900.00"})
        resp = self.client.get("/summary/")
        self.assertNotContains(resp, "সর্বশেষ গোনা")


class RealProfitTests(CashbookTestBase):
    def test_sales_include_cash_and_baki(self):
        customer = self._customer("Hosen Ali")
        self._txn("INCOME", "2000.00", D1)
        self._txn("CREDIT", "3000.00", D1, customer=customer)
        profit = cashbook.profit_breakdown(self.biz, D1, D2)
        self.assertEqual(profit["sales_cash"], Decimal("2000.00"))
        self.assertEqual(profit["sales_due"], Decimal("3000.00"))
        self.assertEqual(profit["sales"], Decimal("5000.00"))

    def test_unsold_stock_is_not_a_loss(self):
        # the shop opened with no stock, bought 3000 worth of goods and
        # sold 2000 of it — 1000 worth is still on the shelf
        self._txn("INCOME", "2000.00", D1)
        self._txn("EXPENSE", "3000.00", D1, description="মাংস ক্রয়")
        StockSnapshot.objects.create(
            business=self.biz, snapshot_on=date(2026, 10, 31),
            kind=StockSnapshot.Kind.OPENING, closing_value=Decimal("0.00"))
        StockSnapshot.objects.create(
            business=self.biz, snapshot_on=D2,
            kind=StockSnapshot.Kind.CLOSING, closing_value=Decimal("1000.00"))

        profit = cashbook.profit_breakdown(self.biz, D1, D2)
        self.assertTrue(profit["stock_complete"])
        self.assertEqual(profit["goods_bought"], Decimal("3000.00"))
        self.assertEqual(profit["stock_delta"], Decimal("1000.00"))
        self.assertEqual(profit["cogs"], Decimal("2000.00"))
        self.assertEqual(profit["profit"], Decimal("0.00"))

    def test_without_a_stock_figure_the_profit_stays_conservative(self):
        self._txn("INCOME", "5000.00", D1)
        self._txn("EXPENSE", "3000.00", D1, description="মাংস ক্রয়")
        profit = cashbook.profit_breakdown(self.biz, D1, D2)
        self.assertFalse(profit["stock_complete"])
        self.assertEqual(profit["stock_delta"], Decimal("0.00"))
        self.assertEqual(profit["cogs"], Decimal("3000.00"))
        self.assertEqual(profit["profit"], Decimal("2000.00"))

    def test_running_costs_are_not_part_of_the_goods(self):
        self._txn("INCOME", "5000.00", D1)
        txn = self._txn("EXPENSE", "500.00", D1, description="দোকান ভাড়া")
        txn.expense_kind = ExpenseKind.OPERATING
        txn.save(update_fields=["expense_kind"])
        profit = cashbook.profit_breakdown(self.biz, D1, D2)
        self.assertEqual(profit["goods_bought"], Decimal("0.00"))
        self.assertEqual(profit["operating"], Decimal("500.00"))
        self.assertEqual(profit["profit"], Decimal("4500.00"))

    def test_stock_snapshot_is_saved_from_the_page(self):
        res = self.client.post("/summary/", {
            "action": "set_stock", "snapshot_on": str(D2),
            "closing_value": "1500.00"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(StockSnapshot.objects.count(), 1)
        self.assertEqual(StockSnapshot.objects.get().closing_value,
                         Decimal("1500.00"))


class LedgerIntegrityTests(CashbookTestBase):
    def test_a_clean_ledger_reports_nothing(self):
        self._txn("INCOME", "5000.00", D1)
        self._txn("EXPENSE", "1000.00", D2)
        report = cashbook.integrity_check(self.biz)
        self.assertTrue(report["clean"], report["broken"])

    def test_an_orphan_posting_is_found(self):
        self._txn("INCOME", "5000.00", D1)
        # a posting whose transaction no longer exists (a soft-deleted row)
        txn = self._txn("EXPENSE", "700.00", D1)
        from apps.accounting.postings import post_auto_journal
        post_auto_journal(txn)
        Transaction.all_objects.filter(pk=txn.pk).update(
            deleted_at="2026-11-11 00:00:00+00:00")

        report = cashbook.integrity_check(self.biz)
        self.assertFalse(report["clean"])
        self.assertTrue(any(r["code"] == "1000" for r in report["broken"]))

    def test_repair_balances_without_deleting(self):
        self._txn("INCOME", "5000.00", D1)
        txn = self._txn("EXPENSE", "700.00", D1)
        from apps.accounting.postings import post_auto_journal
        post_auto_journal(txn)
        Transaction.all_objects.filter(pk=txn.pk).update(
            deleted_at="2026-11-11 00:00:00+00:00")
        before = cashbook.integrity_check(self.biz)
        self.assertFalse(before["clean"])

        res = self.client.post("/summary/", {"action": "repair_ledger"})
        self.assertEqual(res.status_code, 302)
        after = cashbook.integrity_check(self.biz)
        self.assertTrue(after["clean"], after["broken"])
        # the original posting is still there (nothing is deleted)
        self.assertTrue(cashbook.integrity_check(self.biz)["rows"])


class SummaryPageTests(CashbookTestBase):
    def test_page_shows_the_new_sections(self):
        self._txn("INCOME", "5000.00", D1)
        html = self.client.get("/summary/").content.decode("utf-8")
        self.assertIn("আপনার নিট অবস্থান এখন", html)
        # the "what I have" block was removed on request — pin its absence
        self.assertNotIn("ড্রয়ারে নগদ", html)
        self.assertIn("এই সময়ের হিসাব", html)
        self.assertIn("হিসাব যাচাই", html)
        self.assertIn("দিনশেষে হাতে", html)

    def test_page_reports_a_clean_ledger(self):
        self._txn("INCOME", "5000.00", D1)
        html = self.client.get("/summary/").content.decode("utf-8")
        self.assertIn("সব মিলছে", html)
