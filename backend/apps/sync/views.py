"""Sync API (Phase 8).

POST /api/sync/push/   — client pushes a batch of queued ops.
GET  /api/sync/pull/   — client pulls changes since cursor (Phase 8 core:
                         returns personal data snapshot + cursor).
POST /api/sync/resolve/ — resolve a recorded conflict (ADMIN of self's
                          data; client chooses keep_server or retry).

All endpoints are user-scoped. The server is authoritative: conflicts
are recorded, never auto-merged for money rows.
"""
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.personal.models import PersonalAccount, PersonalTransaction
from apps.personal.serializers import (
    PersonalAccountSerializer, PersonalTransactionSerializer,
)
from apps.sync.engine import process_batch
from apps.sync.models import SyncCursor, SyncOp


class SyncPushView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        ops = request.data.get("ops")
        if not isinstance(ops, list):
            return Response({"detail": "'ops' must be a list."}, status=400)
        if len(ops) > 200:
            return Response({"detail": "Batch too large (max 200)."},
                            status=400)
        device_id = str(request.data.get("device_id") or "unknown-device")
        results = process_batch(request.user, ops, device_id)
        summary = {"applied": 0, "conflict": 0, "rejected": 0}
        for r in results:
            if r["status"] in summary:
                summary[r["status"]] += 1
        return Response({"device_id": device_id, "results": results,
                         "summary": summary})


class SyncPullView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        device_id = str(request.query_params.get("device_id")
                        or "unknown-device")
        cursor_obj, _ = SyncCursor.objects.get_or_create(
            user=request.user, device_id=device_id)
        since = cursor_obj.cursor
        # Full snapshot pull for Phase 8 core (cursor advances to server time).
        # A production delta feed would filter by updated_at > since.
        accounts = PersonalAccountSerializer(
            request.user.personal_accounts.all(), many=True).data
        txns = PersonalTransactionSerializer(
            request.user.personal_transactions.order_by("-updated_at")[:500],
            many=True).data
        conflicts = list(SyncOp.objects.filter(
            user=request.user, status=SyncOp.Status.CONFLICT,
            resolved_at__isnull=True).values(
            "id", "entity", "entity_id", "action", "server_snapshot",
            "payload", "error", "created_at"))
        new_cursor = timezone.now().isoformat()
        cursor_obj.cursor = new_cursor
        cursor_obj.save(update_fields=["cursor"])
        return Response({
            "device_id": device_id,
            "previous_cursor": since,
            "cursor": new_cursor,
            "accounts": accounts,
            "transactions": txns,
            "conflicts": conflicts,
        })


class SyncResolveView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        op_id = request.data.get("op_id")
        resolution = request.data.get("resolution")  # keep_server | retry
        op = SyncOp.objects.filter(user=request.user, pk=op_id).first()
        if op is None:
            return Response({"detail": "Conflict op not found."}, status=404)
        if op.status != SyncOp.Status.CONFLICT:
            return Response({"detail": "Op is not in conflict."}, status=400)
        if resolution == "keep_server":
            op.status = SyncOp.Status.APPLIED
            op.resolved_at = timezone.now()
            op.save(update_fields=["status", "resolved_at"])
            return Response({"status": "applied", "resolution": "keep_server"})
        if resolution == "retry":
            # re-queue the op with a fresh base (client must re-push with
            # current base_updated_at); mark the conflict resolved
            op.resolved_at = timezone.now()
            op.save(update_fields=["resolved_at"])
            return Response({"status": "retry", "detail":
                             "Re-push the op with the server snapshot's "
                             "updated_at as base_updated_at."})
        return Response({"detail": "resolution must be keep_server|retry."},
                        status=400)


class SyncStatusView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        counts = {}
        for status, _ in SyncOp.Status.choices:
            counts[status] = SyncOp.objects.filter(
                user=request.user, status=status).count()
        pending_conflicts = SyncOp.objects.filter(
            user=request.user, status=SyncOp.Status.CONFLICT,
            resolved_at__isnull=True).count()
        return Response({"op_counts": counts,
                         "pending_conflicts": pending_conflicts})
