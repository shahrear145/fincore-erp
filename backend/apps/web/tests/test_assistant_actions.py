"""অ্যাসিস্ট্যান্ট অ্যাকশন (#১১) — propose → confirm → post.

Pinned rules:

- asking a command (e.g. "বাকি Hosen 500") creates a PROPOSED
  AssistantAction and posts NO transaction yet;
- confirming the proposal posts the transaction through the one
  validated writer and links it on the action;
- discarding the proposal posts NO transaction at all;
- ambiguous / unknown customers produce helpful errors, not actions;
- normal questions still route to the Q&A engine (no action created);
- non-admins (VIEWER) cannot post actions; ACCOUNTANTs can.
"""
import re
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.accounting.postings import ensure_system_accounts
from apps.assistant.models import AssistantAction
from apps.customers.models import Customer
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionType

D1 = date(2026, 11, 9)


class AssistantActionTests(TestCase):
    def setUp(self):
        self.owner = make_user("assistowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Assistant Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)

        self.partner = Partner.objects.create(
            business=self.biz, name="করিম", status="ACTIVE",
            created_by=self.owner)
        self.customer = self._add_customer("Hosen Ali", "01712345678",
                                           "hosen@example.com")
        self.client = Client()
        self.client.force_login(self.owner)
        self._activate()

    # -- helpers ---------------------------------------------------------
    def _activate(self):
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _add_customer(self, name, phone, email):
        res = auth_client(self.owner).post(
            f"/api/customers/?business={self.biz.id}",
            {"name": name, "partner_id": str(self.partner.id),
             "phone": phone, "email": email},
            format="json")
        self.assertEqual(res.status_code, 201, res.content)
        cust = Customer.objects.get(pk=res.json()["id"])
        cust.phone = phone
        cust.email = email
        cust.save(update_fields=["phone", "email"])
        return cust

    def _find_action(self):
        return AssistantAction.objects.filter(
            user=self.owner, business=self.biz).order_by("-created_at").first()

    def _post(self, question):
        return self.client.post("/assistant/", {"question": question})

    def _confirm(self, action_id, kind="confirm_action"):
        return self.client.post("/assistant/", {
            "action": kind,
            "action_id": str(action_id),
        })

    # -- propose + confirm -------------------------------------------------
    def test_baki_command_creates_proposal_but_posts_nothing_yet(self):
        res = self._post("বাকি Hosen 500")
        self.assertEqual(res.status_code, 302)
        action = self._find_action()
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, AssistantAction.Kind.ADD_BAKI)
        self.assertEqual(action.amount, Decimal("500.00"))
        self.assertEqual(action.status, AssistantAction.Status.PROPOSED)
        self.assertEqual(action.customer_id, self.customer.id)
        self.assertEqual(action.partner_id, self.partner.id)
        # nothing posted yet
        self.assertEqual(Transaction.objects.filter(
            business=self.biz).count(), 0)

    def test_confirm_baki_posts_the_transaction(self):
        self._post("বাকি Hosen 750")
        action = self._find_action()
        res = self._confirm(action.id)
        self.assertEqual(res.status_code, 302)
        action.refresh_from_db()
        self.assertEqual(action.status, AssistantAction.Status.DONE)
        self.assertIsNotNone(action.transaction_id)
        txn = action.transaction
        self.assertEqual(txn.amount, Decimal("750.00"))
        self.assertEqual(txn.transaction_type, TransactionType.CREDIT)

    def test_add_expense_proposal_then_confirm(self):
        res = self._post("খরচ 800 চাল")
        self.assertEqual(res.status_code, 302)
        action = self._find_action()
        self.assertEqual(action.kind, AssistantAction.Kind.ADD_EXPENSE)
        self.assertIn("চাল", action.description)

        res = self._confirm(action.id)
        action.refresh_from_db()
        self.assertEqual(action.status, AssistantAction.Status.DONE)
        self.assertIsNotNone(action.transaction_id)

    def test_discard_posts_nothing(self):
        res = self._post("বাকি Hosen 100")
        action = self._find_action()
        res = self._confirm(action.id, kind="discard_action")
        self.assertEqual(res.status_code, 302)
        action.refresh_from_db()
        self.assertEqual(action.status, AssistantAction.Status.DISCARDED)
        self.assertEqual(Transaction.objects.filter(
            business=self.biz).count(), 0)

    # -- command parse failures ----------------------------------------------
    def test_unknown_customer_shows_helpful_error(self):
        res = self._post("বাকি ঝাকানাকা 500")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn("ঝাকানাকা", body)
        self.assertIn("পাওয়া যায়নি", body)

    def test_missing_amount_rejected(self):
        res = self._post("বাকি Hosen")
        body = res.content.decode("utf-8")
        self.assertIn("টাকার অঙ্ক", body)

    def test_non_command_question_still_goes_to_qa_engine(self):
        res = self._post("How much do customers owe me?")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Transaction.objects.filter(
            business=self.biz).count(), 0)
        self.assertEqual(AssistantAction.objects.filter(
            business=self.biz).count(), 0)

    # -- permissions -----------------------------------------------------------
    def test_viewer_cannot_create_entries(self):
        viewer = make_user("assistantviewer", "viewer@example.com")
        BusinessMembership.objects.create(
            user=viewer, business=self.biz,
            role=BusinessMembership.Role.VIEWER, is_active=True)
        client = Client()
        client.force_login(viewer)
        session = client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()
        res = client.post("/assistant/", {"question": "বাকি Hosen 500"})
        # viewer gets no proposal — it sees no action even for valid commands
        action = AssistantAction.objects.filter(
            business=self.biz, user=viewer).first()
        self.assertIsNone(action)
