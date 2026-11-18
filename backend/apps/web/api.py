"""Additive API surface for the Flutter app.

Every rule of the web UI is enforced here too — business scoping comes from
``BusinessAccessRequired`` (X-Active-Business header, ``?business=`` or URL),
partner scoping is server-side, and the same service functions/builders are
reused. Nothing in the web UI or models changes.
"""
import logging
from datetime import date

from django.http import HttpResponse
from django.utils import timezone

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import BusinessAccessRequired
from apps.web.views import _own_partner, _reminder_json
from apps.web.profile_views import _blacklist_all_tokens, _revoke_all_device_sessions
from apps.customers.models import CustomerAssignment, DueReminder
from apps.customers.reminders import (
    COOLDOWN_DAYS,
    build_email_subject,
    build_reminder_message,
    build_whatsapp_url,
    format_money,
    normalize_bd_phone,
    send_email_reminder,
)
from apps.core.middleware import record_audit
from apps.users.models import User
from apps.users.otp import issue_email_otp, verify_email_otp

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Password reset (forgot) — same flow as the web, as additive JSON endpoints
# --------------------------------------------------------------------------

class PasswordForgotView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        user = User.objects.filter(email__iexact=email, is_active=True).first() if email else None
        if user:
            try:
                issue_email_otp(user, purpose="password_reset")
            except Exception:  # pragma: no cover - mail backend failure
                logger.exception("password_reset OTP email failed")
        # Never reveal whether the email belongs to an account.
        return Response({"ok": True})


