"""Transaction service layer — the validated writer for every money movement.

All rules are enforced here, inside one DB transaction (Section 25 rule 6):

- Period lock: backdated entries into a locked FinancialPeriod are rejected.
- Partner scoping: a PARTNER may only write transactions for their own
  partner profile; ADMIN for any partner profile in the business.
- Due books: customer-linked types post to exactly ONE CustomerAssignment
  (the selected partner's book) — never a different partner's due.
- Due limit: CREDIT / due-up ADJUSTMENT beyond Customer.due_limit is
  rejected, unless an ADMIN forces with allow_beyond_limit=true.
- Over-collection: PAYMENT larger than that partner's book current due is
  rejected with a pointer to the ADVANCE type.
- Idempotency: a repeated client_reference returns the existing transaction
  (offline duplicate prevention, Section 19).
- Reversal (Section 18): posted records are never deleted. Every do-booked
  reversal appends an opposite adjustment line; collection reversals free
  their allocations first; a credit that has allocations cannot be voided
  until those payments are reversed. ADMIN-only, always audited.
"""
from decimal import Decimal
import re

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.core.middleware import record_audit
from apps.customers.models import (
    Customer, CustomerAssignment, DueEntry,
    allocate_collection, free_allocations, record_due_entry,
)
from apps.organizations.models import BusinessMembership, FinancialPeriod
from apps.transactions.models import (
    Transaction, TransactionDateMemory, TransactionStatus, TransactionType,
    PaymentMethod, ExpenseKind, CUSTOMER_REQUIRED_TYPES,
)


