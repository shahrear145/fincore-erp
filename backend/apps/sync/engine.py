"""Sync engine (Phase 8): apply queued ops with idempotency + conflicts.

Conflict rule: for UPDATE, the client's `base_updated_at` must match the
server row's `updated_at`. A mismatch means the server changed since the
client's last sync — we do NOT auto-merge (money is involved); we record
a conflict with the server snapshot for manual resolution.

Idempotency: if a CREATE arrives with a client_reference that already
produced a successful row, or the entity_id already exists for that
owner, we return `applied` pointing at the existing row (never a
duplicate).

Supported entities (Phase 8 core): personal_account, personal_transaction.
The pattern generalises to business entities via the same op envelope.
"""
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.transactions.models import Transaction, TransactionStatus
from apps.transactions.services import (
    TransactionServiceError, create_transaction,
)
from apps.organizations.models import BusinessMembership
from apps.personal.models import PersonalAccount, PersonalTransaction
from apps.personal.service import (
    PersonalServiceError, create_personal_transaction,
)
from apps.sync.models import SyncOp


@dataclass
class OpResult:
    op_id: str
    entity: str
    entity_id: str
    status: str
    server_entity_id: str = ""
    server_snapshot: dict | None = None
    error: dict | None = None


def _coerce_dt(value):
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value
    return parse_datetime(str(value))


def _snapshot_account(account: PersonalAccount) -> dict:
    return {
        "id": str(account.pk), "name": account.name, "kind": account.kind,
        "status": account.status,
        "balance": str(account.balance().quantize(Decimal("0.01"))),
        "updated_at": account.updated_at.isoformat()
        if account.updated_at else None,
    }


def _snapshot_txn(txn: PersonalTransaction) -> dict:
    return {
        "id": str(txn.pk), "transaction_type": txn.transaction_type,
        "amount": str(txn.amount), "account": str(txn.account_id),
        "transaction_date": str(txn.transaction_date),
        "status": txn.status,
        "updated_at": txn.updated_at.isoformat()
        if txn.updated_at else None,
    }


def _snapshot_business_txn(txn: Transaction) -> dict:
    return {
        "id": str(txn.pk), "transaction_type": txn.transaction_type,
        "amount": str(Decimal(str(txn.amount)).quantize(Decimal("0.01"))),
        "partner_id": str(txn.partner_id) if txn.partner_id else None,
        "customer_id": str(txn.customer_id) if txn.customer_id else None,
        "transaction_date": str(txn.transaction_date),
        "status": txn.status,
        "receipt_no": txn.receipt_no,
        "updated_at": txn.updated_at.isoformat()
        if txn.updated_at else None,
    }


def _conflict(op_id, entity, entity_id, snapshot):
    return OpResult(op_id=op_id, entity=entity, entity_id=entity_id,
                    status=SyncOp.Status.CONFLICT,
                    server_entity_id=str(snapshot["id"]),
                    server_snapshot=snapshot)


def _apply_personal_account(user, op, payload, action, base_updated_at):
    if action == SyncOp.Action.CREATE:
        existing = PersonalAccount.objects.filter(
            user=user, pk=op.entity_id).first()
        if existing:
            return OpResult(str(op.id), op.entity, op.entity_id,
                            SyncOp.Status.APPLIED, str(existing.pk),
                            _snapshot_account(existing))
        name = (payload.get("name") or "Offline Account").strip()
        if PersonalAccount.objects.filter(user=user, name=name).exists():
            # name collision — let the client resolve (not a money merge)
            hit = PersonalAccount.objects.filter(user=user, name=name).first()
            return _conflict(str(op.id), op.entity, op.entity_id,
                             _snapshot_account(hit))
        acct = PersonalAccount.objects.create(
            id=uuid.UUID(str(op.entity_id)), user=user, name=name,
            kind=payload.get("kind") or "CASH",
            initial_balance=Decimal(str(payload.get(
                "initial_balance", "0") or "0")).quantize(Decimal("0.01")),
            status=payload.get("status") or PersonalAccount.Status.ACTIVE)
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.APPLIED, str(acct.pk),
                        _snapshot_account(acct))

    acct = PersonalAccount.objects.filter(user=user, pk=op.entity_id).first()
    if acct is None:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.REJECTED,
                        error={"detail": "Account not found."})

    if action == SyncOp.Action.UPDATE:
        base = _coerce_dt(base_updated_at)
        if base is not None and acct.updated_at is not None:
            server_iso = acct.updated_at.replace(microsecond=0).isoformat()
            base_iso = base.replace(microsecond=0).isoformat()
            if server_iso != base_iso and str(acct.updated_at) != str(base):
                # only flag when truly different (seconds precision guard)
                if abs((acct.updated_at - base).total_seconds()) > 2:
                    return _conflict(str(op.id), op.entity, op.entity_id,
                                     _snapshot_account(acct))
        if "name" in payload:
            name = (payload["name"] or "").strip()
            if name:
                dup = PersonalAccount.objects.filter(
                    user=user, name=name).exclude(pk=acct.pk).exists()
                if not dup:
                    acct.name = name
        if "kind" in payload:
            acct.kind = payload["kind"]
        if "status" in payload and payload["status"] in (
                PersonalAccount.Status.ACTIVE, PersonalAccount.Status.INACTIVE):
            acct.status = payload["status"]
        acct.save()
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.APPLIED, str(acct.pk),
                        _snapshot_account(acct))

    if action == SyncOp.Action.DELETE:
        # soft-delete only when unused (matches API rule)
        from django.db.models import Q
        used = PersonalTransaction.objects.filter(user=user).filter(
            Q(account=acct) | Q(to_account=acct)).exists()
        if used:
            return OpResult(str(op.id), op.entity, op.entity_id,
                            SyncOp.Status.REJECTED,
                            error={"detail": "Account has transactions; "
                                             "deactivate instead."})
        acct.delete()
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.APPLIED, str(acct.pk))
    return OpResult(str(op.id), op.entity, op.entity_id,
                    SyncOp.Status.REJECTED,
                    error={"detail": "Unsupported action."})


