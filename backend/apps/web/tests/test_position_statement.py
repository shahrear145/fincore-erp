"""হিসাব কমান্ড সেন্টার — the self-auditing position statement.

Pinned rules:

- the page is read-only: it only answers GET and never posts anything;
- every figure is built from the same engine the exports use;
- the audit is deterministic and always checks today's data, even when the
  page is viewing an older as-of date;
- the waterfall parts add up to the net position (the proof badge);
- the health score is the sum of its four visible parts;
- the trend is 90 daily points and the two-date comparison reports the delta;
- PDF / Excel / Word all come out as real files.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.utils import timezone

from apps.accounting.models import JournalEntry
from apps.cashbook.models import CashCount
from apps.customers.models import Customer
from apps.loans.models import LoanEntry, LoanParty
from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import TransactionType

TODAY = timezone.localdate()
D1 = TODAY - timedelta(days=10)
D2 = TODAY - timedelta(days=5)


class PositionStatementTests(TestCase):
    def setUp(self):
        self.owner = make_user("position")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Command Traders"}, format="json")
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

    # -- helpers ---------------------------------------------------------
    def _txn(self, ttype, amount, day=D1, method="CASH", customer=None):
        payload = {
            "transaction_type": ttype, "amount": amount,
            "transaction_date": str(day), "payment_method": method,
            "partner": str(self.partner.id),
        }
        if customer is not None:
            payload["customer"] = str(customer.id)
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}", payload,
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["id"]

    def _customer(self, name):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(self.partner.id)},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return Customer.objects.get(pk=res.json()["id"])

    def _statement(self, **kwargs):
        from apps.web.position_statement import build_statement
        return build_statement(self.biz, **kwargs)

    def _audit_keys(self, statement):
        return {f["key"] for f in statement["audit"]["findings"]}

    # -- access + page ----------------------------------------------------
    def test_anonymous_is_sent_to_login(self):
        res = Client().get("/summary/position/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login/", res["Location"])

    def test_page_opens_with_every_cockpit_section(self):
        self._txn(TransactionType.INCOME, "10000.00")
        res = self.client.get("/summary/position/")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode("utf-8")
        for text in (
                "হিসাব কমান্ড সেন্টার", "নিট অবস্থান",
                "অডিট — 11টি চেক", "নিট অবস্থান কীভাবে হলো",
                "কাস্টমারের বাকি — পার্টনার অনুযায়ী",
                "৯০ দিনের নিট অবস্থান", "দুই তারিখের তুলনা",
                "এই স্বাস্থ্য স্কোর কেন", "কী-হবে সিমুলেটর"):
            self.assertIn(text, html)
        for fmt in ("pdf", "xlsx", "docx"):
            self.assertIn(f"/summary/position/export/{fmt}/", html)

    def test_page_only_accepts_get(self):
        res = self.client.post("/summary/position/")
        self.assertEqual(res.status_code, 405)

    def test_page_says_it_is_read_only(self):
        html = self.client.get("/summary/position/").content.decode("utf-8")
        self.assertIn("শুধু পড়ার পেজ", html)
        self.assertIn("কোনো লেনদেন পোস্ট, সম্পাদনা বা মুছে ফেলা হয় না", html)

    def test_summary_hero_opens_the_command_center(self):
        html = self.client.get("/summary/").content.decode("utf-8")
        self.assertIn("/summary/position/", html)
        self.assertIn("ভেতরটা দেখুন", html)

    # -- as-of + drill-down ----------------------------------------------
    def test_as_of_uses_the_ledger_at_that_date(self):
        self._txn(TransactionType.INCOME, "1000.00", D1)
        self._txn(TransactionType.INCOME, "2000.00", D2)
        old = self._statement(as_of=D1)
        new = self._statement(as_of=D2)
        self.assertEqual(old["position"]["cash"], Decimal("1000.00"))
        self.assertEqual(old["position"]["net"], Decimal("1000.00"))
        self.assertEqual(new["position"]["cash"], Decimal("3000.00"))
        self.assertEqual(new["position"]["net"], Decimal("3000.00"))

    def test_customer_due_is_broken_down_by_partner(self):
        customer = self._customer("Hosen Ali")
        self._txn(TransactionType.CREDIT, "4000.00", customer=customer)
        statement = self._statement()
        self.assertEqual(statement["due"]["total"], Decimal("4000.00"))
        self.assertEqual(statement["due"]["partner_count"], 1)
        group = statement["due"]["groups"][0]
        self.assertEqual(group["partner_name"], "Rahim")
        self.assertEqual(group["customers"][0]["name"], "Hosen Ali")
        self.assertEqual(group["customers"][0]["due"], Decimal("4000.00"))
        self.assertEqual(len(group["customers"][0]["entries"]), 1)
        html = self.client.get("/summary/position/").content.decode("utf-8")
        self.assertIn("Hosen Ali", html)

    def test_cash_ledger_carries_a_running_balance(self):
        self._txn(TransactionType.INCOME, "1000.00", D1)
        self._txn(TransactionType.EXPENSE, "300.00", D2)
        section = self._statement()["cash"]
        self.assertEqual([row["delta"] for row in section["rows"]],
                         [Decimal("1000.00"), Decimal("-300.00")])
        self.assertEqual([row["running"] for row in section["rows"]],
                         [Decimal("1000.00"), Decimal("700.00")])
        self.assertEqual(section["total"], Decimal("700.00"))

    def test_waterfall_proof_adds_up_to_the_net(self):
        customer = self._customer("Hosen Ali")
        self._txn(TransactionType.INCOME, "1000.00", D1)
        self._txn(TransactionType.CREDIT, "4000.00", customer=customer)
        self._txn(TransactionType.EXPENSE, "300.00", D2)
        statement = self._statement()
        self.assertTrue(statement["proof"]["ok"])
        self.assertEqual(statement["proof"]["difference"], Decimal("0.00"))
        self.assertEqual(statement["proof"]["parts_sum"],
                         statement["position"]["net"])
        self.assertEqual(len(statement["composition"]), 6)

    # -- the audit engine -------------------------------------------------
    def test_clean_books_pass_every_check(self):
        self._txn(TransactionType.INCOME, "10000.00")
        audit = self._statement()["audit"]
        self.assertTrue(audit["clean"])
        self.assertEqual(audit["total"], 0)
        self.assertEqual(audit["checks_run"], 11)

    def test_missing_journal_is_reported_as_unposted(self):
        txn_id = self._txn(TransactionType.INCOME, "10000.00")
        JournalEntry.objects.filter(
            business=self.biz, source_kind="TXN",
            source_id=txn_id).delete()
        audit = self._statement()["audit"]
        self.assertIn("unposted", self._audit_keys({"audit": audit}))
        finding = next(f for f in audit["findings"]
                       if f["key"] == "unposted")
        self.assertEqual(finding["count"], 1)
        self.assertTrue(finding["items"])
        self.assertTrue(finding["fix_url"])

    def test_future_dated_transaction_is_flagged(self):
        self._txn(TransactionType.INCOME, "500.00", TODAY + timedelta(days=7))
        audit = self._statement()["audit"]
        keys = {f["key"] for f in audit["findings"]}
        self.assertIn("future", keys)
        finding = next(f for f in audit["findings"] if f["key"] == "future")
        self.assertEqual(finding["amount"], Decimal("500.00"))

    def test_identical_same_day_transactions_are_duplicate_suspects(self):
        self._txn(TransactionType.INCOME, "700.00", D1)
        self._txn(TransactionType.INCOME, "700.00", D1)
        audit = self._statement()["audit"]
        finding = next(f for f in audit["findings"]
                       if f["key"] == "duplicates")
        self.assertEqual(finding["count"], 1)
        self.assertIn("700.00", finding["items"][0]["label"])

    def test_drawer_gap_finds_same_amount_candidates(self):
        self._txn(TransactionType.INCOME, "1000.00", D1)
        self._txn(TransactionType.INCOME, "320.00", TODAY - timedelta(days=2))
        CashCount.objects.create(
            business=self.biz, counted_on=TODAY,
            counted_amount=Decimal("1000.00"),
            book_amount=Decimal("1320.00"),
            difference=Decimal("320.00"))
        audit = self._statement()["audit"]
        finding = next(f for f in audit["findings"] if f["key"] == "drawer")
        self.assertEqual(finding["amount"], Decimal("320.00"))
        self.assertTrue(finding["items"])
        self.assertIn("320.00", finding["items"][0]["label"])

    def test_negative_cash_day_is_flagged(self):
        self._txn(TransactionType.INCOME, "100.00", D1)
        self._txn(TransactionType.EXPENSE, "300.00", D2)
        audit = self._statement()["audit"]
        keys = {f["key"] for f in audit["findings"]}
        self.assertIn("negative_cash", keys)

    def test_loan_book_ledger_mismatch_is_flagged(self):
        party = LoanParty.objects.create(
            business=self.biz, name="Abu", kind=LoanParty.Kind.LOAN)
        LoanEntry.objects.create(
            business=self.biz, party=party,
            kind=LoanEntry.Kind.RECEIVED, amount=Decimal("5000.00"),
            transaction_date=D1, status=LoanEntry.Status.POSTED)
        audit = self._statement()["audit"]
        finding = next(f for f in audit["findings"]
                       if f["key"] == "loan_ledger")
        self.assertEqual(finding["amount"], Decimal("5000.00"))
        self.assertTrue(finding["items"])

    def test_customer_without_a_partner_is_flagged(self):
        Customer.objects.create(
            business=self.biz, name="No Partner", status="ACTIVE")
        audit = self._statement()["audit"]
        finding = next(f for f in audit["findings"]
                       if f["key"] == "no_partner")
        self.assertEqual(finding["count"], 1)
        self.assertIn("No Partner", finding["items"][0]["label"])

    def test_audit_checks_today_even_when_viewing_the_past(self):
        self._txn(TransactionType.INCOME, "500.00", TODAY + timedelta(days=7))
        statement = self._statement(as_of=D1)
        keys = {f["key"] for f in statement["audit"]["findings"]}
        self.assertIn("future", keys)
        # ...while the position itself still follows the chosen date
        self.assertEqual(statement["position"]["cash"], Decimal("0.00"))

    # -- health / trend / compare ----------------------------------------
    def test_health_score_is_the_sum_of_its_visible_parts(self):
        self._txn(TransactionType.INCOME, "10000.00")
        health = self._statement()["health"]
        self.assertEqual(len(health["parts"]), 4)
        self.assertEqual([p["max"] for p in health["parts"]],
                         [30, 25, 25, 20])
        total = sum(Decimal(str(p["score"])) for p in health["parts"])
        self.assertLessEqual(abs(total - Decimal(health["score"])), 1)
        self.assertGreaterEqual(health["score"], 0)
        self.assertLessEqual(health["score"], 100)
        self.assertTrue(health["grade"])

    def test_trend_has_ninety_daily_points(self):
        self._txn(TransactionType.INCOME, "10000.00", TODAY - timedelta(days=20))
        trend = self._statement()["trend"]
        self.assertEqual(len(trend["points"]), 90)
        self.assertEqual(trend["start_date"], TODAY - timedelta(days=89))
        self.assertEqual(trend["end_date"], TODAY)
        self.assertTrue(trend["path"].startswith("M "))
        self.assertEqual(trend["end_net"], Decimal("10000.00"))

    def test_two_date_comparison_reports_the_delta(self):
        self._txn(TransactionType.INCOME, "5000.00", D2)
        statement = self._statement(as_of=TODAY, compare=D1)
        comparison = statement["comparison"]
        self.assertEqual(comparison["net_before"], Decimal("0.00"))
        self.assertEqual(comparison["net_after"], Decimal("5000.00"))
        self.assertEqual(comparison["net_delta"], Decimal("5000.00"))
        cash_row = next(r for r in comparison["rows"] if r["key"] == "cash")
        self.assertEqual(cash_row["delta"], Decimal("5000.00"))
        html = self.client.get(
            f"/summary/position/?as_of={TODAY}&compare={D1}"
        ).content.decode("utf-8")
        self.assertIn("নিট অবস্থানের পরিবর্তন", html)

    # -- exports ----------------------------------------------------------
    def test_exports_are_real_files(self):
        self._txn(TransactionType.INCOME, "10000.00", D1)
        expected = {
            "pdf": (b"%PDF", "application/pdf"),
            "xlsx": (b"PK", "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet"),
            "docx": (b"PK", "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"),
        }
        for fmt, (magic, content_type) in expected.items():
            res = self.client.get(
                f"/summary/position/export/{fmt}/?as_of={D2}")
            self.assertEqual(res.status_code, 200, fmt)
            self.assertTrue(res.content.startswith(magic), fmt)
            self.assertEqual(res["Content-Type"], content_type)
            self.assertIn("attachment", res["Content-Disposition"])
            self.assertIn(f"position-{D2}", res["Content-Disposition"])

    def test_pdf_export_survives_audit_findings(self):
        # Regression: the audit block draws with multi_cell. If x is left
        # wherever the previous table stopped, fpdf2 raises "Not enough
        # horizontal space to render a single character".
        txn_id = self._txn(TransactionType.INCOME, "10000.00")
        JournalEntry.objects.filter(
            business=self.biz, source_kind="TXN",
            source_id=txn_id).delete()
        Customer.objects.create(
            business=self.biz, name="No Partner", status="ACTIVE")
        res = self.client.get("/summary/position/export/pdf/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.content.startswith(b"%PDF"))
        self.assertGreater(len(res.content), 1000)

    def test_low_severity_findings_do_not_get_a_green_badge(self):
        # A finding must never look like "all clear": only a clean audit
        # shows the green ok badge.
        Customer.objects.create(
            business=self.biz, name="No Partner", status="ACTIVE")
        html = self.client.get("/summary/position/").content.decode("utf-8")
        audit_html = html.split('id="ps-audit"', 1)[1].split('id="ps-health"', 1)[0]
        self.assertNotIn("ps-badge ok", audit_html)
        self.assertIn("ps-badge", audit_html)

    def test_export_data_mirrors_the_statement(self):
        from apps.web.position_export import build_position_export
        customer = self._customer("Hosen Ali")
        self._txn(TransactionType.INCOME, "1000.00", D1)
        self._txn(TransactionType.CREDIT, "4000.00", customer=customer)
        statement = self._statement()
        data = build_position_export(self.biz, statement, "bn")
        self.assertEqual(data["net"], statement["position"]["net"])
        self.assertEqual(data["due_total"], Decimal("4000.00"))
        self.assertEqual(data["due_groups"][0]["customers"][0]["name"],
                         "Hosen Ali")
        self.assertTrue(data["audit"]["clean"])
        self.assertTrue(data["filename"].startswith("position-"))
