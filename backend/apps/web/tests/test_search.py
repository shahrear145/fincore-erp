"""Ctrl+K command palette — /search/ endpoint tests.

Pinned rules:

- pages match by Bengali/English label and keyword;
- customers match by name or phone; the linked URL opens their history;
- expenses match by description and by exact amount;
- an assistant-style command ("বাকি Hosen 500") yields one action item
  pointing at /assistant/?q=…&auto=1 (never posts a ledger row itself);
- VIEWER (read-only) gets no action items;
- anonymous users are redirected to login.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.accounting.postings import ensure_system_accounts
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionType


class SearchPaletteTests(TestCase):
    def setUp(self):
        self.owner = make_user("palowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Palette Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)
        self.partner = Partner.objects.create(
            business=self.biz, name="করিম মিয়া", status="ACTIVE",
            created_by=self.owner)
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": "Hosen Ali", "partner_id": str(self.partner.id),
             "phone": "01712345678", "email": ""},
            format="json")
        self.customer_id = res.json()["id"]

        Transaction.objects.create(
            business=self.biz, transaction_type=TransactionType.EXPENSE,
            amount=Decimal("1234.50"), transaction_date=date.today(),
            payment_method="CASH", description="চাল কেনা",
            expense_kind="GOODS", created_by=self.owner)

        self.client = Client()
        self.client.force_login(self.owner)
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _search(self, q):
        res = self.client.get(f"/search/?q={q}")
        self.assertEqual(res.status_code, 200)
        return res.json()

    def _group(self, data, key):
        for g in data.get("groups", []):
            if g["key"] == key:
                return g
        return None

    def test_anonymous_redirected(self):
        res = Client().get("/search/?q=hosen")
        self.assertEqual(res.status_code, 302)

    def test_empty_query_returns_hints(self):
        data = self._search("")
        self.assertIn("hints", [g["key"] for g in data["groups"]])

    def test_pages_match_bengali_and_english(self):
        data = self._search("রিপোর্ট")
        g = self._group(data, "pages")
        self.assertIsNotNone(g)
        self.assertTrue(any(i["url"] == "/reports/" for i in g["items"]))
        data = self._search("notification")
        g = self._group(data, "pages")
        self.assertTrue(any(i["url"] == "/notifications/" for i in g["items"]))

    def test_customers_match_by_name_and_open_history(self):
        data = self._search("Hosen")
        g = self._group(data, "customers")
        self.assertIsNotNone(g)
        names = [i["title"] for i in g["items"]]
        self.assertIn("Hosen Ali", names)
        url = next(i["url"] for i in g["items"] if i["title"] == "Hosen Ali")
        self.assertIn(f"history={self.customer_id}", url)
        self.assertIn("partner=", url)

    def test_customers_match_by_phone(self):
        data = self._search("01712345678")
        g = self._group(data, "customers")
        self.assertIsNotNone(g)
        self.assertTrue(any(i["title"] == "Hosen Ali" for i in g["items"]))

    def test_partners_match(self):
        data = self._search("করিম")
        g = self._group(data, "partners")
        self.assertIsNotNone(g)
        self.assertTrue(any("করিম" in i["title"] for i in g["items"]))

    def test_transactions_match_by_description(self):
        data = self._search("চাল")
        g = self._group(data, "transactions")
        self.assertIsNotNone(g)
        self.assertIn("1,234.50", g["items"][0]["title"])

    def test_transactions_match_by_amount(self):
        data = self._search("1234.50")
        g = self._group(data, "transactions")
        self.assertIsNotNone(g)
        self.assertIn("1,234.50", g["items"][0]["title"])

    def test_command_yields_single_action_item(self):
        data = self._search("বাকি Hosen 500")
        g = self._group(data, "actions")
        self.assertIsNotNone(g)
        item = g["items"][0]
        self.assertEqual(item["action"], "assistant")
        self.assertEqual(item["question"], "বাকি Hosen 500")
        self.assertTrue(item["url"].startswith("/assistant/?q="))
        self.assertTrue(item["url"].endswith("auto=1"))

    def test_viewer_gets_no_action_items(self):
        viewer = make_user("palviewer", "palviewer@example.com")
        BusinessMembership.objects.create(
            user=viewer, business=self.biz,
            role=BusinessMembership.Role.VIEWER, is_active=True)
        c = Client()
        c.force_login(viewer)
        s = c.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()
        data = c.get("/search/?q=বাকি Hosen 500").json()
        self.assertIsNone(self._group(data, "actions"))
        # pages still work for read-only users
        data = c.get("/search/?q=রিপোর্ট").json()
        self.assertIsNotNone(self._group(data, "pages"))