def _apply_personal_transaction(user, op, payload, action):
    if action != SyncOp.Action.CREATE:
        # money rows are immutable post-create; reversals are separate ops
        # in the domain model — sync UPDATE/DELETE on a txn is rejected
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.REJECTED,
                        error={"detail": "Transactions are immutable after "
                                         "creation; post a reversal instead."})
    # idempotency: client_reference or the UUID itself
    cref = payload.get("client_reference") or op.client_reference \
        or f"SYNC:{op.entity_id}"
    existing = PersonalTransaction.objects.filter(
        user=user, client_reference=cref).first()
    if existing:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.APPLIED, str(existing.pk),
                        _snapshot_txn(existing))
    existing = PersonalTransaction.objects.filter(
        user=user, pk=op.entity_id).first()
    if existing:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.APPLIED, str(existing.pk),
                        _snapshot_txn(existing))
    try:
        txn, _created = create_personal_transaction(
            user,
            transaction_type=payload.get("transaction_type"),
            amount=payload.get("amount"),
            transaction_date=payload.get("transaction_date")
            or date.today().isoformat(),
            account_id=payload.get("account") or payload.get("account_id"),
            to_account_id=payload.get("to_account"),
            category_id=payload.get("category"),
            savings_goal_id=payload.get("savings_goal"),
            loan_id=payload.get("loan"),
            description=payload.get("description", ""),
            notes=payload.get("notes", ""),
            client_reference=cref,
        )
    except PersonalServiceError as exc:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.REJECTED,
                        error=exc.errors or {"detail": str(exc)})
    return OpResult(str(op.id), op.entity, op.entity_id,
                    SyncOp.Status.APPLIED, str(txn.pk), _snapshot_txn(txn))


def _business_actor(user, business_id):
    """Resolve the user's active membership for offline-synced business ops.
    Role/partner scoping is enforced by the transaction writer — never
    granted here."""
    from types import SimpleNamespace

    membership = (BusinessMembership.objects
                  .select_related("business")
                  .filter(business_id=business_id, user=user,
                          is_active=True, business__is_active=True)
                  .first())
    if membership is None:
        return None, "You are not an active member of this business."
    return SimpleNamespace(user=user, business=membership.business,
                           business_membership=membership), None


