"""Phase 2 tests: customer module + partner-wise due separation.

Key guarantees tested (Sections 4 & 5 & 25):
- Unlimited customer creation.
- Partner sees ONLY their assigned customers; others are invisible (404).
- Partner creating a client auto-assigns it to themselves.
- Shared customer keeps separate partner-wise dues; collecting one partner's
  due (COLLECTION due-entry) never reduces the other partner's due.
- Only ADMIN can create assignments between partners and customers.
- Customer statement math with running balance.
"""
from decimal import Decimal

from django.test import TestCase

from apps.customers.models import (
    Customer, CustomerAssignment, DueEntry, record_due_entry,
)
from apps.organizations.models import BusinessMembership
from apps.partners.models import Partner
from apps.tests_common import auth_client, make_user


def _bootstrap(*owner_partners):
    """owner + partners (partner has platform? False -> partner without user)
    Returns (owner, business_id, [(partner...)] )."""
    owner = make_user("ownerC")
    res = auth_client(owner).post("/api/businesses/", {"name": "C Traders"},
                                  format="json")
    biz_id = res.json()["id"]
    partners = []
    for name in owner_partners:
        partner_user = make_user(name.replace(" ", "_").lower())
        auth_client(owner).post(
            f"/api/businesses/{biz_id}/members/",
            {"email": partner_user.email, "role": "PARTNER"}, format="json",
        )
        res = auth_client(owner).post(
            f"/api/partners/?business={biz_id}",
            {"name": name, "linked_user": str(partner_user.id)}, format="json",
        )
        partners.append(Partner.objects.get(pk=res.json()["id"]))
    return owner, biz_id, partners


