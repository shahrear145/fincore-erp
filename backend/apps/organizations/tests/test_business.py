"""Business + membership + financial period tests (Phase 1 scope)."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tests_common import auth_client, make_user


class BusinessTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.partner_user = make_user("partneru")
        self.stranger = make_user("stranger")
        self.client = APIClient()

    def _create_business(self, user, name="My Shop"):
        client = auth_client(user)
        res = client.post("/api/businesses/", {"name": name}, format="json")
        return res

    def test_create_business_makes_owner_admin(self):
        res = self._create_business(self.owner)
        self.assertEqual(res.status_code, 201)
        from apps.organizations.models import BusinessMembership
        membership = BusinessMembership.objects.get(business_id=res.data["id"])
        self.assertEqual(membership.role, BusinessMembership.Role.ADMIN)
        self.assertEqual(membership.user, self.owner)

    def test_multiple_businesses_supported(self):
        r1 = self._create_business(self.owner, "One")
        r2 = self._create_business(self.owner, "Two")
        self.assertEqual(r1.status_code, r2.status_code, 201)
        client = auth_client(self.owner)
        mine = client.get("/api/businesses/mine/")
        self.assertEqual(len(mine.data), 2)

    def test_stranger_cannot_see_business_members(self):
        biz = self._create_business(self.owner)
        biz_id = biz.data["id"]
        res = auth_client(self.stranger).get(f"/api/businesses/{biz_id}/members/")
        self.assertEqual(res.status_code, 403)

    def test_admin_adds_partner_member_and_permission_change_audited(self):
        biz = self._create_business(self.owner)
        biz_id = biz.data["id"]
        res = auth_client(self.owner).post(
            f"/api/businesses/{biz_id}/members/",
            {"email": self.partner_user.email, "role": "PARTNER"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["role"], "PARTNER")
        from apps.organizations.models import BusinessMembership
        ms = BusinessMembership.objects.get(business_id=biz_id, user=self.partner_user)
        self.assertEqual(
            ms.partner_permissions["can_edit_other_partners_dues"], False
        )

    def test_partner_member_cannot_add_members(self):
        biz = self._create_business(self.owner)
        biz_id = biz.data["id"]
        auth_client(self.owner).post(
            f"/api/businesses/{biz_id}/members/",
            {"email": self.partner_user.email, "role": "PARTNER"}, format="json",
        )
        res = auth_client(self.partner_user).post(
            f"/api/businesses/{biz_id}/members/",
            {"email": self.stranger.email, "role": "PARTNER"}, format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_period_locking_roundtrip(self):
        biz = self._create_business(self.owner)
        biz_id = biz.data["id"]
        c = auth_client(self.owner)
        period = c.post(f"/api/businesses/{biz_id}/periods/", {
            "name": "2026-09", "start_date": "2026-09-01", "end_date": "2026-09-30",
        }, format="json")
        self.assertEqual(period.status_code, 201)
        lock = c.post(
            f"/api/businesses/{biz_id}/periods/{period.data['id']}/lock/",
            {"is_locked": True, "reason": "Month-end close"}, format="json",
        )
        self.assertEqual(lock.status_code, 200)
        self.assertTrue(lock.data["is_locked"])
        from apps.organizations.models import FinancialPeriod
        from rest_framework.exceptions import PermissionDenied
        p = FinancialPeriod.objects.get(id=period.data["id"])
        self.assertEqual(FinancialPeriod.period_for_date(p.business, "2026-09-15"), p)
        with self.assertRaises(PermissionDenied):
            FinancialPeriod.assert_date_not_locked(p.business, "2026-09-15")

    def test_period_validation_rejects_bad_range(self):
        biz = self._create_business(self.owner)
        res = auth_client(self.owner).post(
            f"/api/businesses/{biz.data['id']}/periods/",
            {"name": "bad", "start_date": "2026-09-10", "end_date": "2026-09-01"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)


class OwnerMembershipRepairTests(TestCase):
    """A business whose owner has no membership is invisible in the web UI.

    Every page resolves the active business through BusinessMembership, so the
    owner sees an empty app and all partners/customers/transactions look gone.
    The web form always creates the membership; admin/shell/import paths did
    not, hence the repair migration plus the BusinessAdmin hook.
    """

    def setUp(self):
        from apps.organizations.models import Business

        self.owner = make_user("orphanowner")
        self.biz = Business.objects.create(
            name="Orphan Shop", slug="orphan-shop", owner=self.owner)
        from apps.organizations.models import BusinessMembership
        self.BusinessMembership = BusinessMembership

    def _run_repair(self):
        import importlib

        from django.apps import apps as django_apps

        migration = importlib.import_module(
            "apps.organizations.migrations.0003_repair_owner_memberships")
        migration.repair_owner_memberships(django_apps, None)

    def test_repair_gives_the_owner_admin_access(self):
        self.assertFalse(self.BusinessMembership.objects.filter(
            business=self.biz, user=self.owner).exists())

        self._run_repair()

        membership = self.BusinessMembership.objects.get(
            business=self.biz, user=self.owner)
        self.assertEqual(membership.role, self.BusinessMembership.Role.ADMIN)
        self.assertTrue(membership.is_active)

    def test_repair_is_idempotent(self):
        self._run_repair()
        self._run_repair()
        self.assertEqual(self.BusinessMembership.objects.filter(
            business=self.biz, user=self.owner).count(), 1)

    def test_repair_never_overwrites_an_existing_role(self):
        self.BusinessMembership.objects.create(
            user=self.owner, business=self.biz,
            role=self.BusinessMembership.Role.PARTNER,
        )
        self._run_repair()
        membership = self.BusinessMembership.objects.get(
            business=self.biz, user=self.owner)
        self.assertEqual(membership.role, self.BusinessMembership.Role.PARTNER)

    def test_admin_save_model_creates_the_owner_membership(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from apps.organizations.admin import BusinessAdmin
        from apps.organizations.models import Business

        model_admin = BusinessAdmin(Business, AdminSite())
        new_biz = Business(name="Admin Made", slug="admin-made",
                           owner=self.owner)
        request = RequestFactory().post("/admin/organizations/business/add/")
        request.user = self.owner

        model_admin.save_model(request, new_biz, form=None, change=False)

        membership = self.BusinessMembership.objects.get(
            business=new_biz, user=self.owner)
        self.assertEqual(membership.role, self.BusinessMembership.Role.ADMIN)