class PasswordResetView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        code = (request.data.get("code") or "").strip()
        p1 = (request.data.get("new_password1") or "").strip()
        p2 = (request.data.get("new_password2") or p1).strip()

        user = User.objects.filter(email__iexact=email, is_active=True).first() if email else None
        if user is None:
            return Response({"ok": False, "error": "account_not_found"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not p1:
            return Response({"ok": False, "error": "password_required"},
                            status=status.HTTP_400_BAD_REQUEST)
        if p1 != p2:
            return Response({"ok": False, "error": "passwords_do_not_match"},
                            status=status.HTTP_400_BAD_REQUEST)

        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError
        try:
            validate_password(p1, user=user)
        except ValidationError as exc:
            return Response({"ok": False, "error": " ".join(exc.messages)},
                            status=status.HTTP_400_BAD_REQUEST)

        ok, msg = verify_email_otp(user, "password_reset", code)
        if not ok:
            return Response({"ok": False, "error": msg},
                            status=status.HTTP_400_BAD_REQUEST)

        user.set_password(p1)
        user.save(update_fields=["password"])
        from django.db import transaction as db_transaction
        with db_transaction.atomic():
            _blacklist_all_tokens(user)
            _revoke_all_device_sessions(user)
        try:
            record_audit("update", user, resource_type="User",
                         resource_id=user.pk, changes={"password": "***reset***"})
        except Exception:
            logger.exception("reset audit failed")
        return Response({"ok": True})


class PasswordResetResendView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        user = User.objects.filter(email__iexact=email, is_active=True).first() if email else None
        if user:
            try:
                issue_email_otp(user, purpose="password_reset")
            except Exception:  # pragma: no cover - mail backend failure
                logger.exception("password_reset resend OTP failed")
        return Response({"ok": True})


# --------------------------------------------------------------------------
# Binary exports (PDF / XLSX / DOCX) — the same builders the web view uses
# --------------------------------------------------------------------------

def _binary_response(content: bytes, content_type: str, filename: str) -> HttpResponse:
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _fmt_key(fmt: str):
    fmt_key = (fmt or "").lower()
    if fmt_key in ("excel", "xlsx"):
        return "xlsx", "xlsx", (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    if fmt_key in ("word", "docx"):
        return "docx", "docx", (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    if fmt_key == "pdf":
        return "pdf", "pdf", "application/pdf"
    return None, None, None


def _parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


class KhataExportView(APIView):
    permission_classes = [permissions.IsAuthenticated, BusinessAccessRequired]

    def get(self, request, fmt):
        from apps.web.exports import (
            build_khata_export, export_khata_docx, export_khata_pdf, export_khata_xlsx,
        )

        biz, membership = request.business, request.business_membership
        own = _own_partner(biz, membership, request.user)
        partner_id = (request.query_params.get("partner") or "").strip()

        selected = None
        if own is not None:
            if partner_id and str(partner_id) != str(own.id):
                return Response({"ok": False, "error": "partner_scope"},
                                status=status.HTTP_403_FORBIDDEN)
            selected = own
        elif partner_id:
            try:
                selected = biz.partners.filter(pk=partner_id, status="ACTIVE").first()
            except Exception:
                selected = None
        if selected is None:
            return Response({"ok": False, "error": "partner_required"},
                            status=status.HTTP_400_BAD_REQUEST)
        if membership.role != "ADMIN" and (own is None or selected.id != own.id):
            return Response({"ok": False, "error": "partner_scope"},
                            status=status.HTTP_403_FORBIDDEN)

        pdate = _parse_date(request.query_params.get("pdate"))
        if pdate is None:
            return Response({"ok": False, "error": "pdate_required"},
                            status=status.HTTP_400_BAD_REQUEST)

        lang = "en" if (request.query_params.get("lang") or "bn") == "en" else "bn"
        fmt_key, extension, content_type = _fmt_key(fmt)
        if fmt_key is None:
            return Response({"ok": False, "error": "unsupported_format"},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            data = build_khata_export(biz, selected, pdate, lang)
            content = {"pdf": export_khata_pdf, "xlsx": export_khata_xlsx,
                       "docx": export_khata_docx}[fmt_key](data)
        except Exception:
            logger.exception("khata export failed (format=%s, partner=%s, date=%s)",
                             fmt_key, selected.id, pdate)
            return Response({"ok": False, "error": "export_failed"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return _binary_response(content, content_type, f"{data['filename']}.{extension}")


class SummaryExportView(APIView):
    permission_classes = [permissions.IsAuthenticated, BusinessAccessRequired]

    def get(self, request, fmt):
        from apps.web.summary_export import (
            build_summary_export, export_summary_docx, export_summary_pdf, export_summary_xlsx,
        )

        biz = request.business
        today = timezone.localdate()
        dfrom = _parse_date(request.query_params.get("from")) or today.replace(day=1)
        dto = _parse_date(request.query_params.get("to")) or today
        if dfrom > dto:
            dfrom, dto = dto, dfrom
        lang = "en" if (request.query_params.get("lang") or "bn") == "en" else "bn"

        fmt_key, extension, content_type = _fmt_key(fmt)
        if fmt_key is None:
            return Response({"ok": False, "error": "unsupported_format"},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            data = build_summary_export(biz, dfrom, dto, lang)
            content = {"pdf": export_summary_pdf, "xlsx": export_summary_xlsx,
                       "docx": export_summary_docx}[fmt_key](data)
        except Exception:
            logger.exception("summary export failed (format=%s, business=%s)", fmt_key, biz.id)
            return Response({"ok": False, "error": "export_failed"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return _binary_response(content, content_type, f"{data['filename']}.{extension}")


class SuperCalcExportView(APIView):
    permission_classes = [permissions.IsAuthenticated, BusinessAccessRequired]

    def get(self, request, fmt):
        from apps.web.exports import (
            build_supercalc_export, export_supercalc_docx, export_supercalc_pdf, export_supercalc_xlsx,
        )

        biz = request.business
        today = timezone.localdate()
        dfrom = _parse_date(request.query_params.get("from")) or today.replace(day=1)
        dto = _parse_date(request.query_params.get("to")) or today
        if dfrom > dto:
            dfrom, dto = dto, dfrom
        lang = "en" if (request.query_params.get("lang") or "bn") == "en" else "bn"

        fmt_key, extension, content_type = _fmt_key(fmt)
        if fmt_key is None:
            return Response({"ok": False, "error": "unsupported_format"},
                            status=status.HTTP_404_NOT_FOUND)
        try:
            data = build_supercalc_export(biz, dfrom, dto, lang)
            content = {"pdf": export_supercalc_pdf, "xlsx": export_supercalc_xlsx,
                       "docx": export_supercalc_docx}[fmt_key](data)
        except Exception:
            logger.exception("supercalc export failed (format=%s, business=%s)", fmt_key, biz.id)
            return Response({"ok": False, "error": "export_failed"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return _binary_response(content, content_type, f"{data['filename']}.{extension}")


# --------------------------------------------------------------------------
# Due reminders (WhatsApp / email) — one customer, one partner book at a time
# --------------------------------------------------------------------------

def _reminder_assignment(request, customer_id, partner_id):
    biz = request.business
    membership = request.business_membership
    partner_id = (partner_id or "").strip()
    if not partner_id:
        return None, Response({"ok": False, "error": "partner_id_required"},
                              status=status.HTTP_400_BAD_REQUEST)
    assignment = (CustomerAssignment.objects
                  .select_related("customer", "partner")
                  .filter(customer_id=customer_id, partner_id=partner_id,
                          customer__business=biz)
                  .first())
    if assignment is None:
        return None, Response({"ok": False, "error": "assignment_not_found"},
                              status=status.HTTP_404_NOT_FOUND)
    if membership.role != "ADMIN":
        own = _own_partner(biz, membership, request.user)
        if own is None or assignment.partner_id != own.id:
            return None, Response({"ok": False, "error": "partner_scope"},
                                  status=status.HTTP_403_FORBIDDEN)
    return assignment, None


def _reminder_state(biz, assignment, lang):
    customer = assignment.customer
    amount = assignment.current_due()
    message = build_reminder_message(
        customer=customer, business=biz, partner=assignment.partner,
        amount=amount, lang=lang,
    )
    last = (DueReminder.objects
            .filter(business=biz, partner=assignment.partner, customer=customer)
            .order_by("-created_at")
            .first())
    cooldown_active = bool(
        last and (timezone.now() - last.created_at).days < COOLDOWN_DAYS
    )
    return {
        "amount": amount,
        "lang": lang,
        "message": message,
        "last": last,
        "cooldown_active": cooldown_active,
    }


class DueReminderView(APIView):
    """GET: preview the reminder. POST: log + hand off (WhatsApp / email)."""

    permission_classes = [permissions.IsAuthenticated, BusinessAccessRequired]

    def get(self, request, customer_id):
        biz = request.business
        lang = "en" if (request.query_params.get("lang") or "bn") == "en" else "bn"
        assignment, err = _reminder_assignment(request, customer_id,
                                               request.query_params.get("partner"))
        if err is not None:
            return err
        state = _reminder_state(biz, assignment, lang)
        return Response(_reminder_json(biz, assignment, state))

    def post(self, request, customer_id):
        biz = request.business
        lang = "en" if (request.query_params.get("lang") or request.data.get("lang") or "bn") == "en" else "bn"
        channel = str(request.data.get("channel") or "").strip().upper()
        if channel not in ("WHATSAPP", "EMAIL"):
            return Response({"ok": False, "error": "bad_channel"},
                            status=status.HTTP_400_BAD_REQUEST)

        assignment, err = _reminder_assignment(request, customer_id,
                                               request.data.get("partner_id"))
        if err is not None:
            return err

        customer = assignment.customer
        state = _reminder_state(biz, assignment, lang)
        amount = state["amount"]
        if amount <= 0:
            return Response({"ok": False, "error": "no_due"},
                            status=status.HTTP_400_BAD_REQUEST)
        if state["cooldown_active"] and str(request.data.get("confirm_repeat") or "") != "1":
            last = state["last"]
            return Response({
                "ok": False,
                "code": "cooldown",
                "error": "cooldown",
                "cooldown_days": COOLDOWN_DAYS,
                "last_reminder": {
                    "at": last.created_at.isoformat() if last else "",
                    "channel": last.channel if last else "",
                    "amount": format_money(last.amount) if last else "",
                },
            }, status=status.HTTP_409_CONFLICT)

        message = state["message"]
        wa_url = ""
        if channel == "WHATSAPP":
            digits = normalize_bd_phone(customer.phone)
            if not digits:
                return Response({"ok": False, "error": "no_phone"},
                                status=status.HTTP_400_BAD_REQUEST)
            recipient = digits
            wa_url = build_whatsapp_url(digits, message)
        else:
            if not customer.email:
                return Response({"ok": False, "error": "no_email"},
                                status=status.HTTP_400_BAD_REQUEST)
            recipient = customer.email
            subject = build_email_subject(business=biz, lang=lang)
            try:
                send_email_reminder(recipient=recipient, subject=subject, message=message)
            except Exception as exc:  # log failure, never crash
                logger.warning("due reminder email failed: %s", exc)
                DueReminder.objects.create(
                    business=biz, partner=assignment.partner, customer=customer,
                    assignment=assignment, channel=channel, recipient=recipient,
                    amount=amount, message=message, language=lang,
                    status=DueReminder.Status.FAILED, error=str(exc)[:500],
                    sent_by=request.user,
                )
                return Response({
                    "ok": False, "code": "email_failed", "error": "email_failed",
                    "detail": str(exc)[:300],
                }, status=status.HTTP_502_BAD_GATEWAY)

        log = DueReminder.objects.create(
            business=biz, partner=assignment.partner, customer=customer,
            assignment=assignment, channel=channel, recipient=recipient,
            amount=amount, message=message, language=lang,
            status=DueReminder.Status.SENT, sent_by=request.user,
        )
        record_audit("due_reminder", log, business=biz, changes={
            "channel": channel, "recipient": recipient,
            "amount": str(amount), "customer": customer.name,
        })
        return Response({
            "ok": True, "channel": channel, "status": log.status,
            "message": message, "wa_url": wa_url, "id": str(log.id),
            "amount": format_money(amount),
        })


# --------------------------------------------------------------------------
# Reset the signed-in user's data (same service the web view calls)
# --------------------------------------------------------------------------

class ResetDataView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.core.reset import ResetFailed, wipe_user_data

        confirm = str(request.data.get("confirm") or "").strip()
        if confirm != "RESET":
            return Response({"ok": False, "error": "confirm_required"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            summary = wipe_user_data(request.user)
        except ResetFailed as exc:
            return Response({"ok": False, "error": f"reset_failed: {exc}"},
                            status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception("user data reset failed")
            return Response({"ok": False, "error": f"reset_failed: {exc}"},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True, **summary})
