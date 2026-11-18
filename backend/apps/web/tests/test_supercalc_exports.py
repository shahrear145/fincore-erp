"""সুপার ক্যালকুলেটর export tests — PDF / Excel / Word downloads.

These tests pin the contract of the export feature:

- the data comes from the same engine the page shows (calc_day_rows /
  calc_totals): মোট সেল = নগদে সেল (INCOME) + বাকিতে সেল (CREDIT), and a
  due collection (PAYMENT) is never counted as sell;
- profit / running values carry an explicit sign (+৳ / -৳);
- PDF / XLSX / DOCX responses are real files with attachment headers;
- invalid dates fall back to the page defaults, unknown formats 404,
  and a business membership is required.
"""
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType
from apps.web.exports import (
    build_supercalc_export, export_supercalc_docx, export_supercalc_pdf,
    export_supercalc_xlsx,
)

D1 = date(2026, 11, 1)
D2 = date(2026, 11, 2)


class SuperCalcExportBase(TestCase):
    def setUp(self):
        self.owner = make_user("scexportowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Super Export Co"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.client = Client(SERVER_NAME="127.0.0.1")
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()

    def _txn(self, ttype, amount, day,
             status=TransactionStatus.COMPLETED):
        return Transaction.objects.create(
            business=self.biz, transaction_type=ttype,
            amount=Decimal(amount), transaction_date=day,
            payment_method="CASH", status=status,
            created_by=self.owner)

    def _web_client(self, user):
        client = Client(SERVER_NAME="127.0.0.1")
        client.force_login(user)
        session = client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()
        return client

    def _url(self, fmt, dfrom=D1, dto=D2):
        return (f"/supercalculator/export/{fmt}/"
                f"?from={dfrom.isoformat()}&to={dto.isoformat()}")


class SuperCalcExportDataTests(SuperCalcExportBase):
    def test_rows_totals_and_signs(self):
        # D1: সেল 800 (500 cash + 300 due), খরচ 200 → লাভ 600
        self._txn(TransactionType.INCOME, "500.00", D1)
        self._txn(TransactionType.CREDIT, "300.00", D1)
        self._txn(TransactionType.EXPENSE, "200.00", D1)
        # D2: সেল 150, খরচ 50 → লাভ 100; a collection is context only.
        self._txn(TransactionType.INCOME, "150.00", D2)
        self._txn(TransactionType.EXPENSE, "50.00", D2)
        self._txn(TransactionType.PAYMENT, "100.00", D2)

        data = build_supercalc_export(self.biz, D1, D2, "bn")

        self.assertEqual(data["count"], 2)
        self.assertEqual(data["days_with_data"], 2)
        self.assertEqual(data["rows"][0]["date_label"], "01 Nov 2026")
        self.assertEqual(data["rows"][0]["sell_total"], Decimal("800.00"))
        self.assertEqual(data["rows"][0]["profit"], Decimal("600.00"))
        self.assertEqual(data["rows"][0]["profit_display"], "+৳600.00")
        self.assertEqual(data["rows"][0]["running_display"], "+৳600.00")
        self.assertEqual(data["rows"][1]["profit"], Decimal("100.00"))
        self.assertEqual(data["rows"][1]["running"], Decimal("700.00"))
        self.assertEqual(data["rows"][1]["running_display"], "+৳700.00")

        t = data["totals"]
        self.assertEqual(t["sell_cash"], Decimal("650.00"))
        self.assertEqual(t["sell_due"], Decimal("300.00"))
        self.assertEqual(t["sell_total"], Decimal("950.00"))
        self.assertEqual(t["khoroch"], Decimal("250.00"))
        self.assertEqual(t["profit"], Decimal("700.00"))
        self.assertEqual(t["profit_display"], "+৳700.00")
        # The ৳100 collection never lands in a sell column.
        self.assertEqual(t["collection"], Decimal("100.00"))

    def test_loss_negative_display(self):
        self._txn(TransactionType.INCOME, "400.00", D1)
        self._txn(TransactionType.EXPENSE, "900.00", D1)

        data = build_supercalc_export(self.biz, D1, D1, "en")
        self.assertEqual(data["totals"]["profit"], Decimal("-500.00"))
        self.assertEqual(data["totals"]["profit_display"], "-৳500.00")
        self.assertEqual(data["rows"][0]["running_display"], "-৳500.00")

    def test_empty_period_is_safe(self):
        data = build_supercalc_export(
            self.biz, date(2026, 1, 1), date(2026, 1, 3), "en")
        # One zero row per calendar day, like the page shows.
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["days_with_data"], 0)
        for row in data["rows"]:
            self.assertEqual(row["sell_total"], Decimal("0.00"))
            self.assertEqual(row["khoroch"], Decimal("0.00"))
            self.assertEqual(row["profit"], Decimal("0.00"))
        t = data["totals"]
        self.assertEqual(t["sell_total"], Decimal("0.00"))
        self.assertEqual(t["khoroch"], Decimal("0.00"))
        self.assertEqual(t["profit"], Decimal("0.00"))
        self.assertEqual(t["profit_display"], "৳0.00")

    def test_reversed_rows_are_excluded(self):
        self._txn(TransactionType.INCOME, "700.00", D1,
                  status=TransactionStatus.REVERSED)
        self._txn(TransactionType.EXPENSE, "600.00", D1,
                  status=TransactionStatus.REVERSED)
        data = build_supercalc_export(self.biz, D1, D1, "bn")
        self.assertEqual(data["totals"]["sell_total"], Decimal("0.00"))
        self.assertEqual(data["totals"]["profit"], Decimal("0.00"))


