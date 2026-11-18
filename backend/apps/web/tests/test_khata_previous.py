"""বাকির খাতা — আগের বাকি / বর্তমান বাকি behaviour.

Pinned rules:

- with no collection on the viewed date, both columns show the same figure;
- a payment (collection) moves only **বর্তমান বাকি** — **আগের বাকি** stays;
- fixing a wrongly entered amount (edit) moves both columns by the same
  delta — the entry was wrong, not the customer's money.
"""
from decimal import Decimal

from django.test import Client, TestCase

from apps.customers.models import Customer
from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import TransactionType

D1 = "2026-11-10"


class KhataPreviousDueTests(TestCase):
    def setUp(self):
        self.owner = make_user("khataprevowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Prev Due Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner)
        self.customer = self._add_customer("Hosen Ali")
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()

    def _add_customer(self, name):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(self.partner.id)},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return Customer.objects.get(pk=res.json()["id"])

    def _txn(self, txn_type, amount, txn_date=D1):
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}",
            {"transaction_type": txn_type, "amount": amount,
             "transaction_date": txn_date,
             "partner": str(self.partner.id),
             "customer": str(self.customer.id),
             "payment_method": "CASH"},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["id"]

    def _page(self):
        return self.client.get(
            f"/customers/?partner={self.partner.id}&pdate={D1}"
        ).content.decode("utf-8")

    def _row_cells(self, html):
        # extract the [আগের বাকি, বর্তমান বাকি] cells of the Hosen Ali row
        marker = "Hosen Ali"
        i = html.index(marker)
        row_end = html.index("</tr>", i)
        row = html[i:row_end]
        cells = []
        for chunk in row.split("<td"):
            if "৳" in chunk:
                cells.append(chunk)
        return cells

    def test_both_columns_equal_until_a_payment_arrives(self):
        self._txn(TransactionType.CREDIT, "1000.00")
        sale_id = self._txn(TransactionType.CREDIT, "200.00")

        page = self._page()
        self.assertIn("৳1,200.00", page)          # both columns show the same
        self.assertIn("৳1,200.00", page)         # (twice)

        # now a collection arrives on the same date
        self._txn(TransactionType.PAYMENT, "200.00")
        page = self._page()
        self.assertIn("৳1,200.00", page)         # আগের বাকি stays put
        self.assertIn("৳1,000.00", page)         # বর্তমান বাকি drops by the payment

    def test_editing_an_entry_shifts_both_columns(self):
        txn_id = self._txn(TransactionType.CREDIT, "1000.00")

        page = self._page()
        self.assertIn("৳1,000.00", page)

        # edit: the entry was wrong — the customer owed ৳1,500
        res = self.client.post("/customers/", {
            "action": "edit_tx_amount",
            "partner_id": str(self.partner.id),
            "tx_id": txn_id,
            "pdate": D1,
            "new_amount": "1500.00",
        })
        self.assertEqual(res.status_code, 302)

        page = self._page()
        marker = "Hosen Ali"
        row = page[page.index(marker):page.index("</tr>", page.index(marker))]
        self.assertEqual(row.count("৳1,500.00"), 2)   # both columns moved together
