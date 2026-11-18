"""বাকি চালান (customer statement) & পরিশোধ রশিকা (money receipt) PDF tests.

Pinned rules:

- statement PDF downloads for one customer *assigned to the partner's book*;
  a customer of another partner never leaks into the URL;
- and the receipt PDF is only produced for PAYMENT transactions;
- partner-role users are scoped to their own partner book;
- reversed payments / reversed dues never generate a receipt;
- the PDFs are valid binary responses (application/pdf, ``%PDF`` magic).
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.customers.models import Customer
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionStatus, TransactionType

D1 = date(2026, 11, 9)
D2 = date(2026, 11, 10)


class ReceiptPdfTests(TestCase):
    def setUp(self):
        self.owner = make_user("recptowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Receipt Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)
        self.other = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner)

        self.customer = self._add_customer("Hosen Ali", self.partner)
        self.other_customer = self._add_customer("Salma Begum", self.other)

        self._txn("CREDIT", "3000.00", self.partner, self.customer, D1)
        self._txn("PAYMENT", "1000.00", self.partner, self.customer, D2)

        self.client = Client()
        self.client.force_login(self.owner)
        self._activate(self.biz)

    # -- helpers ---------------------------------------------------------
    def _activate(self, biz):
        s = self.client.session
        s["active_business_id"] = str(biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _add_customer(self, name, partner):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(partner.id)},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return Customer.objects.get(pk=res.json()["id"])

    def _txn(self, txn_type, amount, partner, customer, tx_date=D1):
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}",
            {"transaction_type": txn_type, "amount": amount,
             "transaction_date": tx_date.isoformat(),
             "partner": str(partner.id),
             "customer": str(customer.id),
             "payment_method": "CASH",
             "description": ""},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res

    # -- statement --------------------------------------------------------
    def test_statement_downloads_a_pdf_for_own_customer(self):
        res = self.client.get(
            f"/customers/statement/{self.customer.id}/?partner={self.partner.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertTrue(res.content[:4] == b"%PDF")
        self.assertGreater(len(res.content), 800)

    def test_statement_for_other_partners_customer_is_404(self):
        """Naming another partner's customer must never render their book."""
        res = self.client.get(
            f"/customers/statement/{self.other_customer.id}/?partner={self.partner.id}")
        self.assertEqual(res.status_code, 404)

    def test_statement_unknown_customer_is_404(self):
        res = self.client.get(
            f"/customers/statement/00000000-0000-0000-0000-000000000000/?partner={self.partner.id}")
        self.assertEqual(res.status_code, 404)

    # -- receipt ----------------------------------------------------------
    def test_receipt_downloads_a_pdf_for_a_payment(self):
        payment = Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.PAYMENT,
            customer=self.customer).first()
        self.assertIsNotNone(payment)
        res = self.client.get(f"/customers/receipt/{payment.id}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertTrue(res.content[:4] == b"%PDF")

    def test_receipt_for_a_due_is_not_a_receipt(self):
        due = Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.CREDIT,
            customer=self.customer).first()
        self.assertIsNotNone(due)
        res = self.client.get(f"/customers/receipt/{due.id}/")
        self.assertEqual(res.status_code, 404)

    def test_reversed_payment_has_no_receipt(self):
        payment = Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.PAYMENT,
            customer=self.customer).first()
        payment.status = TransactionStatus.REVERSED
        payment.save(update_fields=["status"])
        res = self.client.get(f"/customers/receipt/{payment.id}/")
        self.assertEqual(res.status_code, 404)

    # -- partner role scoping --------------------------------------------
    def test_partner_role_sees_only_own_book(self):
        partner_user = make_user("partnerbob")
        self.other.linked_user = partner_user
        self.other.save(update_fields=["linked_user"])
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz,
            role=BusinessMembership.Role.PARTNER,
            is_active=True)

        client = Client()
        client.force_login(partner_user)
        session = client.session
        session["active_business_id"] = str(self.biz.id)
        session.save()

        # own book: the other partner's customer is accepted
        res = client.get(f"/customers/statement/{self.other_customer.id}/")
        self.assertEqual(res.status_code, 200)

        # but naming the first partner's book is forbidden
        res = client.get(
            f"/customers/statement/{self.customer.id}/?partner={self.partner.id}")
        self.assertEqual(res.status_code, 403)

    # -- bengali words ----------------------------------------------------
    def test_bengali_amount_words_helper(self):
        from decimal import Decimal
        from apps.web.receipt_exports import to_bengali_words
        self.assertEqual(to_bengali_words(Decimal("0")), "শূন্য টাকা")
        self.assertEqual(to_bengali_words(Decimal("1250")), "এক হাজার দুই শত পঞ্চাশ টাকা")
        self.assertEqual(to_bengali_words(Decimal("100.50")), "এক শত টাকা পঞ্চাশ পয়সা")
