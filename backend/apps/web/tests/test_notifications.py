"""নোটিফিকেশন সেন্টার tests.

Pinned rules:

- an admin's কাটিং the বাকি (a CREDIT entry posted through the web UI) fans a
  DUE_INCREASED notification out to every admin of the business;
- an email reminder that really goes out fires a REMINDER_SENT notification
  with the customer linked (amount, partner, name);
- posting an expense that pushes a category over its monthly budget fires
  exactly one BUDGET_OVER notification per month (no spam);
- the /notifications/ page, drawer fragment, mark-read and mark-all-read
  endpoints behave and are login-scoped to the active business;
- the service helpers: timeline grouping, rel_time, kind metadata and
  merge_sort all return sane, stable values.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import Client, TestCase

from apps.customers.models import Customer, CustomerAssignment
from apps.organizations.models import Business, BusinessMembership
from apps.partners.models import Partner
from apps.accounting.postings import ensure_system_accounts
from apps.tests_common import auth_client, make_user
from apps.transactions.models import MonthlyBudget, Transaction, TransactionType
from apps.web.models import Notification
from apps.web import notifications as notif

D1 = date(2026, 11, 9)


class NotificationTests(TestCase):
    def setUp(self):
        self.owner = make_user("notifowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Notification Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])

        self.partner = Partner.objects.create(
            business=self.biz, name="Karim", status="ACTIVE",
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

    def _post_credit(self, amount="1000.00", name="Hosen Ali"):
        self.client.post("/customers/", {
            "action": "add_baki",
            "partner_id": str(self.partner.id),
            "customer_id": str(self.customer.id),
            "transaction_date": D1.isoformat(),
            "name": name,
            "phone": "01712345678",
            "email": "hosen@example.com",
            "amount": amount,
            "transaction_type": "CREDIT",
            "description": "due",
        })

    def _assignment(self):
        return CustomerAssignment.objects.get(
            customer=self.customer, partner=self.partner)

    # -- বাকি hook ---------------------------------------------------------
    def test_credit_due_fans_out_to_the_admin(self):
        self._post_credit()
        note = Notification.objects.filter(
            business=self.biz, recipient=self.owner,
            kind=Notification.Kind.DUE_INCREASED)
        self.assertEqual(note.count(), 1)
        n = note.get()
        self.assertIn("Hosen Ali", n.title)
        self.assertEqual(n.amount, Decimal("1000.00"))
        self.assertIn("history=" + str(self.customer.id), n.link)
        self.assertFalse(n.is_read)

    def test_payment_does_not_create_a_baki_notification(self):
        self.client.post("/customers/", {
            "action": "add_baki",
            "partner_id": str(self.partner.id),
            "transaction_date": D1.isoformat(),
            "name": "Hosen Ali",
            "phone": "01712345678",
            "amount": "500.00",
            "transaction_type": "PAYMENT",
            "description": "pay",
        })
        self.assertEqual(Notification.objects.filter(
            kind=Notification.Kind.DUE_INCREASED).count(), 0)

    # -- reminder hook ------------------------------------------------------
    def test_email_reminder_creates_a_notification(self):
        self._post_credit("3000.00")
        assignment = self._assignment()
        res = self.client.post(
            f"/customers/reminder/{assignment.id}/send/",
            {"channel": "EMAIL", "confirm_repeat": "1"})
        self.assertEqual(res.status_code, 200)
        note = Notification.objects.filter(
            business=self.biz, kind=Notification.Kind.REMINDER_SENT)
        self.assertEqual(note.count(), 1)
        n = note.get()
        self.assertIn("Hosen Ali", n.title)
        self.assertIn("email", n.body.lower())
        self.assertIn("history=" + str(self.customer.id), n.link)
        self.assertEqual(n.amount, Decimal("3000.00"))

    # -- budget hook ---------------------------------------------------------
    def test_expense_over_budget_creates_one_notification(self):
        month = date.today().replace(day=1)
        MonthlyBudget.objects.create(
            business=self.biz, category="OPERATING", month=month,
            limit_amount=Decimal("100.00"))
        self.client.post("/expenses/", {
            "action": "add_expense",
            "name": "চাল",
            "amount": "200.00",
            "transaction_date": date.today().isoformat(),
            "description": "",
            "expense_kind": "OPERATING",
        })
        notes = Notification.objects.filter(
            business=self.biz, kind=Notification.Kind.BUDGET_OVER)
        self.assertEqual(notes.count(), 1)
        self.assertIn("দোকান", notes.get().title)
        # a second expense does NOT spam a new notification the same month
        self.client.post("/expenses/", {
            "action": "add_expense",
            "name": "আটা",
            "amount": "100.00",
            "transaction_date": date.today().isoformat(),
            "description": "",
            "expense_kind": "OPERATING",
        })
        self.assertEqual(Notification.objects.filter(
            business=self.biz, kind=Notification.Kind.BUDGET_OVER).count(), 1)

    # -- page, drawer, read endpoints ----------------------------------------
    def test_notifications_page_requires_login_redirects(self):
        res = Client().get("/notifications/")
        self.assertEqual(res.status_code, 302)

    def test_drawer_requires_login(self):
        res = Client().get("/notifications/drawer/")
        self.assertEqual(res.status_code, 302)

    def test_mark_read_endpoint(self):
        self._post_credit()
        n = Notification.objects.get(kind=Notification.Kind.DUE_INCREASED)
        res = self.client.post(f"/notifications/read/{n.id}/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertEqual(res.json()["unread"], 0)
        n.refresh_from_db()
        self.assertTrue(n.is_read)

    def test_mark_all_read_endpoint(self):
        self._post_credit()
        self._post_credit(amount="500.00", name="Other name")
        res = self.client.post("/notifications/read-all/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertEqual(res.json()["unread"], 0)

    def test_drawer_fragment_renders_items(self):
        self._post_credit()
        res = self.client.get("/notifications/drawer/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn("Hosen Ali", body)
        self.assertIn("বাকি বাড়ল", body)

    def test_notifications_page_renders(self):
        self._post_credit()
        res = self.client.get("/notifications/")
        self.assertEqual(res.status_code, 200)
        body = res.content.decode("utf-8")
        self.assertIn("নোটিফিকেশন", body)
        self.assertIn("Hosen Ali", body)

    # -- service helpers -------------------------------------------------------
    def test_timeline_groups_by_day(self):
        notif.notify_admins(self.biz, kind=Notification.Kind.DUE_INCREASED,
                            title="n1")
        groups = notif.timeline(self.biz, self.owner)
        self.assertTrue(groups)
        labels = [g["label"] for g in groups]
        self.assertIn("আজ", labels)
        self.assertEqual(notif.unread_count(self.biz, self.owner), 1)

    def test_merge_sort_orders_danger_first(self):
        alerts = [
            {"kind": "X", "tone": "info", "title": "info"},
            {"kind": "X", "tone": "danger", "title": "danger"},
            {"kind": "X", "tone": "warn", "title": "warn"},
        ]
        merged = notif.merge_sort(alerts)
        self.assertEqual([a["tone"] for a in merged],
                         ["danger", "warn", "info"])

    def test_rel_time(self):
        from django.utils import timezone
        just = notif.rel_time(timezone.now())
        self.assertEqual(just, "এইমাত্র")

    def test_live_alerts(self):
        month = date.today().replace(day=1)
        budget = MonthlyBudget.objects.create(
            business=self.biz, category="OPERATING", month=month,
            limit_amount=Decimal("100.00"))
        # re-spend through the ORM to be deterministic
        Transaction.objects.create(
            business=self.biz, transaction_type=TransactionType.EXPENSE,
            amount=Decimal("250.00"), transaction_date=date.today(),
            payment_method="CASH", description="চাল",
            expense_kind="OPERATING", created_by=self.owner)
        alerts = notif.live_alerts(self.biz)
        self.assertTrue(any(a["kind"] == "BUDGET_OVER" and
                            a["tone"] == "warn" and
                            a["over"] for a in alerts))


class NotificationClearTests(TestCase):
    """The /notifications/clear/ endpoint (#১২ পরিষ্কার feature)."""

    def setUp(self):
        self.owner = make_user("clearowner")
        res = auth_client(self.owner).post(
            "/api/businesses/", {"name": "Clear Traders"}, format="json")
        self.biz = Business.objects.get(pk=res.json()["id"])
        ensure_system_accounts(self.biz, self.owner)

        self.other = make_user("clearother", "clearother@example.com")
        BusinessMembership.objects.create(
            user=self.other, business=self.biz,
            role=BusinessMembership.Role.VIEWER, is_active=True)

        self.client = Client()
        self.client.force_login(self.owner)
        s = self.client.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()

    def _make_note(self, owner, title):
        return Notification.objects.create(
            business=self.biz, recipient=owner,
            kind=Notification.Kind.DUE_INCREASED,
            title=title, body="টেস্ট বার্তা")

    def _client_for(self, user):
        c = Client()
        c.force_login(user)
        s = c.session
        s["active_business_id"] = str(self.biz.id)
        s["ui_lang"] = "bn"
        s.save()
        return c

    def test_clear_deletes_my_notifications(self):
        self._make_note(self.owner, "নোট ১")
        self._make_note(self.owner, "নোট ২")
        self._make_note(self.other, "অন্যের নোট")

        res = self.client.post("/notifications/clear/")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted"], 2)
        self.assertEqual(body["unread"], 0)
        # mine are gone; the other member's remain
        self.assertFalse(
            Notification.objects.filter(recipient=self.owner).exists())
        self.assertTrue(
            Notification.objects.filter(recipient=self.other).exists())

    def test_clear_requires_post(self):
        res = self.client.get("/notifications/clear/")
        self.assertEqual(res.status_code, 405)

    def test_clear_requires_login(self):
        res = Client().post("/notifications/clear/")
        self.assertEqual(res.status_code, 302)

    def test_drawer_after_clear_shows_empty_state(self):
        self._make_note(self.owner, "নোট X")
        self.client.post("/notifications/clear/")
        res = self.client.get("/notifications/drawer/")
        self.assertEqual(res.status_code, 200)
        # stored events are gone; live pane renders fine regardless
        self.assertNotIn("নোট X", res.content.decode("utf-8"))

    def test_live_alerts_still_returned_after_clear(self):
        budget = MonthlyBudget.objects.create(
            business=self.biz, category="OPERATING",
            month=date.today().replace(day=1),
            limit_amount=Decimal("100.00"))
        Transaction.objects.create(
            business=self.biz, transaction_type=TransactionType.EXPENSE,
            amount=Decimal("250.00"), transaction_date=date.today(),
            payment_method="CASH", description="ওভার",
            expense_kind="OPERATING", created_by=self.owner)
        self.client.post("/notifications/clear/")
        res = self.client.get("/notifications/drawer/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("বাজেট শেষ", res.content.decode("utf-8"))