class SuperCalcExportFileTests(SuperCalcExportBase):
    def setUp(self):
        super().setUp()
        self._txn(TransactionType.INCOME, "500.00", D1)
        self._txn(TransactionType.CREDIT, "300.00", D1)
        self._txn(TransactionType.EXPENSE, "200.00", D1)
        self._txn(TransactionType.INCOME, "150.00", D2)
        self._txn(TransactionType.EXPENSE, "50.00", D2)
        self.data = build_supercalc_export(self.biz, D1, D2, "bn")

    def test_pdf_xlsx_docx_are_valid_files(self):
        with patch("apps.web.exports._find_pdf_browser", return_value=None):
            pdf = export_supercalc_pdf(self.data)
        xlsx = export_supercalc_xlsx(self.data)
        docx = export_supercalc_docx(self.data)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertTrue(xlsx.startswith(b"PK"))
        self.assertTrue(docx.startswith(b"PK"))

        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(xlsx))
        ws = wb.active
        self.assertEqual(ws["A1"].value, "সুপার ক্যালকুলেটর — Super Export Co")
        # Premium layout: banner (1-2), subtitle (3), meta (4),
        # stat strip (5-6), blank (7), table header (8), data from 9.
        self.assertEqual(ws["A5"].value, "মোট সেল")
        self.assertEqual(ws["A6"].value, "৳950.00")
        self.assertEqual(ws["A8"].value, "তারিখ")
        self.assertEqual(ws.cell(row=9, column=1).value, "01 Nov 2026")
        self.assertEqual(ws.cell(row=9, column=4).value, 800)
        self.assertEqual(ws.cell(row=9, column=6).value, 600)
        self.assertEqual(ws.cell(row=10, column=6).value, 100)
        self.assertEqual(ws.cell(row=10, column=7).value, 700)
        # Gold total row: sell 950 / khoroch 250 / profit 700 / running 700.
        self.assertEqual(ws.cell(row=11, column=4).value, 950)
        self.assertEqual(ws.cell(row=11, column=5).value, 250)
        self.assertEqual(ws.cell(row=11, column=6).value, 700)
        self.assertEqual(ws.cell(row=11, column=7).value, 700)

        from docx import Document
        doc = Document(BytesIO(docx))
        # tables[0] = stat strip, tables[1] = day table, tables[2] = signatures.
        self.assertEqual(len(doc.tables), 3)
        self.assertIn("মোট সেল", doc.tables[0].rows[0].cells[0].text)
        self.assertIn("৳950.00", doc.tables[0].rows[0].cells[0].text)
        table = doc.tables[1]
        self.assertEqual(table.rows[1].cells[0].text, "01 Nov 2026")
        self.assertEqual(table.rows[1].cells[3].text, "৳800.00")
        self.assertEqual(table.rows[1].cells[5].text, "+৳600.00")
        section = doc.sections[0]
        self.assertGreater(section.page_width, section.page_height)

    def test_empty_period_files_still_render(self):
        data = build_supercalc_export(
            self.biz, date(2026, 1, 1), date(2026, 1, 3), "bn")
        with patch("apps.web.exports._find_pdf_browser", return_value=None):
            pdf = export_supercalc_pdf(data)
        xlsx = export_supercalc_xlsx(data)
        docx = export_supercalc_docx(data)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertTrue(xlsx.startswith(b"PK"))
        self.assertTrue(docx.startswith(b"PK"))


class SuperCalcExportViewTests(SuperCalcExportBase):
    def test_admin_downloads_all_formats(self):
        expected = {
            "pdf": ("application/pdf", b"%PDF"),
            "xlsx": ("application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet", b"PK"),
            "docx": ("application/vnd.openxmlformats-officedocument"
                     ".wordprocessingml.document", b"PK"),
        }
        with patch("apps.web.exports._find_pdf_browser", return_value=None):
            for fmt, (content_type, signature) in expected.items():
                res = self.client.get(self._url(fmt))
                self.assertEqual(res.status_code, 200, (fmt, res.content[:200]))
                self.assertEqual(res["Content-Type"], content_type)
                self.assertTrue(res.content.startswith(signature), fmt)
                self.assertIn("attachment;", res["Content-Disposition"])
                self.assertIn(f".{fmt}", res["Content-Disposition"])
                self.assertIn("super-calculator-", res["Content-Disposition"])
                self.assertEqual(res["Cache-Control"], "private, no-store")

    def test_missing_or_invalid_dates_fall_back_to_defaults(self):
        with patch("apps.web.exports._find_pdf_browser", return_value=None):
            res = self.client.get("/supercalculator/export/pdf/")
            self.assertEqual(res.status_code, 200)
            res = self.client.get(
                "/supercalculator/export/xlsx/?from=bad-date&to=xxx")
            self.assertEqual(res.status_code, 200)

    def test_unsupported_format_is_404(self):
        res = self.client.get(self._url("txt"))
        self.assertEqual(res.status_code, 404)

    def test_business_membership_is_required(self):
        stranger = make_user("scexportstranger")
        client = self._web_client(stranger)
        res = client.get(self._url("pdf"))
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], "/")

    def test_page_shows_export_buttons(self):
        html = self.client.get("/supercalculator/").content.decode("utf-8")
        self.assertIn("/supercalculator/export/pdf/", html)
        self.assertIn("/supercalculator/export/xlsx/", html)
        self.assertIn("/supercalculator/export/docx/", html)
        self.assertIn("export-btn pdf", html)
