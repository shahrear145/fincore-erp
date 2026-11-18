"""টিম পারমিশন (#10) — Owner / Accountant / Viewer.

Pinned rules:

- the owner adds an existing registered user as ACCOUNTANT or VIEWER from
  /business/users/ — the action is notified to the new member;
- the owner cannot remove/demote the last owner or change his own role;
- ACCOUNTANT can post entries (বাকি, expense) but cannot hard-delete /
  reverse them; VIEWER can't post anything;
- /business/users/ is hidden to non-owners;
- TEAM_UPDATE lightening walks the change into /notifications/.
"""
from datetime import date
from decimal import Decimal

from django.test import Client, TestCase

from apps.customers.models import Customer
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user
from apps.transactions.models import Transaction, TransactionType
from apps.web.models import Notification

D1 = date(2026, 11, 9)


class TeamPermissionTests(TestCase):
    def setUp(self):
        self.owner = make_user("teamowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Permission Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])

        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
            created_by=self.owner)
        self.customer = self._add_customer("Hosen Ali", "01712345678",
                                           "hosen@example.com")
        self.accountant = make_user("teamacc", "acc@example.com")
        self.viewer = make_user("teamview", "view@example.com")

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

    def _client_for(self, user):
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_business_id"] = str(self.biz.id)
        session["ui_lang"] = "bn"
        session.save()
        return client

    def _member(self, user, role):
        BusinessMembership.objects.filter(
            business=self.biz, user=user).update(role=role)

    def _add_member(self, user, role):
        return self.client.post("/business/users/", {
            "action": "add_member",
            "member": user.username,
            "role": role,
        })

    def _post_credit_as(self, client, amount="1000.00"):
        client.post("/customers/", {
            "action": "add_baki",
            "partner_id": str(self.partner.id),
            "customer_id": str(self.customer.id),
            "transaction_date": D1.isoformat(),
            "name": "Hosen Ali",
            "phone": "01712345678",
            "email": "hosen@example.com",
            "amount": amount,
            "transaction_type": "CREDIT",
            "description": "due",
        })

    # -- owner adds team members -------------------------------------------
    def test_owner_adds_accountant_and_viewer(self):
        self._add_member(self.accountant, "ACCOUNTANT")
        m = BusinessMembership.objects.get(business=self.biz, user=self.accountant)
        self.assertEqual(m.role, BusinessMembership.Role.ACCOUNTANT)
        self.assertTrue(m.is_active)

        self._add_member(self.viewer, "VIEWER")
        m2 = BusinessMembership.objects.get(business=self.biz, user=self.viewer)
        self.assertEqual(m2.role, BusinessMembership.Role.VIEWER)

        notes = Notification.objects.filter(
            business=self.biz, kind=Notification.Kind.TEAM_UPDATE)
        self.assertEqual(notes.count(), 2)

    def test_owner_removes_someone_and_notifies_them(self):
        self._add_member(self.accountant, "ACCOUNTANT")
        self.client.post("/business/users/", {
            "action": "remove_member",
            "user_id": str(self.accountant.pk),
        })
        m = BusinessMembership.objects.get(business=self.biz, user=self.accountant)
        self.assertFalse(m.is_active)
        note = Notification.objects.filter(
            business=self.biz, recipient=self.accountant,
            kind=Notification.Kind.TEAM_UPDATE).first()
        self.assertIsNotNone(note)

    def test_owner_cannot_remove_self(self):
        self.client.post("/business/users/", {
            "action": "remove_member",
            "user_id": str(self.owner.pk),
        })
        m = BusinessMembership.objects.get(business=self.biz, user=self.owner)
        self.assertTrue(m.is_active)

    # -- viewer is read-only ------------------------------------------------
    def test_viewer_cannot_add_baki(self):
        self._add_member(self.viewer, "VIEWER")
        vc = self._client_for(self.viewer)
        self._post_credit_as(vc)
        self.assertEqual(Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.CREDIT).count(), 0)

    def test_accountant_can_add_baki(self):
        self._add_member(self.accountant, "ACCOUNTANT")
        ac = self._client_for(self.accountant)
        self._post_credit_as(ac, "750.00")
        self.assertTrue(Transaction.objects.filter(
            business=self.biz, amount=Decimal("750.00")).exists())

    def test_accountant_cannot_delete_expense(self):
        self._add_member(self.accountant, "ACCOUNTANT")
        # owner creates the expense first
        self.client.post("/expenses/", {
            "action": "add_expense",
            "name": "চাল",
            "amount": "300.00",
            "transaction_date": D1.isoformat(),
            "description": "",
            "expense_kind": "OPERATING",
        })
        txn = Transaction.objects.filter(
            business=self.biz, transaction_type=TransactionType.EXPENSE).first()
        self.assertIsNotNone(txn)

        ac = self._client_for(self.accountant)
        ac.post("/expenses/", {
            "action": "delete_expense",
            "tx_id": str(txn.pk),
            "reason": "test",
        })
        txn.refresh_from_db()
        self.assertEqual(txn.status, "COMPLETED")

    # -- page scope ---------------------------------------------------------
    def test_non_owner_cannot_open_business_users_page(self):
        self._add_member(self.accountant, "ACCOUNTANT")
        ac = self._client_for(self.accountant)
        res = ac.get("/business/users/")
        self.assertRedirects(res, "/")
        res = ac.post("/business/users/", {"action": "remove_member"})
        self.assertRedirects(res, "/")

    def test_owner_can_promote_someone_to_owner(self):
        self._add_member(self.accountant, "ACCOUNTANT")
        self.client.post("/business/users/", {
            "action": "set_role",
            "user_id": str(self.accountant.pk),
            "role": "ADMIN",
        })
        m = BusinessMembership.objects.get(business=self.biz, user=self.accountant)
        self.assertEqual(m.role, BusinessMembership.Role.ADMIN)
