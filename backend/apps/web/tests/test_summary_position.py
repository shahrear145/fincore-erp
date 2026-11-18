"""হিসাব সারাংশ — the redesigned page (position blocks) and its exports.

Pinned rules:

- the page shows two separate blocks: what the business HAS and what it
  still OWES, and the net position is the first minus the second;
- every box carries its own figure (cash, wallet, baki due, stock on the
  shelf / loan, investors' capital, advances, bills to pay);
- the loan and investment boxes show taken vs repaid vs outstanding;
- the date filter and the export buttons are still there;
- the exports (PDF / Excel / Word) carry the same two blocks and the
  day-wise table.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType

D1 = date(2026, 11, 1)
D2 = date(2026, 11, 2)


class SummaryPositionTests(TestCase):
    def setUp(self):
        self.owner = make_user("sumpos")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Position Traders"}, format="json")
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

    def _txn(self, ttype, amount, day=D1, method="CASH", customer=None):
        """Posted through the API so the double-entry ledger gets it too."""
        payload = {
            "transaction_type": ttype, "amount": amount,
            "transaction_date": str(day), "payment_method": method,
            "partner": str(self.partner.id),
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

    # -- the position service ---------------------------------------------
    def test_have_minus_owe_is_the_net_position(self):
        from apps.cashbook.services import business_position
        self._txn(TransactionType.INCOME, "10000.00", D1)
        position = business_position(self.biz)
        self.assertEqual(position["cash"], Decimal("10000.00"))
        self.assertEqual(position["wallet"], Decimal("0.00"))
        self.assertEqual(position["have"], Decimal("10000.00"))
        self.assertEqual(position["owe"], Decimal("0.00"))
        self.assertEqual(position["net"], position["have"] - position["owe"])

    def test_money_in_hand_and_money_still_to_come_are_separate(self):
        from apps.cashbook.services import business_position
        self._txn(TransactionType.INCOME, "10000.00", D1)
        # a customer who owes money (it is not in the drawer yet)
        customer = self._customer("Hosen Ali")
        self._txn(TransactionType.CREDIT, "4000.00", D1, customer=customer)
        position = business_position(self.biz)
        self.assertEqual(position["cash_in_hand"], Decimal("10000.00"))
        self.assertEqual(position["due"], Decimal("4000.00"))
        # the due is part of what the business owns, but NOT of the cash
        # you can touch right now
        self.assertEqual(position["have"], Decimal("14000.00"))

    def test_collected_money_is_already_inside_the_drawer_cash(self):
        from apps.cashbook.services import business_position
        customer = self._customer("Hosen Ali")
        self._txn(TransactionType.CREDIT, "4000.00", D1, customer=customer)
        self._txn(TransactionType.PAYMENT, "1500.00", D1, customer=customer)
        position = business_position(self.biz)
        # the 1500 collected is cash in the drawer
        self.assertEqual(position["cash"], Decimal("1500.00"))
        # and 2500 of the due is still out there
        self.assertEqual(position["due"], Decimal("2500.00"))
        self.assertEqual(position["cash_in_hand"], Decimal("1500.00"))

    def test_wallet_money_is_not_cash(self):
        from apps.cashbook.services import business_position
        self._txn(TransactionType.INCOME, "4000.00", D1, method="BKASH")
        position = business_position(self.biz)
        self.assertEqual(position["cash"], Decimal("0.00"))
        self.assertEqual(position["wallet"], Decimal("4000.00"))

    def test_investment_is_our_own_money_not_a_debt(self):
        from apps.cashbook.services import business_position
        from apps.loans.models import LoanParty
        from apps.loans.service import create_entry
        from apps.organizations.models import BusinessMembership
        from types import SimpleNamespace
        membership = BusinessMembership.objects.filter(
            user=self.owner, business=self.biz).first()
        actor = SimpleNamespace(user=self.owner, business=self.biz,
                                business_membership=membership)
        party = LoanParty.objects.create(business=self.biz, name="Abu",
                                         kind=LoanParty.Kind.INVESTMENT)
        create_entry(actor, business=self.biz, party=party,
                     kind="RECEIVED", amount="50000.00",
                     transaction_date=D1, description="capital")
        position = business_position(self.biz)
        self.assertEqual(position["invest_outstanding"], Decimal("50000.00"))
        self.assertEqual(position["invest_received"], Decimal("50000.00"))
        self.assertEqual(position["invest_returned"], Decimal("0.00"))
        # the investors' money is our own money: it is neither a debt nor
        # added again (the cash it became is already counted)
        self.assertEqual(position["owe"], Decimal("0.00"))
        self.assertEqual(position["net"], position["have"])

    # -- the page ----------------------------------------------------------
    def test_page_shows_both_blocks_and_the_net_position(self):
        self._txn(TransactionType.INCOME, "10000.00", D1)
        html = self.client.get("/summary/").content.decode("utf-8")
        self.assertIn("ব্যবসার কাছে যা আছে", html)
        self.assertIn("যা ফেরত দিতে হবে", html)
        self.assertIn("নিট অবস্থান", html)
        # the "what I have" block was removed on request — pin its absence
        self.assertNotIn("ড্রয়ারে নগদ", html)
        self.assertNotIn("বিকাশ / নগদ / ব্যাংক", html)
        self.assertNotIn("কাস্টমাররা আমাকে দেবে", html)
        self.assertIn("ঋণদাতাকে ফেরত", html)
        self.assertIn("খরচ বাকি", html)
        # no stock box (not needed) and the investors' money is information
        self.assertNotIn("শেলে থাকা মজুদ", html)
        self.assertIn("বিনিয়োগকারীর মূল", html)
        self.assertIn("নিজের টাকা", html)

    def test_page_keeps_the_date_filter_and_the_exports(self):
        html = self.client.get("/summary/").content.decode("utf-8")
        for preset in ("আজ", "এই সপ্তাহ", "এই মাস", "গত মাস", "এই বছর"):
            self.assertIn(preset, html)
        self.assertIn('name="from"', html)
        self.assertIn('name="to"', html)
        self.assertIn("/summary/export/pdf/", html)
        self.assertIn("/summary/export/xlsx/", html)
        self.assertIn("/summary/export/docx/", html)

    # -- the exports -------------------------------------------------------
    def test_exports_are_produced(self):
        self._txn(TransactionType.INCOME, "10000.00", D1)
        for fmt, head in (("xlsx", b"PK"), ("docx", b"PK"), ("pdf", b"%PDF")):
            res = self.client.get(
                f"/summary/export/{fmt}/?from=2026-11-01&to=2026-11-30")
            self.assertEqual(res.status_code, 200, fmt)
            self.assertTrue(res.content.startswith(head), fmt)
            self.assertIn("attachment", res["Content-Disposition"])

    def test_export_data_carries_both_blocks(self):
        from apps.web.summary_export import build_summary_export
        self._txn(TransactionType.INCOME, "10000.00", D1)
        data = build_summary_export(self.biz, D1, D2, "bn")
        self.assertEqual(len(data["have"]), 3)
        self.assertEqual(len(data["owe"]), 3)
        self.assertEqual(data["have_total"], sum(
            item["value"] for item in data["have"]))
        self.assertEqual(data["owe_total"], sum(
            item["value"] for item in data["owe"]))
        self.assertEqual(data["net"],
                         data["have_total"] - data["owe_total"])
        self.assertTrue(data["rows"])
        # the investors' money travels as information, never as a debt
        self.assertIn("invest", data)
