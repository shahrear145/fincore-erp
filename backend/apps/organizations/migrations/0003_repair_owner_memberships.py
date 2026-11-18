"""Repair businesses whose owner has no membership.

Every screen resolves the "active business" through BusinessMembership, so a
business whose owner has no membership row is completely unreachable in the
web UI — the business, its partners, customers and every transaction look as
if they had been deleted.

That state is possible when a Business is created outside the web form (for
example from the Django admin, a shell script or an import): the web form
always creates an ADMIN membership for the owner, nothing else does.

This migration is idempotent: it only adds a membership where none exists.
It never changes an existing role, so a deliberately demoted owner keeps the
role they were given.
"""
from django.db import migrations


def repair_owner_memberships(apps, schema_editor):
    Business = apps.get_model("organizations", "Business")
    BusinessMembership = apps.get_model("organizations", "BusinessMembership")

    for business in Business.objects.exclude(owner_id=None).iterator():
        already = BusinessMembership.objects.filter(
            business_id=business.pk, user_id=business.owner_id
        ).exists()
        if already:
            continue
        BusinessMembership.objects.create(
            business_id=business.pk,
            user_id=business.owner_id,
            role="ADMIN",
            is_active=True,
            partner_permissions={},
        )


def noop(apps, schema_editor):
    """Nothing to undo: the membership may be legitimate by the time we
    migrate backwards, and deleting rows is never worth the risk."""


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0002_initial"),
    ]

    operations = [
        migrations.RunPython(repair_owner_memberships, noop),
    ]
