"""Partner module tests (Phase 1):

- Unlimited partner creation at application level.
- ADMIN sees all partners; PARTNER sees only their own.
- A partner of business A cannot even list/detect partners of business B.
- PARTNER cannot create/deactivate partners.
- Deactivation is audited, records survive (no hard delete).
"""
from django.test import TestCase

from apps.core.middleware import record_audit
from apps.organizations.models import BusinessMembership
from apps.tests_common import auth_client, make_user


def _bootstrap(owner_name, partner_username=None):
    owner = make_user(owner_name)
    res = auth_client(owner).post(
        "/api/businesses/", {"name": f"{owner_name} Traders"}, format="json"
    )
    biz_id = res.json()["id"]
    partner_user = None
    if partner_username:
        partner_user = make_user(partner_username)
        auth_client(owner).post(
            f"/api/businesses/{biz_id}/members/",
            {"email": partner_user.email, "role": "PARTNER"}, format="json",
        )
    return owner, partner_user, biz_id


class PartnerTests(TestCase):
    def setUp(self):
        self.owner_a, self.partner_a_user, self.biz_a = _bootstrap(
            "ownerA", "pa_user"
        )
        self.owner_b, _, self.biz_b = _bootstrap("ownerB")

    def test_unlimited_partner_creation(self):
        """No application-level partner cap: create 40 partners fine (Section 4)."""
        client = auth_client(self.owner_a)
        for i in range(40):
            res = client.post(f"/api/partners/?business={self.biz_a}", {
                "name": f"Partner {i:03d}",
                "initial_capital": "10000.00",
            }, format="json")
            self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(
            client.get(f"/api/partners/?business={self.biz_a}").data["count"], 40
        )

    def test_partner_role_sees_only_own_partner_profile(self):
        client = auth_client(self.owner_a)
        other = client.post(f"/api/partners/?business={self.biz_a}", {
            "name": "Another Partner"
        }, format="json")
        own = client.post(f"/api/partners/?business={self.biz_a}", {
            "name": "Pa Partner", "linked_user": str(self.partner_a_user.id),
        }, format="json")
        self.assertEqual(other.status_code, 201)
        self.assertEqual(own.status_code, 201)

        as_partner = auth_client(self.partner_a_user)
        listing = as_partner.get(f"/api/partners/?business={self.biz_a}")
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(listing.data["results"][0]["name"], "Pa Partner")

        res = as_partner.get(
            f"/api/partners/{other.json()['id']}/?business={self.biz_a}"
        )
        self.assertEqual(res.status_code, 404)

    def test_partner_cannot_create_partners(self):
        res = auth_client(self.partner_a_user).post(
            f"/api/partners/?business={self.biz_a}",
            {"name": "Sneaky Partner"}, format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_partner_cannot_touch_other_partner_of_other_business(self):
        """Partner A of business A must get 403 for business B (rule: partner
        never leaves their business scope; enforced server-side)."""
        res = auth_client(self.partner_a_user).get(
            f"/api/partners/?business={self.biz_b}"
        )
        self.assertEqual(res.status_code, 403)

    def test_deactivation_is_soft_and_audited(self):
        client = auth_client(self.owner_a)
        created = client.post(f"/api/partners/?business={self.biz_a}", {
            "name": "Togglable", "initial_capital": "500.00",
        }, format="json")
        pid = created.json()["id"]
        res = client.post(
            f"/api/partners/{pid}/status/?business={self.biz_a}",
            {"status": "INACTIVE", "reason": "resigned"}, format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "INACTIVE")
        self.assertTrue(
            __import__("apps.core.middleware", fromlist=["AuditLog"])
            .AuditLog.objects.filter(resource_id=str(pid)).exists()
        )
        from apps.partners.models import Partner
        soft_qs = Partner.all_objects.filter(pk=pid)
        self.assertEqual(soft_qs.count(), 1)
        self.assertIsNone(soft_qs.first().deleted_at)

    def test_partner_attribution_not_ownership_separateness(self):
        """Two partners in same business may share a customer concept later;
        structural invariant here: partner rows never hard-delete."""
        from apps.partners.models import Partner

        client = auth_client(self.owner_a)
        created = client.post(f"/api/partners/?business={self.biz_a}", {
            "name": "Erasable"}, format="json")
        p = Partner.objects.get(pk=created.json()["id"])
        p.delete()                       # soft delete
        self.assertTrue(Partner.all_objects.filter(pk=p.pk).exists())
        self.assertFalse(Partner.objects.filter(pk=p.pk).exists())
