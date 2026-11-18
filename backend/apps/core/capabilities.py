"""Business role → capabilities. One place to answer "তো এই কাজটি পারে না".

Roles inside a business:

  ADMIN (Owner)   - full access. Only role that can manage users, partners,
                    settings, reset data and hard-delete.
  ACCOUNTANT      - the daily book keeper: entries, edits, exports, customers.
                    No deletes, no settings, no user/partner management.
  VIEWER          - read-only. Sees everything, changes nothing.
  PARTNER         - unchanged from before (partner_permissions JSON governs).

Usage in views: prefer the helpers below (can_write/can_delete/…) instead of
hand-rolling ``membership.role != "ADMIN"`` so all enforcement stays in sync
when roles are added again later.
"""
from __future__ import annotations

from apps.organizations.models import BusinessMembership

ROLE_OWNER = BusinessMembership.Role.ADMIN
ROLE_ACCOUNTANT = BusinessMembership.Role.ACCOUNTANT
ROLE_VIEWER = BusinessMembership.Role.VIEWER
ROLE_PARTNER = BusinessMembership.Role.PARTNER
ALL_ROLES = {ROLE_OWNER, ROLE_ACCOUNTANT, ROLE_VIEWER, ROLE_PARTNER}

READ = "read"
WRITE = "write"                      # create / edit day-to-day entries
DELETE = "delete"                    # void / reverse / hard-delete entries
MANAGE_CUSTOMERS = "manage_customers"  # add/assign customers, set dues
MANAGE_PARTNERS = "manage_partners"    # create/edit/deactivate partners
MANAGE_SETTINGS = "manage_settings"    # business profile, period lock
MANAGE_USERS = "manage_users"          # invite/change/remove members
EXPORT = "export"                     # PDF/Excel/Word exports
APPEND = "append"                     # post follow-ups on own lines (PARTNER)

_MATRIX = {
    ROLE_OWNER: {READ: True, WRITE: True, DELETE: True,
                 MANAGE_CUSTOMERS: True, MANAGE_PARTNERS: True,
                 MANAGE_SETTINGS: True, MANAGE_USERS: True, EXPORT: True,
                 APPEND: True},
    ROLE_ACCOUNTANT: {READ: True, WRITE: True, DELETE: False,
                      MANAGE_CUSTOMERS: True, MANAGE_PARTNERS: False,
                      MANAGE_SETTINGS: False, MANAGE_USERS: False, EXPORT: True,
                      APPEND: True},
    ROLE_VIEWER: {READ: True, WRITE: False, DELETE: False,
                  MANAGE_CUSTOMERS: False, MANAGE_PARTNERS: False,
                  MANAGE_SETTINGS: False, MANAGE_USERS: False, EXPORT: True,
                  APPEND: False},
    # PARTNER keeps the legacy behavior — governed by partner_permissions.
    ROLE_PARTNER: {READ: True, WRITE: True, DELETE: False,
                   MANAGE_CUSTOMERS: True, MANAGE_PARTNERS: False,
                   MANAGE_SETTINGS: False, MANAGE_USERS: False, EXPORT: True,
                   APPEND: True},
}


def capability(membership, cap: str) -> bool:
    """Does this membership's role include ``cap``?"""
    if membership is None:
        return False
    role = membership.role
    if role == ROLE_PARTNER:
        # Partners: the JSON blob decides the borderline caps; the rest match
        # the owner matrix (view anything in their book).
        perms = membership.partner_permissions or {}
        if cap == MANAGE_CUSTOMERS:
            return bool(perms.get("can_manage_customers", True))
        if cap == WRITE:
            return bool(perms.get("can_create_transactions", True))
        if cap == READ:
            return True
        if cap == EXPORT:
            return True
        return _MATRIX[ROLE_PARTNER].get(cap, False)
    return _MATRIX.get(role, {}).get(cap, False)


def is_owner(membership) -> bool:
    return bool(membership and membership.role == ROLE_OWNER)


# alias kept for callers that read better as a role test
is_owner_role = is_owner


def is_viewer(membership) -> bool:
    return bool(membership and membership.role == ROLE_VIEWER)


def can_write(membership) -> bool:
    return capability(membership, WRITE)


def can_book(membership) -> bool:
    """The cash/loan/investment books — Owner and Accountant only.

    PARTNER may post (বাকি/পরিশোধ) on their own book, but not into the
    lender/investor ledger or the নগদ history editor.
    """
    return bool(membership and membership.role in (ROLE_OWNER, ROLE_ACCOUNTANT))


def can_delete(membership) -> bool:
    return capability(membership, DELETE)


def can_read(membership) -> bool:
    return capability(membership, READ)


def can_export(membership) -> bool:
    return capability(membership, EXPORT)


def can_manage_customers(membership) -> bool:
    return capability(membership, MANAGE_CUSTOMERS)


def can_manage_partners(membership) -> bool:
    return capability(membership, MANAGE_PARTNERS)


def can_manage_settings(membership) -> bool:
    return capability(membership, MANAGE_SETTINGS)


def can_manage_users(membership) -> bool:
    return capability(membership, MANAGE_USERS)


def role_label(membership) -> str:
    """বাংলা লেবেল for templates."""
    if membership is None:
        return "—"
    if membership.role == ROLE_OWNER:
        return "মালিক"
    if membership.role == ROLE_ACCOUNTANT:
        return "হিসাবরক্ষক"
    if membership.role == ROLE_VIEWER:
        return "দর্শক"
    return "পার্টনার"


def role_badge(membership) -> str:
    if membership is None:
        return "b-muted"
    if membership.role == ROLE_OWNER:
        return "b-warn"
    if membership.role == ROLE_ACCOUNTANT:
        return "b-brand"
    if membership.role == ROLE_VIEWER:
        return "b-info"
    return "b-muted"
