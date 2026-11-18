"""Assistant actions — natural language → proposed ledger entries.

Nothing is posted automatically. Every command parse produces one of:

* ``kind="answer"``   — not a command; fall back to the Q&A engine.
* ``kind="proposed"`` — a fully-resolved ``AssistantAction`` is created with
  status ``PROPOSED``; the user clicks *Confirm* and it is posted through
  the one validated writer (``apps.transactions.services.create_transaction``)
  with the exact actor/role that has always been enforced everywhere else.

Supported command shapes (Bangla or English, case-insensitive):

    বাকি|due|owes   <customer-name> <amount>        → ADD_BAKI
    পরিশোধ|payment|paid <customer-name> <amount>    → ADD_PAYMENT
    খরচ|expense <amount> [ <description> ]          → ADD_EXPENSE

Numbers can include commas/decimals; the first number is the amount.
Everything else is the customer/description. Names are matched against
the active customers of the selected business (case-insensitive substring).
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction

from apps.assistant.models import AssistantAction
from apps.customers.models import Customer, CustomerAssignment
from apps.transactions.models import TransactionType
from apps.transactions.services import create_transaction
from apps.organizations.models import BusinessMembership

_AMOUNT_RE = re.compile(r"[0-9][0-9,\.]*")
_BAKI_WORDS = ("বাকি", "due", "owes", "owing", "outstanding", "bakai", "বকেয়া")
_PAY_WORDS = ("পরিশোধ", "payment", "pay", "paid", "collection",
              "আদায়", "collected", "পরিশোধিত")
_EXPENSE_WORDS = ("খরচ", "expense", "spend", "spent", "cost")
_FILLER = (
    "যোগ", "দিলাম", "দেও", "হলো", "করুন", "করো", "করি", "for", "the",
    "টাকা", "taka", "৳", "tk", "amount is", "of", "a", "an", "কে", "সে",
    "add", "save", "create",
)


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _find_amount(q: str) -> str | None:
    m = _AMOUNT_RE.search(q)
    return m.group(0) if m else None


def _remainingTokens(q: str, verbWords: tuple[str, ...]) -> str:
    """Drop verb-ish tokens and the leading number token, keep the rest."""
    tokens = _norm(q).replace(",", "").split()
    out = []
    for tok in tokens:
        if tok.lower() in verbWords:
            continue
        if tok in verbWords:
            continue
        if _AMOUNT_RE.fullmatch(tok):
            continue
        out.append(tok)
    return " ".join(out).strip()


def _match_customer(name: str, business):
    if not name:
        return []
    q = name.strip().lower()
    qs = (Customer.objects
          .filter(business=business, status="ACTIVE")
          .filter(name__icontains=q)[:8])
    return list(qs)


def _resolve_assignment(customer, partner, own_partner):
    """WHICH partner's book this customer belongs to. own-partner wins for
    PARTNER role; the customer's single active assignment wins otherwise."""
    if own_partner is not None:
        assignment = (CustomerAssignment.objects
                      .filter(customer=customer, partner=own_partner)
                      .first())
        if assignment is not None:
            return own_partner
        return own_partner  # create_transaction will get_or_create
    if partner is not None:
        return partner
    assignments = (CustomerAssignment.objects
                   .filter(customer=customer, is_active=True)
                   .select_related("partner"))
    assignments = list(assignments)
    if len(assignments) == 1:
        return assignments[0].partner
    if assignments:
        return assignments[0].partner  # default to the customer's first book
    # no assignment anywhere — put on the first ACTIVE partner of the business
    from apps.partners.models import Partner
    return business_first_active_partner(business=customer.business)


def business_first_active_partner(business):
    from apps.partners.models import Partner
    return Partner.objects.filter(business=business, status="ACTIVE").first()


def _own_partner_for(business, membership):
    if membership is None or membership.role != BusinessMembership.Role.PARTNER:
        return None
    return business.partners.filter(linked_user=membership.user).first()


