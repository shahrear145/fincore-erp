"""Shared factories (isolated test data only — never shipped data).

Builds: owner (ADMIN) + two partner users/profiles.
"""
from apps.tests_common import auth_client, make_user

from apps.partners.models import Partner
from apps.customers.models import Customer


def build_env(username_prefix="p4"):
    owner = make_user(f"{username_prefix}owner")
    res = auth_client(owner).post(
        "/api/businesses/", {"name": "P4 Traders"}, format="json")
    biz = res.json()["id"]

    def partner(name, n):
        user = make_user(f"{username_prefix}partner{n}")
        auth_client(owner).post(
            f"/api/businesses/{biz}/members/",
            {"email": user.email, "role": "PARTNER"}, format="json")
        pid = auth_client(owner).post(
            f"/api/partners/?business={biz}",
            {"name": name, "linked_user": str(user.id)}, format="json"
        ).json()["id"]
        return Partner.objects.get(pk=pid)

    p1 = partner("Partner One", 1)
    p2 = partner("Partner Two", 2)
    return {"owner": owner, "biz": biz, "p1": p1, "p2": p2}


def add_customer(env_c, biz, partner, name, opening="0.00"):
    res = env_c.post(f"/api/customers/?business={biz}",
                     {"name": name, "opening_balance": opening,
                      "partner_id": str(partner.id)}, format="json")
    return Customer.objects.get(pk=res.json()["id"])