class TransactionServiceError(Exception):
    def __init__(self, message, status_code=400, errors=None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or {}


def _money(value) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception:
        raise TransactionServiceError("amount must be a valid decimal number.")
    return d.quantize(Decimal("0.01"))


def resolve_actor_partner(request, partner_id=None):
    """Role-aware partner resolution (Section 4).

    ADMIN: partner_id required when the type needs a partner; may act for any.
    PARTNER: locked to their own profile — partner_id (if given) must match.
    Returns (partner_or_None, is_admin).
    """
    m = request.business_membership
    is_full_access = m.role in (BusinessMembership.Role.ADMIN,
                                BusinessMembership.Role.ACCOUNTANT)
    if is_full_access:
        if not partner_id:
            return None, True
        partner = m.business.partners.filter(pk=partner_id).first()
        if partner is None:
            raise TransactionServiceError(
                {"partner": "Unknown partner for this business."})
        return partner, True

    if not m.partner_permissions.get("can_create_transactions", True):
        raise TransactionServiceError(
            "Your role does not permit creating transactions.", status_code=403)
    profile = m.business.partners.filter(linked_user=request.user).first()
    if profile is None:
        raise TransactionServiceError(
            "No partner profile linked to your account.", status_code=403)
    if partner_id and str(partner_id) != str(profile.pk):
        raise TransactionServiceError(
            "Partners can only record transactions for their own profile.",
            status_code=403)
    return profile, False


def _validate_shape(type_code, customer, partner, method, to_method):
    if type_code in CUSTOMER_REQUIRED_TYPES and (customer is None or partner is None):
        raise TransactionServiceError(
            f"Type {type_code} requires both a customer and a partner.")
    if type_code == TransactionType.TRANSFER:
        if customer is not None:
            raise TransactionServiceError("A transfer does not target a customer.")
        if not to_method:
            raise TransactionServiceError({
                "to_payment_method": "Transfer requires a destination account."})
        if to_method == method:
            raise TransactionServiceError({
                "to_payment_method": "Destination must differ from source."})


def create_transaction(request, *, business, amount, transaction_type,
                       transaction_date, partner_id=None, customer_id=None,
                       payment_method=PaymentMethod.CASH, to_payment_method=None,
                       adjustment_direction=None, transaction_time=None,
                       description="", notes="", reference_number="",
                       client_reference="", allow_beyond_limit=False,
                       expense_kind=None):
    amount = _money(amount)
    if amount <= 0:
        raise TransactionServiceError("amount must be greater than zero.")
    if not transaction_date:
        raise TransactionServiceError(
            {"transaction_date": "A transaction date is required."})
    if transaction_type not in TransactionType.values:
        raise TransactionServiceError({"transaction_type": "Invalid type."})
    if transaction_type == TransactionType.ADJUSTMENT:
        if not adjustment_direction:
            raise TransactionServiceError({
                "adjustment_direction": "UP or DOWN is required for adjustments."})
        if adjustment_direction not in ("UP", "DOWN"):
            raise TransactionServiceError({
                "adjustment_direction": "Must be UP or DOWN."})

    customer = None
    if customer_id:
        customer = business.customers.filter(pk=customer_id).first()
        if customer is None:
            raise TransactionServiceError(
                {"customer": "Customer not found in this business."})

    partner, is_admin = resolve_actor_partner(request, partner_id)
    _validate_shape(transaction_type, customer, partner,
                    payment_method, to_payment_method)

    FinancialPeriod.assert_date_not_locked(business, transaction_date)

    if client_reference:
        existing = business.transactions.filter(
            client_reference=client_reference).first()
        if existing is not None:
            return existing, False

    with db_transaction.atomic():
        txn = Transaction.objects.create(
            business=business,
            transaction_type=transaction_type,
            amount=amount,
            partner=partner,
            customer=customer,
            transaction_date=transaction_date,
            transaction_time=transaction_time,
            payment_method=payment_method,
            to_payment_method=to_payment_method,
            adjustment_direction=adjustment_direction,
            description=description or "",
            notes=notes or "",
            reference_number=reference_number or "",
            client_reference=client_reference or "",
            expense_kind=(expense_kind or ExpenseKind.GOODS),
            created_by=request.user,
        )
        if (customer is not None and partner is not None
                and transaction_type in CUSTOMER_REQUIRED_TYPES):
            assignment, _created = CustomerAssignment.objects.get_or_create(
                customer=customer, partner=partner,
                defaults={"opening_due": Decimal("0.00")},
            )
            _post_to_due_book(assignment, txn, is_admin, allow_beyond_limit)
            if transaction_type == TransactionType.PAYMENT:
                txn.receipt_no = _make_receipt_no(business, txn.created_at)
                txn.save(update_fields=["receipt_no"])

        # Last-used date memory: partner-specific, or global when partnerless.
        TransactionDateMemory.touch(business, transaction_date, partner=partner)

        # Double-entry ledger: auto-post the balanced journal for this txn.
        from apps.accounting import postings as _postings
        _postings.post_auto_journal(txn, user=request.user)

        record_audit("create", txn, business=business)

    return txn, True


def _post_to_due_book(assignment, txn, is_admin, allow_beyond_limit):
    t = txn.transaction_type
    date = txn.transaction_date

    if t == TransactionType.PAYMENT:
        due_before = assignment.current_due()
        if txn.amount > due_before:
            raise TransactionServiceError(
                f"Collection ৳{txn.amount} exceeds the current due ৳{due_before} "
                "on this partner's book. Record the excess as an ADVANCE "
                "instead.")
        record_due_entry(assignment, DueEntry.Kind.COLLECTION, txn.amount, date,
                         note=txn.description, user=txn.created_by,
                         transaction=txn)
        allocate_collection(assignment, txn)

    elif t == TransactionType.ADVANCE:
        record_due_entry(assignment, DueEntry.Kind.ADVANCE, txn.amount, date,
                         note=txn.description, user=txn.created_by,
                         transaction=txn)

    elif t == TransactionType.CREDIT:
        _assert_due_limit(assignment, txn.amount, is_admin, allow_beyond_limit)
        record_due_entry(assignment, DueEntry.Kind.CREDIT, txn.amount, date,
                         note=txn.description, user=txn.created_by,
                         transaction=txn,
                         remaining=txn.amount)

    elif t == TransactionType.ADJUSTMENT:
        if txn.adjustment_direction == "UP":
            if not is_admin:
                raise TransactionServiceError(
                    "Only ADMINs can post due-up adjustments.", status_code=403)
            _assert_due_limit(assignment, txn.amount, is_admin,
                              allow_beyond_limit)
            record_due_entry(assignment, DueEntry.Kind.ADJUSTMENT, txn.amount,
                             date, note=txn.description, user=txn.created_by,
                             transaction=txn,
                             direction=DueEntry.Direction.UP)
        else:
            if not is_admin:
                raise TransactionServiceError(
                    "Only ADMINs can post due-down adjustments.", status_code=403)
            if txn.amount > assignment.current_due():
                raise TransactionServiceError(
                    "Due-down adjustment exceeds current due; record an "
                    "ADVANCE or REFUND instead.")
            record_due_entry(assignment, DueEntry.Kind.ADJUSTMENT, txn.amount,
                             date, note=txn.description, user=txn.created_by,
                             transaction=txn,
                             direction=DueEntry.Direction.DOWN)

    elif t == TransactionType.REFUND:
        record_due_entry(assignment, DueEntry.Kind.REFUND, txn.amount, date,
                         note=txn.description, user=txn.created_by,
                         transaction=txn)


def _assert_due_limit(assignment, amount, is_admin, allow_beyond_limit):
    customer = assignment.customer
    limit = customer.due_limit
    if allow_beyond_limit:
        if not is_admin:
            raise TransactionServiceError(
                "Only ADMINs may bypass the customer due limit.", status_code=403)
        return
    if limit is None:
        return
    projected = assignment.current_due() + Decimal(str(amount))
    if projected > Decimal(str(limit)):
        raise TransactionServiceError(
            f"Projected due ৳{projected} exceeds the customer's due limit "
            f"৳{limit}. ADMIN can force with allow_beyond_limit=true.")


def _make_receipt_no(business, created_at) -> str:
    day = created_at.strftime("%Y%m%d")
    count = business.transactions.filter(
        receipt_no__startswith=f"RCP-{day}-").count() + 1
    return f"RCP-{day}-{count:04d}"


def reverse_transaction(request, txn, reason=""):
    """Void a posted transaction. ADMIN-only. No records are deleted."""
    m = request.business_membership
    if m.role != BusinessMembership.Role.ADMIN:
        raise TransactionServiceError(
            "Only business ADMINs may reverse posted transactions.",
            status_code=403)
    if txn.status == TransactionStatus.REVERSED:
        raise TransactionServiceError("Already reversed.")

    def mirror(direction_down: bool):
        assignment = CustomerAssignment.objects.filter(
            customer=txn.customer, partner=txn.partner).first()
        if assignment is None:
            return
        record_due_entry(
            assignment, DueEntry.Kind.ADJUSTMENT, txn.amount,
            txn.transaction_date,
            note=f"Reversal of transaction {txn.id}"
                 + (f": {reason}" if reason else ""),
            user=request.user, transaction=txn,
            direction=DueEntry.Direction.DOWN if direction_down
            else DueEntry.Direction.UP,
        )

    with db_transaction.atomic():
        if txn.transaction_type in (TransactionType.CREDIT,
                                    TransactionType.PAYMENT,
                                    TransactionType.ADVANCE,
                                    TransactionType.REFUND,
                                    TransactionType.ADJUSTMENT):
            assignment = CustomerAssignment.objects.get(
                customer=txn.customer, partner=txn.partner)

            if txn.transaction_type == TransactionType.PAYMENT:
                free_allocations(txn)
                mirror(direction_down=False)     # +amount back to the due

            elif txn.transaction_type == TransactionType.CREDIT:
                entry = txn.due_entries.first()
                if entry is None:
                    raise TransactionServiceError("Ledger entry missing.")
                if entry.allocations.filter(freed_at__isnull=True).exists():
                    raise TransactionServiceError(
                        "This charge has payments allocated against it. "
                        "Reverse those collections first.")
                entry.remaining = Decimal("0.00")
                entry.save(update_fields=["remaining"])
                mirror(direction_down=True)      # −amount back out of the due

            elif txn.transaction_type == TransactionType.ADVANCE:
                mirror(direction_down=False)     # advance reduction is undone

            elif txn.transaction_type == TransactionType.REFUND:
                mirror(direction_down=False)     # refund effect is undone

            elif txn.transaction_type == TransactionType.ADJUSTMENT:
                mirror(direction_down=(txn.adjustment_direction == "UP"))

        txn.status = TransactionStatus.REVERSED
        txn.save(update_fields=["status"])

        # Mirror the automatic journal so the ledger stays balanced.
        from apps.accounting import postings as _postings
        _postings.post_reversal_for_transaction(txn, user=request.user)

        record_audit("reversal", txn, business=txn.business, reason=reason,
                     changes={"status": "REVERSED"})
    return txn