def parse_command(question: str, business, membership) -> dict:
    """Classify ``question``. Returns dict with kind in
    {answer, proposed, ambiguous_customer, customer_not_found, invalid_amount}.
    Token-based: splits on whitespace, first numeric token is the amount,
    everything else is the customer/description — no substring mangling.
    """
    raw_tokens = _norm(question).replace(",", "").split()
    if not raw_tokens:
        return {"kind": "answer"}
    lowered = [t.lower() for t in raw_tokens]

    action_kind = None
    if any(t in _BAKI_WORDS for t in lowered):
        action_kind = AssistantAction.Kind.ADD_BAKI
    elif any(t in _PAY_WORDS for t in lowered):
        action_kind = AssistantAction.Kind.ADD_PAYMENT
    elif any(t in _EXPENSE_WORDS for t in lowered):
        action_kind = AssistantAction.Kind.ADD_EXPENSE

    if action_kind is None:
        return {"kind": "answer"}

    amount_token = next((t for t in raw_tokens if _AMOUNT_RE.fullmatch(t)), None)
    if amount_token is None:
        return {"kind": "invalid_amount", "action_kind": action_kind}
    try:
        amount = Decimal(amount_token).quantize(Decimal("0.01"))
    except InvalidOperation:
        return {"kind": "invalid_amount", "action_kind": action_kind}
    if amount <= 0:
        return {"kind": "invalid_amount", "action_kind": action_kind}

    verb_set = (_BAKI_WORDS + _PAY_WORDS + _EXPENSE_WORDS + _FILLER)
    remainder = " ".join(t for t, low in zip(raw_tokens, lowered)
                         if low not in verb_set
                         and not _AMOUNT_RE.fullmatch(t)).strip()
    own_partner = _own_partner_for(business, membership)

    if action_kind == AssistantAction.Kind.ADD_EXPENSE:
        # description = the remaining non-numeric tokens
        desc_tokens = [t for t in remainder.split()
                       if not _AMOUNT_RE.fullmatch(t)]
        description = " ".join(desc_tokens).strip() or "বিবিধ ব্যয়"
        return {
            "kind": "proposed",
            "action_kind": action_kind,
            "amount": amount,
            "description": description[:200],
            "customer": None,
            "partner": None,
        }

    # ADD_BAKI / ADD_PAYMENT — need a customer
    matches = _match_customer(remainder, business)
    if len(matches) == 0:
        return {"kind": "customer_not_found", "action_kind": action_kind,
                "amount": amount, "hint": remainder}
    if len(matches) > 1:
        return {"kind": "ambiguous_customer", "action_kind": action_kind,
                "amount": amount,
                "choices": [{"id": str(c.pk), "name": c.name} for c in matches]}
    customer = matches[0]
    partner = _resolve_assignment(customer, None, own_partner)
    if partner is None:
        return {"kind": "no_partner", "action_kind": action_kind,
                "amount": amount, "customer_name": customer.name}
    return {
        "kind": "proposed",
        "action_kind": action_kind,
        "amount": amount,
        "description": remainder[:200],
        "customer": customer,
        "partner": partner,
    }


def create_proposed_action(*, user, membership, business, raw_question, parsed):
    """Persist a PROPOSED AssistantAction from a parse result."""
    action = AssistantAction(
        user=user,
        business=business,
        kind=parsed["action_kind"],
        amount=parsed["amount"],
        partner=parsed.get("partner"),
        customer=parsed.get("customer"),
        description=parsed.get("description") or "",
        raw_question=_norm(raw_question)[:500],
        status=AssistantAction.Status.PROPOSED,
        expense_kind=("GOODS" if parsed["action_kind"]
                      == AssistantAction.Kind.ADD_EXPENSE else ""),
    )
    action.save()
    return action


def execute_action(action: AssistantAction, *, user, business, membership):
    """Confirm a proposed action — goes through the one validated writer."""
    from types import SimpleNamespace
    ns = SimpleNamespace(
        user=user,
        business=business,
        business_membership=membership)

    if action.status != AssistantAction.Status.PROPOSED:
        raise ValueError("This proposal was already handled.")

    try:
        with db_transaction.atomic():
            if action.kind == AssistantAction.Kind.ADD_EXPENSE:
                from apps.transactions.models import PaymentMethod, ExpenseKind
                txn = create_transaction(
                    ns, business=action.business,
                    amount=action.amount,
                    transaction_type=TransactionType.EXPENSE,
                    transaction_date=date.today(),
                    payment_method=PaymentMethod.CASH,
                    description=action.description or "বিবিধ ব্যয়",
                    notes="Assistant: " + action.raw_question,
                    expense_kind=(action.expense_kind or ExpenseKind.GOODS))
                action.transaction = txn[0] if isinstance(txn, tuple) else txn
            elif action.kind == AssistantAction.Kind.ADD_BAKI:
                from apps.transactions.models import PaymentMethod
                txn = create_transaction(
                    ns, business=action.business,
                    amount=action.amount,
                    transaction_type=TransactionType.CREDIT,
                    transaction_date=date.today(),
                    partner_id=str(action.partner_id),
                    customer_id=str(action.customer_id),
                    payment_method=PaymentMethod.CASH,
                    description=action.description or action.customer.name,
                    client_reference="")
                action.transaction = txn[0] if isinstance(txn, tuple) else txn
            elif action.kind == AssistantAction.Kind.ADD_PAYMENT:
                from apps.transactions.models import PaymentMethod
                txn = create_transaction(
                    ns, business=action.business,
                    amount=action.amount,
                    transaction_type=TransactionType.PAYMENT,
                    transaction_date=date.today(),
                    partner_id=str(action.partner_id),
                    customer_id=str(action.customer_id),
                    payment_method=PaymentMethod.CASH,
                    description=action.description or action.customer.name,
                    client_reference="")
                action.transaction = txn[0] if isinstance(txn, tuple) else txn
            action.status = AssistantAction.Status.DONE
            action.error = ""
            action.save(update_fields=["transaction", "status", "error"])
    except Exception as exc:
        action.status = AssistantAction.Status.FAILED
        action.error = str(exc)[:500]
        action.save(update_fields=["status", "error"])
        raise

    return action
