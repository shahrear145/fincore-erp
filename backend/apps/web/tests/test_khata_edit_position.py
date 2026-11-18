"""বাকির খাতা — edit must not shuffle rows, and must not append notes.

Pinned rules:

- fixing an entry amount keeps every row exactly where it was — the row
  order is anchored to the earliest credit created that day (never to a
  reversed/re-posted one);
- the description of the corrected entry is left alone — no "আগে ৳X" /
  "সংশোধিত" is appended behind the user's back.
"""
from django.test import Client, TestCase

from apps.organizations.models import Business
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import TransactionType

D1 = "2026-11-10"


class KhataEditPositionTests(TestCase):
    def setUp(self):
        self.owner = make_user("khataeditowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Edit Position Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)
        self.client = Client()
        self.client.force_login(self.owner)
        session = self.client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()
        self.first = self._add("First Customer")
        self.second = self._add("Second Customer")
        self._txn("CREDIT", "100.00", self.first, "প্রথম এন্ট্রি")
        self._txn("CREDIT", "200.00", self.second, "দ্বিতীয় এন্ট্রি")

    def _add(self, name):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(self.partner.id)},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        from apps.customers.models import Customer
        return Customer.objects.get(pk=res.json()["id"])

    def _txn(self, txn_type, amount, customer, note=""):
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}",
            {"transaction_type": txn_type, "amount": amount,
             "transaction_date": D1,
             "partner": str(self.partner.id),
             "customer": str(customer.id),
             "payment_method": "CASH",
             "description": note},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()["id"]

    def _page(self):
        return self.client.get(
            f"/customers/?partner={self.partner.id}&pdate={D1}"
        ).content.decode("utf-8")

    def test_row_keeps_its_position_after_edit(self):
        page = self._page()
        self.assertLess(page.index("First Customer"),
                        page.index("Second Customer"))

        # fix the wrongly entered amount of the first customer's entry
        res = self.client.post("/customers/", {
            "action": "edit_tx_amount",
            "partner_id": str(self.partner.id),
            "tx_id": self._txn_id(),
            "pdate": D1,
            "new_amount": "300.00",
        })
        self.assertEqual(res.status_code, 302)

        page = self._page()
        self.assertLess(page.index("First Customer"),
                        page.index("Second Customer"))
        # description untouched by the fix
        self.assertIn("প্রথম এন্ট্রি", page)
        self.assertNotIn("আগে ৳", page)
        self.assertNotIn("সংশোধিত", page)

    def _txn_id(self):
        from apps.transactions.models import Transaction
        return Transaction.objects.filter(
            business=self.biz, partner=self.partner,
            customer=self.first, status="COMPLETED").latest("created_at").id