class CustomerTests(TestCase):
    def setUp(self):
        self.owner, self.biz, (self.p1, self.p2) = _bootstrap("P One", "P Two")
        self.p1u = self.p1.linked_user
        self.p2u = self.p2.linked_user

    def test_unlimited_customer_creation(self):
        client = auth_client(self.owner)
        for i in range(30):
            res = client.post(f"/api/customers/?business={self.biz}", {
                "name": f"Customer {i:03d}", "phone": f"01700000000",
            }, format="json")
            self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(
            client.get(f"/api/customers/?business={self.biz}").data["count"], 30
        )

    def test_admin_creates_with_partner_assignment_and_opening_due(self):
        client = auth_client(self.owner)
        res = client.post(f"/api/customers/?business={self.biz}", {
            "name": "Shop A", "opening_balance": "5000.00",
            "partner_id": str(self.p1.id),
        }, format="json")
        self.assertEqual(res.status_code, 201)
        customer = Customer.objects.get(pk=res.json()["id"])
        assignment = CustomerAssignment.objects.get(customer=customer,
                                                    partner=self.p1)
        self.assertEqual(assignment.current_due(), Decimal("5000.00"))
        self.assertEqual(customer.total_due(), Decimal("5000.00"))

    def test_partner_auto_assign_creates_clients(self):
        client = auth_client(self.p1u)
        res = client.post(f"/api/customers/?business={self.biz}", {
            "name": "P1 Client", "opening_balance": "2000.00",
        }, format="json")
        self.assertEqual(res.status_code, 201)
        assignment = CustomerAssignment.objects.get(
            customer__id=res.json()["id"], partner=self.p1
        )
        self.assertEqual(assignment.current_due(), Decimal("2000.00"))

    def test_partner_sees_only_assigned_customers(self):
        client = auth_client(self.owner)
        c_assigned = client.post(f"/api/customers/?business={self.biz}", {
            "name": "Assigned One", "partner_id": str(self.p1.id)},
            format="json").json()["id"]
        c_other = client.post(f"/api/customers/?business={self.biz}", {
            "name": "Unassigned"}, format="json").json()["id"]

        as_p1 = auth_client(self.p1u)
        listing = as_p1.get(f"/api/customers/?business={self.biz}")
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(listing.data["results"][0]["id"], c_assigned)
        self.assertEqual(
            as_p1.get(f"/api/customers/{c_other}/?business={self.biz}").status_code,
            404,
        )

    def test_shared_customer_partner_wise_dues_separate(self):
        """The Section 5 example: dues never merge, collection is scoped."""
        client = auth_client(self.owner)
        res = client.post(f"/api/customers/?business={self.biz}", {
            "name": "Shared Shop", "opening_balance": "5000.00",
            "partner_id": str(self.p1.id)}, format="json")
        cid = res.json()["id"]
        client.post(
            f"/api/customers/{cid}/assignments/?business={self.biz}",
            {"partner_id": str(self.p2.id), "opening_due": "3000.00"},
            format="json",
        )
        dues = auth_client(self.owner).get(
            f"/api/customers/{cid}/dues/?business={self.biz}"
        ).json()
        by_partner = {d["partner_id"]: d["due"] for d in dues["partner_wise"]}
        self.assertEqual(Decimal(str(by_partner[str(self.p1.id)])), Decimal("5000.00"))
        self.assertEqual(Decimal(str(by_partner[str(self.p2.id)])), Decimal("3000.00"))
        self.assertEqual(Decimal(str(dues["total_due"])), Decimal("8000.00"))

        # Collect 2000 against Partner 1 only → Partner 2 untouched.
        assignment1 = CustomerAssignment.objects.get(customer_id=cid, partner=self.p1)
        record_due_entry(
            assignment1, DueEntry.Kind.COLLECTION, Decimal("2000.00"),
            "2026-09-10", user=self.owner,
        )
        dues = auth_client(self.owner).get(
            f"/api/customers/{cid}/dues/?business={self.biz}"
        ).json()
        by_partner = {d["partner_id"]: d["due"] for d in dues["partner_wise"]}
        self.assertEqual(Decimal(str(by_partner[str(self.p1.id)])), Decimal("3000.00"))
        self.assertEqual(Decimal(str(by_partner[str(self.p2.id)])), Decimal("3000.00"))
        self.assertEqual(Decimal(str(dues["total_due"])), Decimal("6000.00"))

    def test_partner_cannot_create_assignments(self):
        client = auth_client(self.owner)
        cid = client.post(f"/api/customers/?business={self.biz}", {
            "name": "X", "partner_id": str(self.p1.id)}, format="json").json()["id"]
        res = auth_client(self.p1u).post(
            f"/api/customers/{cid}/assignments/?business={self.biz}",
            {"partner_id": str(self.p2.id), "opening_due": "100"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_statement_running_balance(self):
        client = auth_client(self.owner)
        cid = client.post(f"/api/customers/?business={self.biz}", {
            "name": "Statement Co", "opening_balance": "1000.00",
            "partner_id": str(self.p1.id)}, format="json").json()["id"]
        a1 = CustomerAssignment.objects.get(customer_id=cid, partner=self.p1)
        record_due_entry(a1, DueEntry.Kind.CREDIT, Decimal("500.00"),
                         "2026-09-11", user=self.owner)
        record_due_entry(a1, DueEntry.Kind.COLLECTION, Decimal("200.00"),
                         "2026-09-12", user=self.owner)
        statement = auth_client(self.owner).get(
            f"/api/customers/{cid}/statement/?business={self.biz}"
        ).json()
        self.assertEqual(Decimal(str(statement["opening_due_total"])),
                         Decimal("1000.00"))
        self.assertEqual(Decimal(str(statement["lines"][0]["balance_after"])),
                         Decimal("1500.00"))
        self.assertEqual(Decimal(str(statement["lines"][1]["balance_after"])),
                         Decimal("1300.00"))
        self.assertEqual(Decimal(str(statement["closing_due"])), Decimal("1300.00"))

    def test_partner_cleaning_invalid_assignment_business_cross(self):
        """A partner/business mismatch is structurally rejected."""
        owner2 = make_user("other_owner_x")
        res2 = auth_client(owner2).post("/api/businesses/",
                                        {"name": "Other Traders"}, format="json")
        other_biz = res2.json()["id"]
        pu = make_user("other_p_user_x")
        auth_client(owner2).post(
            f"/api/businesses/{other_biz}/members/",
            {"email": pu.email, "role": "PARTNER"}, format="json",
        )
        res = auth_client(owner2).post(
            f"/api/partners/?business={other_biz}",
            {"name": "Other P", "linked_user": str(pu.id)}, format="json",
        )
        other_partner = Partner.objects.get(pk=res.json()["id"])

        client = auth_client(self.owner)
        cid = client.post(f"/api/customers/?business={self.biz}", {
            "name": "Local"}, format="json").json()["id"]
        assignment = CustomerAssignment(
            customer_id=cid, partner=other_partner, opening_due=Decimal("10")
        )
        with self.assertRaises(ValueError):
            assignment.clean()