def _apply_business_transaction(user, op, payload, action):
    if action != SyncOp.Action.CREATE:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.REJECTED,
                        error={"detail": "Transactions are immutable after "
                                         "creation; post a reversal instead."})
    business_id = payload.get("business") or payload.get("business_id")
    if not business_id:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.REJECTED,
                        error={"detail": "business is required."})
    actor, err = _business_actor(user, business_id)
    if actor is None:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.REJECTED,
                        error={"detail": err})

    cref = payload.get("client_reference") or op.client_reference \
        or f"SYNC:{op.entity_id}"
    existing = Transaction.objects.filter(
        business=actor.business, client_reference=cref).first()
    if existing:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.APPLIED, str(existing.pk),
                        _snapshot_business_txn(existing))
    existing = Transaction.objects.filter(
        business=actor.business, pk=op.entity_id).first()
    if existing:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.APPLIED, str(existing.pk),
                        _snapshot_business_txn(existing))
    try:
        is_admin = actor.business_membership.role == BusinessMembership.Role.ADMIN
        txn, _created = create_transaction(
            actor,
            business=actor.business,
            amount=payload.get("amount"),
            transaction_type=payload.get("transaction_type"),
            transaction_date=payload.get("transaction_date")
            or date.today().isoformat(),
            partner_id=payload.get("partner"),
            customer_id=payload.get("customer"),
            payment_method=payload.get("payment_method", "CASH"),
            to_payment_method=payload.get("to_payment_method"),
            adjustment_direction=payload.get("adjustment_direction"),
            transaction_time=payload.get("transaction_time"),
            description=payload.get("description", ""),
            notes=payload.get("notes", ""),
            reference_number=payload.get("reference_number", ""),
            client_reference=cref,
            allow_beyond_limit=bool(payload.get("allow_beyond_limit"))
            and is_admin,
        )
    except TransactionServiceError as exc:
        return OpResult(str(op.id), op.entity, op.entity_id,
                        SyncOp.Status.REJECTED,
                        error=exc.errors or {"detail": str(exc)})
    return OpResult(str(op.id), op.entity, op.entity_id,
                    SyncOp.Status.APPLIED, str(txn.pk),
                    _snapshot_business_txn(txn))


SUPPORTED = {
    "personal_account": _apply_personal_account,
    "personal_transaction": _apply_personal_transaction,
    "business_transaction": _apply_business_transaction,
}


def apply_op(user, op_data: dict, device_id: str) -> OpResult:
    """Apply one client op. Returns a result; never raises for bad data."""
    try:
        op_id = str(op_data.get("op_id") or op_data.get("id")
                    or uuid.uuid4())
        entity = str(op_data.get("entity") or "")
        entity_id = str(op_data.get("entity_id") or "")
        action = str(op_data.get("action") or "CREATE").upper()
        payload = op_data.get("payload") or {}
        if not entity or not entity_id:
            return OpResult(op_id, entity, entity_id, SyncOp.Status.REJECTED,
                            error={"detail": "entity and entity_id required."})
        if entity not in SUPPORTED:
            return OpResult(op_id, entity, entity_id, SyncOp.Status.REJECTED,
                            error={"detail": f"Unsupported entity: {entity}"})
        if action not in (SyncOp.Action.CREATE, SyncOp.Action.UPDATE,
                          SyncOp.Action.DELETE):
            return OpResult(op_id, entity, entity_id, SyncOp.Status.REJECTED,
                            error={"detail": f"Invalid action: {action}"})

        with db_transaction.atomic():
            handler = SUPPORTED[entity]
            base = op_data.get("base_updated_at")
            if entity == "personal_account":
                result = handler(user, type("O", (), {"id": op_id, "entity": entity,
                                "entity_id": entity_id,
                                "client_reference": op_data.get(
                                    "client_reference", "")})(),
                                payload, action, base)
            else:
                result = handler(user, type("O", (), {"id": op_id, "entity": entity,
                                "entity_id": entity_id,
                                "client_reference": op_data.get(
                                    "client_reference", "")})(),
                                payload, action)
            try:
                row_id = uuid.UUID(op_id)
            except (ValueError, AttributeError, TypeError):
                row_id = uuid.uuid4()
            SyncOp.objects.update_or_create(
                id=row_id,
                defaults=dict(
                    user=user, device_id=device_id, entity=entity,
                    entity_id=entity_id, action=action,
                    client_reference=op_data.get("client_reference", ""),
                    base_updated_at=_coerce_dt(base),
                    payload=payload, status=result.status,
                    server_entity_id=result.server_entity_id,
                    server_snapshot=result.server_snapshot,
                    error=result.error,
                ),
            )
            result.op_id = str(row_id)
            return result
    except Exception as exc:
        return OpResult(str(op_data.get("op_id", "")),
                        str(op_data.get("entity", "")),
                        str(op_data.get("entity_id", "")),
                        SyncOp.Status.REJECTED,
                        error={"detail": str(exc)})


def process_batch(user, ops: list, device_id: str) -> list[dict]:
    results = []
    for op in ops:
        r = apply_op(user, op, device_id)
        results.append({
            "op_id": r.op_id, "entity": r.entity, "entity_id": r.entity_id,
            "status": r.status, "server_entity_id": r.server_entity_id,
            "server_snapshot": r.server_snapshot, "error": r.error,
        })
    return results
