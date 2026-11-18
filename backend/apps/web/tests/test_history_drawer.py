"""No-reload history drawer tests — পার্টনার ও বাকি page.

Pinned rules:

- `?partial=history` returns ONLY the drawer fragment (no full page shell);
- an unknown customer id or no resolvable partner answers 400 (the client
  then falls back to the old full-reload flow);
- partner-role users are scoped to their own partner book: naming another
  partner in the URL never leaks that partner's rows (server-enforced);
- REVERSED rows never appear in the history (ledger-safe view);
- the full page with ?history=... still server-renders the opened drawer
  for the no-JS fallback.
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


class HistoryDrawerTests(TestCase):
    def setUp(self):
        self.owner = make_user("hdowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "HD Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])

        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner,
        )
        self.other = Partner.objects.create(
            business=self.biz, name="Rahim", status="ACTIVE",
            created_by=self.owner,
        )

        self.customer = self._add_customer("Hosen Ali", self.partner)
        self.other_customer = self._add_customer("Salma Begum", self.other)

        # One due and one payment for the drawer rows; the due of the
        # assignment (created with the customer) becomes 3000 - 1000.
        self._txn("CREDIT", "3000.00", self.partner, self.customer)
        self._txn("PAYMENT", "1000.00", self.partner, self.customer)

        # A REVERSED credit must never appear in the history drawer.
        Transaction.objects.create(
            business=self.biz, transaction_type=TransactionType.CREDIT,
            amount=Decimal("999.00"), partner=self.partner,
            customer=self.customer, transaction_date=D1,
            status=TransactionStatus.REVERSED, created_by=self.owner,
        )

        self.client = Client()
        self.client.force_login(self.owner)
        self._activate(self.biz)

    def _add_customer(self, name, partner):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(partner.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return Customer.objects.get(pk=res.json()["id"])

    def _txn(self, txn_type, amount, partner, customer, txn_date=D1):
        res = auth_client(self.owner).post(
            f"/api/transactions/?business={self.biz.id}",
            {"transaction_type": txn_type, "amount": amount,
             "transaction_date": str(txn_date), "partner": str(partner.id),
             "customer": str(customer.id), "payment_method": "CASH"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res

    def _activate(self, biz):
        s = self.client.session
        s["active_business_id"] = str(biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _partial_url(self, partner=None, customer=None):
        partner = partner or self.partner
        customer = customer or self.customer
        return (f"/customers/?partner={partner.id}&history={customer.id}"
                f"&partial=history")

    # --- fragment contract -------------------------------------------------

    def test_partial_returns_fragment_only(self):
        res = self.client.get(self._partial_url())
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("Hosen Ali", body)
        self.assertIn("Karim", body)                    # partner book in header
        self.assertIn("৳3,000.00", body)                # due row
        self.assertIn("৳1,000.00", body)                # payment row
        self.assertIn("৳2,000.00", body)                # live current due
        self.assertNotIn("999.00", body)                # REVERSED excluded
        # no full-page shell leaks into the fragment
        self.assertNotIn("<!doctype", body.lower())
        self.assertNotIn("baki-form", body)
        self.assertNotIn("<nav", body.lower())

    def test_partial_unknown_customer_is_400(self):
        res = self.client.get(
            f"/customers/?partner={self.partner.id}&history=deadbeef"
            f"&partial=history")
        self.assertEqual(res.status_code, 400)

    def test_partial_without_partner_falls_back_to_first_book(self):
        # admins without a partner param land on the first partner's book
        # (same fallback the full page has always had)
        res = self.client.get(
            f"/customers/?history={self.customer.id}&partial=history")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Karim", res.content.decode())

    def test_partner_without_own_book_is_400(self):
        partner_user = make_user("hdnopartner")
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz,
            role=BusinessMembership.Role.PARTNER)
        client = Client()
        client.force_login(partner_user)
        s = client.session
        s["active_business_id"] = str(self.biz.id)
        s.save()
        res = client.get(self._partial_url())
        self.assertEqual(res.status_code, 400)

    def test_partial_requires_login(self):
        res = Client().get(self._partial_url())
        self.assertEqual(res.status_code, 302)
        self.assertIn("login", res["Location"])

    def test_full_page_still_renders_open_drawer(self):
        res = self.client.get(
            f"/customers/?partner={self.partner.id}&history={self.customer.id}"
            f"&pdate={D1.isoformat()}")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn("<!doctype", body.lower())
        self.assertIn('id="history-drawer"', body)
        self.assertIn("hd open", body)                  # opened server-side
        self.assertIn("Hosen Ali", body)
        self.assertIn('data-history-link', body)        # hook for the fetch JS

    def test_full_page_without_history_has_closed_drawer(self):
        res = self.client.get(
            f"/customers/?partner={self.partner.id}&pdate={D1.isoformat()}")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode()
        self.assertIn('id="history-drawer"', body)
        self.assertNotIn("hd open", body)
        self.assertIn("Hosen Ali", body)                # khata row still there
        self.assertIn('data-history-link', body)

    # --- scoping (books never mix) -----------------------------------------

    def test_partner_role_scoped_to_own_book(self):
        partner_user = make_user("hdpartner")
        BusinessMembership.objects.create(
            user=partner_user, business=self.biz, role=BusinessMembership.Role.PARTNER)
        linked = Partner.objects.create(
            business=self.biz, name="Own Book", status="ACTIVE",
            created_by=self.owner, linked_user=partner_user)

        client = Client()
        client.force_login(partner_user)
        s = client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

        # URL names the other partner + that partner's customer — nothing
        # may render: not the named partner's name, not their customer, no
        # rows (the client falls back to a clean full reload without drawer)
        res = client.get(
            f"/customers/?partner={self.other.id}&history={self.other_customer.id}"
            f"&partial=history")
        self.assertEqual(res.status_code, 400)
        self.assertNotIn("Rahim", res.content.decode())
        self.assertNotIn("Salma Begum", res.content.decode())

        # the partner's own book stays visible for their own customer link
        own_customer = self._add_customer("Nijer Customer", linked)
        res = client.get(
            f"/customers/?partner={self.other.id}&history={own_customer.id}"
            f"&partial=history")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Nijer Customer", res.content.decode())
