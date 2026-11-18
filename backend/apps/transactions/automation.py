"""Phase 7 automation: recurring runs + duplicate detection.

Recurring rules ALWAYS post through `create_transaction` (the single
validated writer) â€” journals, due books, date memory and audit all flow
from the normal path. Idempotency comes from the deterministic
client_reference `RR:{rule_id}:{due_date}`, so running a due day twice
can never double-post.

Duplicate detection is advisory: the writer still succeeds, but the
create response carries `duplicate_warning` (near-identical committed
transactions from Â±1 day) and a review endpoint scans for suspicious
pairs. Nothing is auto-deleted â€” reversals stay manual, per the
no-hard-delete policy.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.transactions.models import (
    RecurringTransaction, Transaction, TransactionStatus,
)
from apps.transactions.services import create_transaction
from apps.core.middleware import record_audit

CENT = Decimal("0.01")
MAX_CATCHUP_CYCLES = 500  # runaway guard for badly-behaving schedules


class AutomationError(Exception):
    def __init__(self, message, status_code=400, errors=None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or {}


def build_actor(business, user):
    """A request-like context for calls outside the HTTP cycle (tests,
    schedulers). Role comes from the real membership row â€” never a fake
    ADMIN badge."""
    from types import SimpleNamespace

    from apps.organizations.models import BusinessMembership
    membership = BusinessMembership.objects.filter(
        business=business, user=user, is_active=True).first()
    if membership is None:
        raise AutomationError(
            "Only active business members can run automation.",
            status_code=403)
    return SimpleNamespace(user=user, business=business,
                           business_membership=membership)


def _ensure_actor(request, business):
    if hasattr(request, "business_membership") and \
            hasattr(request, "business"):
        return request
    user = getattr(request, "user", None) or request
    return build_actor(business, user)


def advance_date(d: date, frequency: str, interval: int) -> date:
    """Pure date math; MONTHLY clamps to the month's last day."""
    if frequency == RecurringTransaction.Frequency.DAILY:
        return d + timedelta(days=interval)
    if frequency == RecurringTransaction.Frequency.WEEKLY:
        return d + timedelta(weeks=interval)
    months = int(interval)
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last_day = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0))
                else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last_day))


def run_recurring(request, rule: RecurringTransaction, *,
                  upto: date | None = None) -> dict:
    """Post every due cycle of one rule through the transaction writer."""
    request = _ensure_actor(request, rule.business)
    if rule.status != RecurringTransaction.Status.ACTIVE:
        raise AutomationError({"rule": "This rule is paused."})
    if str(rule.business_id) != str(request.business.pk):
        raise AutomationError("Not this business's rule.", status_code=403)

    today = upto or timezone.localdate()
    if hasattr(today, "date") and not isinstance(today, date):
        today = today.date()
    if isinstance(today, str):
        today = date.fromisoformat(today)

    posted = []
    skipped = []
    cycles = 0
    while (rule.next_date and rule.next_date <= today
           and cycles < MAX_CATCHUP_CYCLES):
        if rule.end_date and rule.next_date > rule.end_date:
            break
        if rule.max_runs is not None and rule.times_run >= rule.max_runs:
            break
        due = rule.next_date
        client_reference = rule.client_ref_for(due)
        with db_transaction.atomic():
            txn, created = create_transaction(
                request,
                business=request.business,
                amount=rule.amount,
                transaction_type=rule.transaction_type,
                transaction_date=due,
                partner_id=str(rule.partner_id) if rule.partner_id else None,
                customer_id=str(rule.customer_id) if rule.customer_id else None,
                payment_method=rule.payment_method,
                to_payment_method=rule.to_payment_method,
                adjustment_direction=rule.adjustment_direction,
                description=rule.description or rule.title,
                client_reference=client_reference,
            )
            # advance the schedule atomically with the posting
            rule.times_run = rule.times_run + 1
            rule.next_date = advance_date(
                due, rule.frequency, rule.interval)
            rule.last_run_at = timezone.now()
            rule.save(update_fields=["times_run", "next_date",
                                     "last_run_at"])
            record_audit(
                "create" if created else "recurring-skip",
                rule, business=request.business, changes={
                    "posted_date": str(due),
                    "transaction": str(txn.pk),
                    "idempotent": not created,
                })
        (posted if created else skipped).append({
            "due_date": str(due), "transaction_id": str(txn.pk),
        })
        cycles += 1
    return {"posted": posted, "skipped_existing": skipped,
            "next_date": rule.next_date and str(rule.next_date),
            "times_run": rule.times_run}


def run_due_recurring(request) -> dict:
    """Process every active & due rule of the current business."""
    today = timezone.localdate()
    results = []
    rules = request.business.recurring_rules.filter(
        status=RecurringTransaction.Status.ACTIVE,
        next_date__lte=today).order_by("next_date")
    for rule in rules:
        try:
            outcome = run_recurring(request, rule)
            results.append({"rule": rule.title, "rule_id": str(rule.pk),
                            **outcome})
        except Exception as exc:  # one bad rule must not stop the batch
            results.append({"rule": rule.title, "rule_id": str(rule.pk),
                            "error": str(exc)})
    return {"runs": results, "date": str(today)}

# --- duplicate detection -----------------------------------------------------

def find_potential_duplicates(txn: Transaction) -> list[Transaction]:
    """Committed txns of the same partner+customer+amount within Â±1 day."""
    if txn.customer_id is None:
        return []
    d = txn.transaction_date
    if isinstance(d, str):
        d = date.fromisoformat(d)
    window = (d - timedelta(days=1), d + timedelta(days=1))
    return list(Transaction.objects.filter(
        business=txn.business,
        partner=txn.partner,
        customer=txn.customer,
        amount=Decimal(str(txn.amount)).quantize(CENT),
        transaction_date__range=window,
        status=TransactionStatus.COMPLETED,
    ).exclude(pk=txn.pk).order_by("-transaction_date")[:5])


def scan_duplicates(business) -> list[dict]:
    """Pairs of near-identical committed transactions for review."""
    recent = business.transactions.filter(
        status=TransactionStatus.COMPLETED,
        customer__isnull=False,
    ).order_by("-transaction_date")[:200]
    groups = []
    seen = set()
    for txn in recent:
        if txn.pk in seen:
            continue
        twins = find_potential_duplicates(txn)
        if twins:
            seen.add(txn.pk)
            seen.update(t.pk for t in twins)
            groups.append({
                "amount": str(txn.amount.quantize(CENT)),
                "customer": txn.customer.name,
                "window": "Â±1 day",
                "transaction_ids": [str(txn.pk)] +
                                   [str(t.pk) for t in twins],
            })
        if len(groups) >= 50:
            break
    return groups
