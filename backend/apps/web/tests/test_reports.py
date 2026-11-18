"""রিপোর্ট — P&L, cash flow and partner statement download tests.

Pinned rules:

- the /reports/ page renders for a member with a business;
- each report downloads as PDF (``%PDF`` magic) and as XLSX
  (spreadsheetML content-type);
- partner-role users are locked to their own partner book — naming
  another partner is rejected;
- reversed transactions never enter the partner statement builder.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.customers.models import Customer
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionType

D1 = date(2026, 11, 5)


class ReportsTests(TestCase):
    def setUp(self):
        self.owner = make_user("reportowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Report Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)
        self.customer = self._add_customer("Hosen Ali")

        # a due, a payment and an expense (journals are posted by the API)
        self._txn("CREDIT", "3000.00")
        self._txn("PAYMENT", "1000.00")
        self._txn("EXPENSE", "450.00")

        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()

    # -- helpers ---------------------------------------------------------
    def _add_customer(self, name):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(self.partner.id)},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return Customer.objects.get(pk=res.json()["id"])

    def _txn(self, txn_type, amount):
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}",
            {"transaction_type": txn_type, "amount": amount,
             "transaction_date": D1.isoformat(),
             "partner": str(self.partner.id),
             "customer": str(self.customer.id),
             "payment_method": "CASH",
             "description": ""},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res

    # -- page ------------------------------------------------------------
    def test_reports_page_renders(self):
        res = self.client.get("/reports/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn("রিপোর্ট", body)
        self.assertIn("লাভ-ক্ষতি", body)
        self.assertIn("পার্টনার স্টেটমেন্ট", body)

    def test_reports_page_year_preset_renders(self):
        res = self.client.get("/reports/?preset=year")
        self.assertEqual(res.status_code, 200)

    # -- PDF downloads ----------------------------------------------------
    def test_pnl_pdf_downloads(self):
        res = self.client.get("/reports/?report=pnl&format=pdf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertTrue(res.content[:4] == b"%PDF")
        self.assertIn("profit-loss-", res["Content-Disposition"])

    def test_cashflow_pdf_downloads(self):
        res = self.client.get("/reports/?report=cashflow&format=pdf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertTrue(res.content[:4] == b"%PDF")
        self.assertIn("cash-flow-", res["Content-Disposition"])

    def test_partner_statement_pdf_downloads(self):
        res = self.client.get("/reports/?report=partner&format=pdf")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertTrue(res.content[:4] == b"%PDF")

    # -- XLSX downloads ---------------------------------------------------
    def test_pnl_xlsx_downloads(self):
        res = self.client.get("/reports/?report=pnl&format=xlsx")
        self.assertEqual(res.status_code, 200)
        self.assertIn("spreadsheetml", res["Content-Type"])
        self.assertGreater(len(res.content), 2000)

    def test_cashflow_xlsx_downloads(self):
        res = self.client.get("/reports/?report=cashflow&format=xlsx")
        self.assertEqual(res.status_code, 200)
        self.assertIn("spreadsheetml", res["Content-Type"])

    def test_partner_statement_xlsx_downloads(self):
        res = self.client.get("/reports/?report=partner&format=xlsx")
        self.assertEqual(res.status_code, 200)
        self.assertIn("spreadsheetml", res["Content-Type"])

    def test_unknown_report_404s(self):
        res = self.client.get("/reports/?report=nope&format=pdf")
        self.assertEqual(res.status_code, 404)

    # -- builder correctness ----------------------------------------------
    def test_partner_statement_builder_excludes_reversals(self):
        from apps.web.report_exports import build_partner_statement
        self._txn("CREDIT", "9999.00")
        # reverse the second CREDIT by hand
        rev = Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.CREDIT,
            amount=Decimal("9999.00")).first()
        rev.status = "REVERSED"
        rev.save(update_fields=["status"])

        data = build_partner_statement(self.biz, self.partner, D1, D1)
        self.assertEqual(data["total_given"], Decimal("3000.00"))
        self.assertEqual(data["total_collected"], Decimal("1000.00"))
        self.assertEqual(data["closing"], Decimal("2000.00"))
        self.assertEqual(len(data["rows"]), 2)  # original CREDIT + PAYMENT

    # -- partner role -------------------------------------------------------
    def test_partner_role_cannot_pick_another_partner(self):
        partner_user = make_user("reportbob")
        self.partner.linked_user = partner_user
        self.partner.save(update_fields=["linked_user"])
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz,
            role=BusinessMembership.Role.PARTNER, is_active=True)
        other = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE", created_by=self.owner)

        client = Client()
        client.force_login(partner_user)
        session = client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

        res = client.get(f"/reports/?report=partner&format=pdf&partner={other.id}")
        self.assertEqual(res.status_code, 403)

        # own book still works
        res = client.get("/reports/?report=partner&format=pdf")
        self.assertEqual(res.status_code, 200)
