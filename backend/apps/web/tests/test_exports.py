"""Web UI export tests — partner/date baki khata downloads.

These tests pin the contract of the export feature:
- rows come from non-reversed CREDIT transactions for exactly one partner
  on exactly one date (cash-only transaction types never leak in);
- আগের বাকি is that day's credit total, বর্তমান বাকি is the live
  assignment balance (so later payments reduce it);
- PDF / XLSX / DOCX responses are real files with attachment headers;
- partner users can only export their own book; invalid input redirects.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase

from apps.customers.models import Customer
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import TransactionType
from apps.web.exports import (
    build_khata_export, export_khata_docx, export_khata_pdf,
    export_khata_xlsx,
)

CREDIT_DATE = date(2026, 11, 10)
PAYMENT_DATE = date(2026, 11, 12)


class ExportBase(TestCase):
    def setUp(self):
        self.owner = make_user("wexportowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Export Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])

        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner,
        )
        self.other = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner,
        )
        self.customer = self._add_customer("Rahim Uddin", self.partner)
        self.other_customer = self._add_customer("Salma Begum", self.other)

        # One day's baki: 5000 for the first customer, 2000 for a second
        # customer on the same partner.
        self._txn(self.partner, self.customer, "5000.00", CREDIT_DATE,
                  TransactionType.CREDIT)
        self._txn(self.partner, self.customer, "2000.00", CREDIT_DATE,
                  TransactionType.CREDIT)
        self.second_customer = self._add_customer("Nabila", self.partner)
        self._txn(self.partner, self.second_customer, "1200.00", CREDIT_DATE,
                  TransactionType.CREDIT)
        # A payment on a later date must reduce current due without adding a
        # row to CREDIT_DATE's khata.
        self._txn(self.partner, self.customer, "1000.00", PAYMENT_DATE,
                  TransactionType.PAYMENT)
        # Cash-only type on the same date/partner must never enter the khata.
        self._txn(self.partner, None, "9999.00", CREDIT_DATE,
                  TransactionType.INCOME)
        # Another partner's credit on the same date must stay separate.
        self._txn(self.other, self.other_customer, "7000.00", CREDIT_DATE,
                  TransactionType.CREDIT)

    def _add_customer(self, name, partner):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(partner.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return Customer.objects.get(pk=res.json()["id"])

    def _txn(self, partner, customer, amount, txn_date, txn_type):
        payload = {
            "transaction_type": txn_type,
            "amount": amount,
            "transaction_date": str(txn_date),
            "partner": str(partner.id),
            "payment_method": "CASH",
        }
        if customer is not None:
            payload["customer"] = str(customer.id)
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}", payload,
            format="json")
        self.assertEqual(res.status_code, 201, res.content)

    def _web_client(self, user, role=None):
        client = Client(SERVER_NAME="127.0.0.1")
        client.force_login(user)
        session = client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()
        return client

    def _url(self, fmt, partner, pdate=CREDIT_DATE):
        return (f"/customers/export/{fmt}/?partner={partner.id}"
                f"&pdate={pdate.isoformat()}")


class KhataDataTests(ExportBase):
    def test_rows_totals_and_partner_date_scoping(self):
        data = build_khata_export(self.biz, self.partner, CREDIT_DATE, "bn")

        self.assertEqual(data["count"], 2)
        self.assertEqual([r["name"] for r in data["rows"]],
                         ["Rahim Uddin", "Nabila"])
        self.assertEqual(data["rows"][0]["previous"], Decimal("7000.00"))
        self.assertEqual(data["rows"][1]["previous"], Decimal("1200.00"))
        # Later payment reduces the live balance only.
        self.assertEqual(data["rows"][0]["current"], Decimal("6000.00"))
        self.assertEqual(data["total_previous"], Decimal("8200.00"))
        self.assertEqual(data["total_current"], Decimal("7200.00"))
        # No INCOME row, no other partner's customer.
        names = [r["name"] for r in data["rows"]]
        self.assertNotIn("Salma Begum", names)
        self.assertEqual(data["rows"][0]["tx_count"], 2)

    def test_empty_date_is_safe(self):
        data = build_khata_export(self.biz, self.partner, date(2026, 1, 1), "en")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["total_previous"], Decimal("0.00"))
        self.assertEqual(data["total_current"], Decimal("0.00"))


class ExportFileTests(ExportBase):
    def test_pdf_xlsx_docx_are_valid_files(self):
        data = build_khata_export(self.biz, self.partner, CREDIT_DATE, "bn")
        with patch("apps.web.exports._find_pdf_browser", return_value=None):
            pdf = export_khata_pdf(data)
        xlsx = export_khata_xlsx(data)
        docx = export_khata_docx(data)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertTrue(xlsx.startswith(b"PK"))
        self.assertTrue(docx.startswith(b"PK"))

        from openpyxl import load_workbook
        from io import BytesIO
        wb = load_workbook(BytesIO(xlsx))
        ws = wb.active
        self.assertEqual(ws["A1"].value, "বাকির খাতা — Karim")
        # Premium layout: banner (1-2), subtitle (3), meta (4),
        # stat strip (5-6), blank (7), table header (8), data from 9.
        self.assertEqual(ws["A5"].value, "নাম")
        self.assertEqual(ws["A6"].value, "2")
        self.assertEqual(ws["A8"].value, "ক্রমিক")
        self.assertEqual(ws.cell(row=9, column=3).value, "Rahim Uddin")
        self.assertEqual(ws.cell(row=9, column=5).value, 7000)
        self.assertEqual(ws.cell(row=10, column=3).value, "Nabila")
        # Total row is shaded teal with a double top border.
        total_cell = ws.cell(row=11, column=5)
        self.assertEqual(total_cell.value, 8200)

        from docx import Document
        doc = Document(BytesIO(docx))
        # tables[0] = stat strip, tables[1] = khata table, tables[2] = signatures.
        self.assertEqual(len(doc.tables), 3)
        stat_table = doc.tables[0]
        self.assertIn("2", stat_table.rows[0].cells[0].text)
        table = doc.tables[1]
        self.assertEqual(table.rows[1].cells[2].text, "Rahim Uddin")
        self.assertEqual(table.rows[1].cells[4].text, "৳7,000.00")
        section = doc.sections[0]
        self.assertGreater(section.page_width, section.page_height)


class ExportViewTests(ExportBase):
    def test_admin_downloads_all_formats(self):
        client = self._web_client(self.owner)
        expected = {
            "pdf": ("application/pdf", b"%PDF"),
            "xlsx": ("application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet", b"PK"),
            "docx": ("application/vnd.openxmlformats-officedocument"
                     ".wordprocessingml.document", b"PK"),
        }
        with patch("apps.web.exports._find_pdf_browser", return_value=None):
            for fmt, (content_type, signature) in expected.items():
                res = client.get(self._url(fmt, self.partner))
                self.assertEqual(res.status_code, 200, (fmt, res.content[:200]))
                self.assertEqual(res["Content-Type"], content_type)
                self.assertTrue(res.content.startswith(signature), fmt)
                self.assertIn("attachment;", res["Content-Disposition"])
                self.assertIn(f".{fmt}", res["Content-Disposition"])
                self.assertEqual(res["Cache-Control"], "private, no-store")

    def test_partner_can_only_export_own_book(self):
        partner_user = make_user("wexportpartner")
        res = auth_client(self.owner).post(
            f"/api/businesses/{self.biz.id}/members/",
            {"email": partner_user.email, "role": "PARTNER"}, format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.partner.linked_user = partner_user
        self.partner.save(update_fields=["linked_user"])

        client = self._web_client(partner_user)
        with patch("apps.web.exports._find_pdf_browser", return_value=None):
            own = client.get(self._url("pdf", self.partner))
            other = client.get(self._url("pdf", self.other))
        self.assertEqual(own.status_code, 200, own.get("Location", ""))
        self.assertEqual(other.status_code, 403)

    def test_invalid_requests_redirect_or_404(self):
        client = self._web_client(self.owner)
        self.assertEqual(
            client.get("/customers/export/pdf/?pdate=2026-11-10").status_code,
            302,
        )
        self.assertEqual(
            client.get("/customers/export/pdf/?partner=not-a-uuid"
                       "&pdate=2026-11-10").status_code,
            302,
        )
        res = client.get(f"/customers/export/pdf/?partner={self.partner.id}"
                         "&pdate=bad-date")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(
            client.get(self._url("txt", self.partner)).status_code, 404,
        )

    def test_business_membership_is_required(self):
        stranger = make_user("wexportstranger")
        client = self._web_client(stranger)
        res = client.get(self._url("pdf", self.partner))
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], "/")
