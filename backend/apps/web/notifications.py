"""নোটিফিকেশন সেন্টার — event feed + live alerts, one place.

Two layers, deliberately kept apart:

1. **Event feed** — rows in ``Notification`` (append-only): বাকি বাড়ল,
   রিমাইন্ডার পাঠানো হলো, বাজেট শেষ (claimed at the moment the expense hits).
   Per-recipient, read/unread state, survives reload — the shop's audit trail.

2. **Live alerts** — computed fresh on every render, never stored: budget
   categories already over this month, installments coming due (≤ 7 days) or
   already overdue. They can never drift from reality; they carry no unread
   flag.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import re

from django.utils import timezone

from apps.organizations.models import BusinessMembership
from apps.web.models import Notification

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def admin_user_ids(business) -> list:
    return list(
        BusinessMembership.objects.filter(
            business=business, role=BusinessMembership.Role.ADMIN,
            is_active=True).values_list("user_id", flat=True))


def notify_user(business, *, recipient, kind: str, title: str,
                body: str = "", link: str = "",
                amount: Decimal | None = None) -> None:
    """One targeted notification to exactly one member."""
    Notification.objects.create(
        business=business, recipient=recipient, kind=kind,
        title=title[:160], body=body, link=link,
        amount=(amount if amount is not None else ZERO))


def notify_team_update(business, *, recipient, title: str, body: str = "",
                       link: str = "") -> None:
    notify_user(business, recipient=recipient,
                kind=Notification.Kind.TEAM_UPDATE,
                title=title, body=body, link=link)


def notify_admins(business, *, kind: str, title: str, body: str = "",
                  link: str = "", amount: Decimal | None = None) -> int:
    """Fan out one notification row to every active ADMIN of the business."""
    rows = []
    for uid in admin_user_ids(business):
        rows.append(Notification(
            business=business, recipient_id=uid, kind=kind,
            title=title[:160], body=body, link=link,
            amount=(amount if amount is not None else ZERO)))
    if rows:
        Notification.objects.bulk_create(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Event feed — read
# ---------------------------------------------------------------------------

def unread_count(business, user) -> int:
    if business is None:
        return 0
    return Notification.objects.filter(
        business=business, recipient=user, is_read=False).count()


def timeline(business, user, *, kind: str | None = None,
             unread_only: bool = False, limit: int = 60) -> list[dict]:
    qs = Notification.objects.filter(business=business, recipient=user)
    if kind:
        qs = qs.filter(kind=kind)
    if unread_only:
        qs = qs.filter(is_read=False)
    qs = qs.order_by("-created_at")[:limit]
    today = timezone.localdate()
    groups: dict[str, list[dict]] = {}
    order = ["আজ", "গতকাল", "এই সপ্তাহে", "পুরোনো"]
    for n in qs:
        d = timezone.localdate(n.created_at)
        diff = (today - d).days
        if diff <= 0:
            bucket = "আজ"
        elif diff == 1:
            bucket = "গতকাল"
        elif diff < 7:
            bucket = "এই সপ্তাহে"
        else:
            bucket = "পুরোনো"
        groups.setdefault(bucket, []).append({
            "id": n.id,
            "title": n.title,
            "body": n.body,
            "link": n.link,
            "amount": n.amount,
            "is_read": n.is_read,
            "created_at": n.created_at,
            "kind": n.kind,
            "icon": kind_icon(n.kind),
            "tone": kind_tone(n.kind),
            "rel": rel_time(n.created_at),
        })
    return [{"label": k, "items": groups[k]} for k in order if k in groups]


# ---------------------------------------------------------------------------
# Live alerts — always computed, never stored
# ---------------------------------------------------------------------------

def live_alerts(business, today: date | None = None) -> list[dict]:
    """Points the shop should look at right now. Empty list = all quiet."""
    today = today or timezone.localdate()
    alerts: list[dict] = []

    # -- budgets already over this month -----------------------------------
    month_start = today.replace(day=1)
    from apps.transactions.models import MonthlyBudget
    for budget in MonthlyBudget.objects.filter(business=business,
                                               month=month_start):
        if budget.is_over:
            spent = budget.spent
            pct = min(100, int(spent / budget.limit_amount * 100)) if budget.limit_amount else 100
            alerts.append({
                "kind": "BUDGET_OVER",
                "tone": "warn",
                "icon": "fa-receipt",
                "title": f"বাজেট শেষ — {budget.get_category_display()}",
                "body": (f"এই মাসে {money_or_blank(budget.spent)} খরচ — "
                         f"সিলিং {money_or_blank(budget.limit_amount)} ({pct}%)"),
                "amount": budget.spent - budget.limit_amount,
                "link": "/expenses/",
                "pct": pct,
                "spent": budget.spent,
                "limit": budget.limit_amount,
                "category": budget.get_category_display(),
                "over": True,
            })

    # -- installments due soon or already overdue ---------------------------
    from apps.loans.models import LoanInstallment, LoanParty
    horizon = today + timedelta(days=7)
    due = (LoanInstallment.objects
           .filter(business=business, is_paid=False,
                   due_date__lte=horizon)
           .select_related("party")
           .order_by("due_date"))
    for inst in due:
        days_left = (inst.due_date - today).days
        overdue = days_left < 0
        alerts.append({
            "kind": "INSTALLMENT_DUE",
            "tone": "danger" if overdue else "info",
            "icon": "fa-calendar-check",
            "title": (f"কিস্তি ওভারডিউ — {inst.party.name}" if overdue
                      else f"কিস্তির তারিখ ঘনিয়ে এলো — {inst.party.name}"),
            "body": (f"কিস্তি {inst.sequence} · ৳{inst.amount:,.2f} · "
                     f"{inst.due_date.strftime('%d %b %Y')}"),
            "amount": inst.amount,
            "link": "/loans/",
            "days_left": days_left,
            "days_abs": abs(days_left),
            "overdue": overdue,
            "party": inst.party.name,
            "due_date": inst.due_date,
            "sequence": inst.sequence,
        })

    return alerts


def money_or_blank(value) -> str:
    try:
        return f"৳{Decimal(str(value or 0)).quantize(CENT):,.2f}"
    except Exception:
        return str(value)


def merge_sort(alerts: list[dict]) -> list[dict]:
    """Tone-priority order: danger → warn → info."""
    tone_order = {"danger": 0, "warn": 1, "info": 2, "ok": 3}
    return sorted(alerts, key=lambda a: tone_order.get(a.get("tone"), 9))


# ---------------------------------------------------------------------------
# Small helpers the UI needs
# ---------------------------------------------------------------------------

def rel_time(dt, now=None) -> str:
    now = now or timezone.now()
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "এইমাত্র"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes} মিনিট আগে"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ঘন্টা আগে"
    days = hours // 24
    if days == 1:
        return "গতকাল"
    if days < 7:
        return f"{days} দিন আগে"
    if days < 30:
        return f"{days // 7} সপ্তাহ আগে"
    return dt.strftime("%d %b %Y")


def kind_icon(kind: str) -> str:
    return {
        Notification.Kind.DUE_INCREASED: "fa-hand-holding-dollar",
        Notification.Kind.REMINDER_SENT: "fa-bell",
        Notification.Kind.BUDGET_OVER: "fa-receipt",
        Notification.Kind.TEAM_UPDATE: "fa-user-gear",
    }.get(kind, "fa-circle-info")


def kind_tone(kind: str) -> str:
    return {
        Notification.Kind.DUE_INCREASED: "red",
        Notification.Kind.REMINDER_SENT: "blue",
        Notification.Kind.BUDGET_OVER: "amber",
        Notification.Kind.TEAM_UPDATE: "violet",
    }.get(kind, "slate")
