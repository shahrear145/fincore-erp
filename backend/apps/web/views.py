from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import logging
import json
from types import SimpleNamespace
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.views.decorators.http import require_POST, require_http_methods

from apps.web import google_auth
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.http import (
    Http404, HttpResponse, HttpResponseRedirect, JsonResponse)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounting.reports import income_statement
from apps.customers.models import Customer, CustomerAssignment, DueReminder
from apps.customers.reminders import (
    COOLDOWN_DAYS, build_email_subject, build_reminder_message,
    build_whatsapp_url, format_money, normalize_bd_phone, send_email_reminder,
)
from apps.core.middleware import record_audit
from apps.core.capabilities import (
    can_write, can_delete, can_manage_users, can_manage_partners,
    can_manage_customers, can_export, is_owner_role, is_viewer, can_book,
)
from apps.organizations.models import Business, BusinessMembership
from apps.personal.models import PersonalAccount, PersonalBudget, PersonalCategory
from apps.personal.service import (
    PersonalServiceError, create_personal_transaction,
    ensure_default_accounts_and_categories, expense_summary_range,
    month_expense_summary, net_worth,
)
from apps.transactions.models import (
    MonthlyBudget, Transaction, TransactionStatus, TransactionType, ExpenseKind,
)


class LoanBookKind:
    """The two separate books: loans (lenders) and investments (investors)."""

    LOAN = "LOAN"
    INVESTMENT = "INVESTMENT"
from apps.transactions.services import (
    TransactionServiceError, create_transaction,
)


logger = logging.getLogger(__name__)


def _require_business(request):
    bid = request.session.get("active_business_id")
    m = None
    if bid:
        m = (BusinessMembership.objects
             .select_related("business")
             .filter(user=request.user, business_id=bid,
                     is_active=True, business__is_active=True)
             .first())
    if m is None:
        # First-time visitors have no business in the session yet — pick the
        # user's first active membership (the same choice web_context makes)
        # instead of showing the "select a business" state.
        m = (BusinessMembership.objects
             .select_related("business")
             .filter(user=request.user, is_active=True,
                     business__is_active=True)
             .order_by("id")
             .first())
        if m is not None:
            request.session["active_business_id"] = str(m.business_id)
    if m is None:
        return None, None
    return m.business, m


def _own_partner(biz, membership, user):
    """The Partner profile linked to this user (PARTNER role only).

    OWNER and ACCOUNTANT see and manage every book in the business; VIEWER
    is read-only and owns nothing.
    """
    if membership and membership.role == BusinessMembership.Role.PARTNER:
        return biz.partners.filter(linked_user=user).first()
    return None


def _reverse_cash_transaction(request, txn, reason=""):
    """Reverse a cash (INCOME/EXPENSE) transaction. ADMIN-only.
    Cash entries don't touch the due book, so reversal only marks REVERSED
    and posts the mirror journal — no records are hard-deleted."""
    from apps.accounting import postings as _postings
    from apps.core.middleware import record_audit

    if txn.status == TransactionStatus.REVERSED:
        raise TransactionServiceError("Already reversed.")

    with db_transaction.atomic():
        txn.status = TransactionStatus.REVERSED
        txn.save(update_fields=["status"])

        # Mirror the automatic journal so the ledger stays balanced.
        _postings.post_reversal_for_transaction(txn, user=request.user)

        record_audit("reversal", txn, business=txn.business, reason=reason,
                     changes={"status": "REVERSED"})
    return txn


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("web:dashboard")
    if request.method == "POST":
        identifier = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=identifier, password=password)
        matched = None
        if user is None:
            from django.contrib.auth import get_user_model
            U = get_user_model()
            matched = U.objects.filter(username__iexact=identifier).first() or \
                      U.objects.filter(email__iexact=identifier).first()
            if matched:
                user = authenticate(request, username=matched.username,
                                    password=password)
        if user is not None:
            login(request, user)
            nxt = request.GET.get("next") or ""
            if not nxt.startswith("/"):
                nxt = "/"
            return redirect(nxt)
        # helpful case: correct password but account not verified/active
        try:
            if matched is None:
                from django.contrib.auth import get_user_model
                U = get_user_model()
                matched = U.objects.filter(username__iexact=identifier).first() or \
                          U.objects.filter(email__iexact=identifier).first()
            if matched and (not matched.is_active) and matched.check_password(password):
                request.session["pending_verify_user_id"] = str(matched.id)
                request.session["pending_verify_email"] = matched.email
                messages.info(request,
                    "Your account is not verified yet. Enter the code sent to your email, or resend it."
                    if request.session.get("ui_lang") == "en" else
                    "আপনার অ্যাকাউন্ট এখনো verify হয়নি। ইমেইলে পাঠানো কোড দিন, অথবা নতুন কোড পাঠান।")
                return redirect("web:register_verify")
        except Exception:
            pass
        messages.error(request,
            "Those credentials did not match. Check your username/email and password."
            if request.session.get("ui_lang") == "en" else
            "ইউজারনেম/ইমেইল বা পাসওয়ার্ড মিলছে না। আবার চেষ্টা করুন।")
    return render(request, "web/login.html")


@require_http_methods(["GET", "POST"])
def google_login_view(request):
    if request.user.is_authenticated:
        return redirect("web:dashboard")
    if not google_auth.is_configured():
        messages.error(request,
                       "গুগল সাইন-ইন সেট করা হয়নি। .env-এ GOOGLE_CLIENT_ID ও "
                       "GOOGLE_CLIENT_SECRET যোগ করুন।")
        return redirect("web:login")
    state = google_auth.make_state()
    request.session["google_oauth_state"] = state
    return HttpResponseRedirect(google_auth.authorization_url(state))


@require_http_methods(["GET"])
def google_callback_view(request):
    if request.user.is_authenticated:
        return redirect("web:dashboard")
    state = request.GET.get("state")
    code = request.GET.get("code")
    if state != request.session.get("google_oauth_state"):
        messages.error(request, "গুগল সাইন-ইন মিলল না। আবার চেষ্টা করুন।")
        return redirect("web:login")
    request.session.pop("google_oauth_state", None)
    if not code:
        messages.error(request, "গুগল থেকে অনুমোদন পাইনি।")
        return redirect("web:login")
    try:
        tokens = google_auth.exchange_code(code)
        profile = google_auth.fetch_profile(tokens.get("access_token", ""))
    except google_auth.GoogleSignInError as e:
        messages.error(request, str(e))
        return redirect("web:login")
    email = (profile.get("email") or "").strip().lower()
    if not profile.get("email_verified", False) or not email:
        messages.error(request, "গুগলের ই-মেইল যাচাই করা যায়নি।")
        return redirect("web:login")
    from django.contrib.auth import get_user_model
    U = get_user_model()
    matched = U.objects.filter(email__iexact=email).first()
    if matched is None:
        messages.error(
            request,
            f"'{email}' দিয়ে কোনো অ্যাকাউন্ট নেই। আগে নতুন অ্যাকাউন্ট তৈরি করুন।")
        return redirect("web:register")
    if not matched.is_active:
        messages.error(request, "আপনার অ্যাকাউন্ট নিষ্ক্রিয়।")
        return redirect("web:login")
    login(request, matched)
    nxt = request.GET.get("next") or ""
    if not nxt.startswith("/"):
        nxt = "/"
    return redirect(nxt)


@require_POST
def logout_view(request):
    logout(request)
    return redirect("web:dashboard")


@login_required
@require_POST
def switch_business(request):
    bid = request.POST.get("business_id")
    ok = BusinessMembership.objects.filter(
        user=request.user, business_id=bid,
        is_active=True, business__is_active=True).exists()
    if ok:
        request.session["active_business_id"] = str(bid)
    nxt = request.POST.get("next") or "/"
    if not nxt.startswith("/"):
        nxt = "/"
    return redirect(nxt)


def _dashboard_period(request, today):
    """Resolve the dashboard's date range from ?preset/from/to.

    Defaults to month-to-date (the behaviour the dashboard always had).
    Returns (preset, dfrom, dto, label) where label is a short, human
    readable range used in the section headings.
    """
    def _d(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    preset = (request.GET.get("preset") or "month").strip()
    dfrom = _d(request.GET.get("from"))
    dto = _d(request.GET.get("to"))
    if preset == "today":
        dfrom = dto = today
    elif preset == "week":
        dfrom = today.fromordinal(today.toordinal() - today.weekday())
        dto = today
    elif preset == "7d":
        dfrom = today.fromordinal(today.toordinal() - 6)
        dto = today
    elif preset == "30d":
        dfrom = today.fromordinal(today.toordinal() - 29)
        dto = today
    elif preset == "last_month":
        first_this = today.replace(day=1)
        last_end = first_this.fromordinal(first_this.toordinal() - 1)
        dfrom = last_end.replace(day=1)
        dto = last_end
    elif preset == "year":
        dfrom = date(today.year, 1, 1)
        dto = today
    else:
        preset = "month" if not (dfrom and dto) else "custom"
        if dfrom is None:
            dfrom = today.replace(day=1)
        if dto is None:
            dto = today
    if dfrom > dto:
        dfrom, dto = dto, dfrom

    en = request.session.get("ui_lang") == "en"
    if preset == "today":
        label = "Today" if en else "আজ"
    elif preset == "week":
        label = "This week" if en else "এই সপ্তাহ"
    elif preset == "7d":
        label = "Last 7 days" if en else "গত ৭ দিন"
    elif preset == "30d":
        label = "Last 30 days" if en else "গত ৩০ দিন"
    elif preset == "month":
        label = "This month" if en else "এই মাস"
    elif preset == "last_month":
        label = "Last month" if en else "গত মাস"
    elif preset == "year":
        label = "This year" if en else "এই বছর"
    else:
        label = f"{dfrom:%d %b} – {dto:%d %b}"
    return preset, dfrom, dto, label


@login_required
def dashboard(request):
    today = timezone.localdate()
    first = today.replace(day=1)
    preset, dfrom, dto, period_label = _dashboard_period(request, today)
    ensure_default_accounts_and_categories(request.user)

    nw = net_worth(request.user)
    msum = expense_summary_range(request.user, dfrom, dto)
    personal = {
        "net_worth": nw,
        "month": msum,
        "accounts": request.user.personal_accounts.filter(
            status=PersonalAccount.Status.ACTIVE),
    }

    business = None
    chart = None
    biz, membership = _require_business(request)
    if biz:
        own = _own_partner(biz, membership, request.user)
        assignments = (CustomerAssignment.objects
                       .filter(customer__business=biz, is_active=True)
                       .select_related("customer", "partner"))
        if own:
            assignments = assignments.filter(partner=own)
        due_rows = []
        total_due = Decimal("0.00")
        for a in assignments:
            d = a.current_due()
            if d > 0:
                due_rows.append({"customer": a.customer, "partner": a.partner,
                                 "due": d})
                total_due += d
        due_rows.sort(key=lambda r: r["due"], reverse=True)

        tx_qs = Transaction.objects.filter(
            business=biz, status=TransactionStatus.COMPLETED)
        if own:
            tx_qs = tx_qs.filter(partner=own)
        month_qs = tx_qs.filter(transaction_date__gte=dfrom,
                                transaction_date__lte=dto)
        totals = {r["transaction_type"]: str(r["t"] or 0) for r in
                  month_qs.values("transaction_type")
                  .annotate(t=Sum("amount"))}
        recent = tx_qs.select_related("customer", "partner").order_by(
            "-transaction_date", "-created_at")[:8]
        try:
            isec = income_statement(biz, from_date=dfrom, to_date=dto)
        except Exception:
            isec = {"total_revenue": "0.00", "total_expenses": "0.00",
                    "net_profit": "0.00"}
        # the money you can touch right now (drawer + mobile/bank)
        from apps.cashbook import services as cashbook
        position = cashbook.business_position(biz)
        # the chart shows only money that really moved (reversed entries and
        # their reversal would otherwise show up as huge bars in both ways)
        try:
            daily = cashbook.real_cash_daily(biz, dfrom, dto)[-14:]
            chart = {
                "labels": [str(r["date"]) for r in daily],
                "inflow": [r["inflow"] for r in daily],
                "outflow": [r["outflow"] for r in daily],
            }
        except Exception:
            chart = {"labels": [], "inflow": [], "outflow": []}
        try:
            from apps.loans.service import summarize_both as loans_summary
            loans = loans_summary(biz)
        except Exception:
            loans = None
        business = {
            "biz": biz, "membership": membership, "total_due": total_due,
            "top_dues": due_rows[:6], "totals": totals, "recent": recent,
            "income_statement": isec, "loans": loans,
            "cash_in_hand": position["cash_in_hand"],
            "cash": position["cash"], "wallet": position["wallet"],
        }

    return render(request, "web/dashboard.html", {
        "personal": personal, "business": business, "today": today,
        "preset": preset, "dfrom": dfrom, "dto": dto,
        "period_label": period_label,
        "chart": chart or {"labels": [], "inflow": [], "outflow": []},
    })


@login_required
@require_http_methods(["GET", "POST"])
def transactions_view(request):
    """নগদ: cash-only entry + calculation/history."""
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business to view cash." if request.session.get("ui_lang") == "en" else "নগদ দেখতে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")

    own = _own_partner(biz, membership, request.user)
    en = request.session.get("ui_lang") == "en"
    from apps.transactions.models import TransactionDateMemory, PaymentMethod, CUSTOMER_REQUIRED_TYPES

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        # ---- DELETE a cash entry (reverse, ledger-safe, owner only) ------
        if action == "delete_cash":
            if not can_delete(membership):
                messages.error(request, "Not allowed." if en else "অনুমতি নেই।")
            else:
                tx_id = (request.POST.get("tx_id") or "").strip()
                reason = (request.POST.get("reason") or "").strip()
                try:
                    txn = (Transaction.objects
                           .filter(pk=tx_id, business=biz,
                                   payment_method=PaymentMethod.CASH,
                                   transaction_type=TransactionType.INCOME)
                           .exclude(status=TransactionStatus.REVERSED)
                           .first())
                    if not txn:
                        messages.error(request, "Entry not found." if en else "এন্ট্রি পাওয়া যায়নি।")
                    else:
                        _reverse_cash_transaction(request, txn, reason)
                        messages.success(request, "Entry reversed." if en else "এন্ট্রি বাতিল করা হয়েছে।")
                except Exception as exc:
                    messages.error(request, (f"Could not delete: {exc}" if en else f"মুছতে পারিনি: {exc}"))
            return redirect("web:transactions")

        # ---- EDIT a cash entry (reverse + repost) -------------------------
        if action == "edit_cash":
            if not can_write(membership):
                messages.error(request, "Not allowed." if en else "অনুমতি নেই।")
            else:
                tx_id = (request.POST.get("tx_id") or "").strip()
                new_amount = (request.POST.get("amount") or "").strip()
                new_date = (request.POST.get("transaction_date") or "").strip()
                new_name = (request.POST.get("name") or "").strip()
                reason = (request.POST.get("reason") or "").strip()
                try:
                    txn = (Transaction.objects
                           .filter(pk=tx_id, business=biz,
                                   payment_method=PaymentMethod.CASH,
                                   transaction_type=TransactionType.INCOME)
                           .exclude(status=TransactionStatus.REVERSED)
                           .first())
                    if not txn:
                        messages.error(request, "Entry not found." if en else "এন্ট্রি পাওয়া যায়নি।")
                    else:
                        # validate new values
                        from apps.transactions.services import _money
                        amount = _money(new_amount)
                        if amount <= 0:
                            raise TransactionServiceError("amount must be greater than zero.")
                        if not new_date:
                            raise TransactionServiceError(
                                {"transaction_date": "A date is required."})

                        actor = SimpleNamespace(user=request.user, business=biz,
                                                business_membership=membership)

                        # reverse old entry
                        _reverse_cash_transaction(
                            request, txn, reason=f"Edit: {reason}")

                        # create new entry with updated values
                        create_transaction(
                            actor, business=biz,
                            amount=amount,
                            transaction_type=TransactionType.INCOME,
                            transaction_date=new_date,
                            payment_method=PaymentMethod.CASH,
                            description=new_name or txn.description or "",
                            client_reference="",
                        )[0]
                        messages.success(request, "Entry updated." if en else "এন্ট্রি আপডেট করা হয়েছে।")
                except TransactionServiceError as exc:
                    err = exc.errors
                    if isinstance(err, dict):
                        err = "; ".join(f"{k}: {v}" for k, v in err.items())
                    messages.error(request, err or str(exc))
                except Exception as exc:
                    messages.error(request, (f"Could not edit: {exc}" if en else f"সম্পাদনা করতে পারিনি: {exc}"))
            return redirect("web:transactions")

        # ---- ADD a new cash entry ------------------------------------------
        if not can_write(membership):
            messages.error(request, "Permission denied." if en else "অনুমতি নেই — শুধু মালিক/হিসাবরক্ষক লেনদেন করতে পারবেন।")
            return redirect("web:transactions")
        actor = SimpleNamespace(user=request.user, business=biz,
                                business_membership=membership)
        try:
            tx_date = request.POST.get("transaction_date") or date.today()
            name = (request.POST.get("name") or request.POST.get("description") or "").strip()
            create_transaction(
                actor, business=biz,
                amount=request.POST.get("amount"),
                transaction_type=TransactionType.INCOME,
                transaction_date=tx_date,
                payment_method=PaymentMethod.CASH,
                description=name,
                client_reference="",
            )[0]
            messages.success(request, "Cash entry saved." if en else "নগদ এন্ট্রি সংরক্ষণ হয়েছে।")
            return redirect("web:transactions")
        except TransactionServiceError as exc:
            err = exc.errors
            if isinstance(err, dict):
                err = "; ".join(f"{k}: {v}" for k, v in err.items())
            messages.error(request, err or str(exc))
        except Exception as exc:
            messages.error(request, (f"Could not save: {exc}" if en else f"সংরক্ষণ করা যায়নি: {exc}"))

    # নগদ হিসাব/হিস্ট্রি: শুধু নগদে পাওয়া টাকা (INCOME), বাতিলকৃত বাদ — বাকি/আদায়/অ্যাডজাস্ট ও খরচ (EXPENSE) কখনো মিশবে না
    def _parse_date(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    # ---- search / sort / page size (all optional, all safe) ----
    search_q = (request.GET.get("q") or "").strip()[:80]
    sort = (request.GET.get("sort") or "new").strip()
    if sort not in {"new", "old", "high", "low"}:
        sort = "new"
    try:
        per = int(request.GET.get("per") or 25)
    except (TypeError, ValueError):
        per = 25
    if per not in {10, 25, 50, 100}:
        per = 25

    base_qs = (Transaction.objects.filter(
        business=biz, payment_method=PaymentMethod.CASH,
        transaction_type=TransactionType.INCOME)
        .exclude(status=TransactionStatus.REVERSED))
    if own:
        base_qs = base_qs.filter(partner=own)

    qs = base_qs
    if date_from:
        qs = qs.filter(transaction_date__gte=date_from)
    if date_to:
        qs = qs.filter(transaction_date__lte=date_to)

    # search: name/note, reference numbers, or an exact amount ("1250", "1,250.50")
    if search_q:
        search_filter = (
            Q(description__icontains=search_q)
            | Q(notes__icontains=search_q)
            | Q(reference_number__icontains=search_q)
            | Q(receipt_no__icontains=search_q)
        )
        try:
            search_amount = Decimal(search_q.replace(",", "").replace("৳", "").strip())
        except (InvalidOperation, ValueError):
            search_amount = None
        if search_amount is not None:
            search_filter |= Q(amount=search_amount)
        qs = qs.filter(search_filter)

    order = {
        "new": ("-transaction_date", "-created_at"),
        "old": ("transaction_date", "created_at"),
        "high": ("-amount", "-transaction_date"),
        "low": ("amount", "-transaction_date"),
    }[sort]
    qs = qs.order_by(*order)

    # ---- totals come from the whole filtered set, not just the visible page ----
    total = qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    cash_count = qs.count()
    all_total = base_qs.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    today = date.today()
    month_total = (qs.filter(transaction_date__year=today.year,
                             transaction_date__month=today.month)
                     .aggregate(s=Sum("amount"))["s"] or Decimal("0.00"))
    month_all_total = (base_qs.filter(transaction_date__year=today.year,
                                      transaction_date__month=today.month)
                             .aggregate(s=Sum("amount"))["s"] or Decimal("0.00"))

    paginator = Paginator(qs, per)
    page_obj = paginator.get_page(request.GET.get("page"))
    rows = list(page_obj.object_list)

    # compact pager window: 1 … 4 5 [6] 7 8 … 20
    def _pager_window(number, num_pages, around=2):
        if num_pages <= 7:
            return list(range(1, num_pages + 1))
        wanted = {1, num_pages}
        for n in range(number - around, number + around + 1):
            if 1 <= n <= num_pages:
                wanted.add(n)
        out, prev = [], 0
        for n in sorted(wanted):
            if prev and n - prev > 1:
                out.append(None)
            out.append(n)
            prev = n
        return out

    biz_row = TransactionDateMemory.resolve(biz, partner=own)
    default_date = (biz_row.last_date.isoformat() if biz_row and biz_row.last_date else today.isoformat())

    return render(request, "web/transactions.html", {
        "rows": rows,
        "page_obj": page_obj,
        "paginator": paginator,
        "pager_pages": _pager_window(page_obj.number, paginator.num_pages),
        "per": per,
        "q": search_q,
        "sort": sort,
        "default_date": default_date,
        "today": today.isoformat(),
        "default_month_start": today.replace(day=1).isoformat(),
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "date_active": bool(date_from or date_to),
        "filter_active": bool(date_from or date_to or search_q),
        "cash_all_total": all_total,
        "cash_month_all_total": month_all_total,
        "cash_total": total,
        "cash_month_total": month_total,
        "cash_count": cash_count,
    })


@login_required
@require_http_methods(["GET", "POST"])
def customers_view(request):
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business to view customers.")
        return redirect("web:dashboard")
    own = _own_partner(biz, membership, request.user)
    en = request.session.get("ui_lang") == "en"
    from apps.transactions.models import TransactionDateMemory
    from apps.partners.models import Partner

    if membership.role == BusinessMembership.Role.PARTNER:
        partners = ([own] if own else [])
    else:
        partners = list(biz.partners.filter(status="ACTIVE").order_by("name"))

    # resolve selected partner (partner-centric book)
    sel_id = request.GET.get("partner") or request.POST.get("partner_id") or request.POST.get("partner")
    selected = None
    if own:
        selected = own
    elif sel_id:
        selected = next((p for p in partners if str(p.id) == str(sel_id)), None)
    elif partners:
        selected = partners[0]

    if selected and membership.role == BusinessMembership.Role.PARTNER and (not own or selected.id != own.id):
        selected = own

    can_manage = can_manage_customers(membership)

    if request.method == "POST":
        if not can_write(membership):
            messages.error(request, "Permission denied." if en else "অনুমতি নেই — শুধু মালিক/হিসাবরক্ষক লেনদেন করতে পারবেন।")
            return redirect(request.path)
        action = (request.POST.get("action") or "").strip()
        actor = SimpleNamespace(user=request.user, business=biz,
                                business_membership=membership)

        if action == "add_partner":
            if not can_manage_partners(membership):
                messages.error(request, "Not allowed." if en else "অনুমতি নেই।")
                return redirect(request.path)
            pname = (request.POST.get("partner_name") or "").strip()
            pphone = (request.POST.get("partner_phone") or "").strip()
            if not pname:
                messages.error(request, "Partner name is required." if en else "পার্টনারের নাম দিন।")
                return redirect(request.path)
            try:
                from apps.partners.models import Partner
                partner = Partner.objects.create(
                    business=biz, name=pname, phone=pphone,
                    status=Partner.Status.ACTIVE, created_by=request.user,
                )
                messages.success(request,
                    (f"Partner '{partner.name}' added." if en else f"পার্টনার '{partner.name}' যোগ হয়েছে।"))
                try:
                    record_audit("create", partner, business=biz)
                except Exception:
                    pass
                return redirect(f"{request.path}?partner={partner.id}")
            except Exception as exc:
                messages.error(request, (f"Could not add partner: {exc}" if en else f"পার্টনার যোগ করা যায়নি: {exc}"))
                return redirect(request.path)

        if not selected:
            messages.info(request, "Select a partner first." if en else "আগে একটি পার্টনার নির্বাচন করুন।")
            return redirect(request.path)

        try:
            if action == "add_baki" or action == "add_name" or action == "add_due":
                if action in ("add_name", "add_baki") and not can_manage:
                    raise PermissionError("Not allowed")
                # unified: name + phone + email + amount + date + description + type
                name = (request.POST.get("name") or "").strip()
                phone = (request.POST.get("phone") or "").strip()
                email = (request.POST.get("email") or "").strip()
                description = (request.POST.get("description") or "").strip()
                tx_date = request.POST.get("transaction_date") or date.today()
                cust_id = (request.POST.get("customer_id") or "").strip()
                ttype = request.POST.get("transaction_type") or TransactionType.CREDIT
                if ttype not in (TransactionType.CREDIT, TransactionType.PAYMENT):
                    ttype = TransactionType.CREDIT

                if cust_id:
                    customer = Customer.objects.filter(pk=cust_id, business=biz).first()
                    if not customer:
                        messages.error(request, "Name not found." if en else "নাম পাওয়া যায়নি।")
                        txd = str(tx_date) if tx_date else (request.POST.get("pdate") or "").strip()
                        back = f"{request.path}?partner={selected.id}" + (f"&pdate={txd}" if txd else "")
                        return redirect(back)
                else:
                    if not name:
                        messages.error(request, "Name is required." if en else "নাম দিন।")
                        txd = str(tx_date) if tx_date else (request.POST.get("pdate") or "").strip()
                        back = f"{request.path}?partner={selected.id}" + (f"&pdate={txd}" if txd else "")
                        return redirect(back)
                    # same name, same date = same customer (accumulate); different date = separate customer/history
                    tx_date_lookup = tx_date
                    try:
                        if hasattr(tx_date_lookup, 'isoformat'):
                            tx_date_lookup = tx_date_lookup
                        else:
                            from datetime import datetime as _dt
                            tx_date_lookup = _dt.strptime(str(tx_date_lookup)[:10], "%Y-%m-%d").date()
                    except Exception:
                        tx_date_lookup = None
                    customer = None
                    if tx_date_lookup:
                        customer = (Customer.objects
                            .filter(business=biz, name=name,
                                    assignments__partner=selected, assignments__is_active=True,
                                    transactions__partner=selected,
                                    transactions__transaction_date=tx_date_lookup)
                            .exclude(transactions__status=TransactionStatus.REVERSED)
                            .distinct().first())
                    created = False
                    if not customer:
                        customer = Customer.objects.create(
                            business=biz, name=name,
                            phone=phone, email=email, status="ACTIVE",
                        )
                        created = True
                    updates = []
                    if getattr(customer, "serial_no", None) is None:
                        from django.db.models import Max
                        mx = Customer.objects.filter(business=biz).aggregate(m=Max("serial_no"))["m"] or 0
                        customer.serial_no = mx + 1
                        updates.append("serial_no")
                    if phone and phone != (customer.phone or ""):
                        customer.phone = phone; updates.append("phone")
                    if email and email != (customer.email or ""):
                        customer.email = email; updates.append("email")
                    if updates:
                        customer.save(update_fields=updates)

                CustomerAssignment.objects.get_or_create(
                    customer=customer, partner=selected,
                    defaults={"opening_due": Decimal("0.00")},
                )
                # reactivate if previously removed (fresh book for this partner)
                CustomerAssignment.objects.filter(
                    customer=customer, partner=selected, is_active=False
                ).update(is_active=True)

                amount = (request.POST.get("amount") or "").strip()
                if amount:
                    create_transaction(
                        actor, business=biz,
                        amount=amount,
                        transaction_type=ttype,
                        transaction_date=tx_date,
                        partner_id=str(selected.id),
                        customer_id=str(customer.id),
                        payment_method="CASH",
                        description=description,
                        client_reference="",
                    )
                    # ---- notify the business: a new বাকি was posted -------
                    if ttype == TransactionType.CREDIT:
                        try:
                            from apps.web import notifications as notif
                            notif.notify_admins(
                                biz, kind=notif.Notification.Kind.DUE_INCREASED,
                                title=(f"বাকি বাড়ল — {customer.name}"
                                       if not en
                                       else f"Due increased — {customer.name}"),
                                body=(f"{customer.name} এর বাকি ৳{Decimal(str(amount)):,.2f} যোগ হয়েছে।"
                                      if not en
                                      else f"Baki of ৳{Decimal(str(amount)):,.2f} was added for {customer.name}."),
                                link=(f"/customers/?partner={selected.id}&history={customer.id}"),
                                amount=Decimal(str(amount)),
                            )
                        except Exception:
                            logger.exception("credit-due notification failed")

                    if ttype == TransactionType.PAYMENT:
                        messages.success(request,
                            (f"Payment received for {customer.name}." if en else f"{customer.name} এর পরিশোধ/আদায় গ্রহণ হয়েছে।"))
                    else:
                        messages.success(request,
                            (f"Baki added for {customer.name}." if en else f"{customer.name} এর বাকি যোগ হয়েছে।"))
                else:
                    from apps.transactions.models import TransactionDateMemory
                    TransactionDateMemory.touch(biz, tx_date, partner=selected)
                    messages.success(request,
                        (f"{customer.name} saved." if en else f"{customer.name} সংরক্ষণ হয়েছে।"))
                txd = (request.POST.get("pdate") or "").strip() or (str(tx_date) if tx_date else "")
                back = f"{request.path}?partner={selected.id}" + (f"&pdate={txd}" if txd else "")
                return redirect(back)

            elif action == "edit_name":
                if not can_manage:
                    raise PermissionError("Not allowed")
                cust_id = (request.POST.get("customer_id") or "").strip()
                customer = Customer.objects.filter(pk=cust_id, business=biz).first()
                if not customer:
                    messages.error(request, "Name not found." if en else "নাম পাওয়া যায়নি।")
                    return redirect(f"{request.path}?partner={selected.id}")
                new_name = (request.POST.get("name") or "").strip()
                if new_name:
                    customer.name = new_name
                customer.phone = (request.POST.get("phone") or "").strip()
                customer.email = (request.POST.get("email") or "").strip()
                ser = (request.POST.get("serial_no") or "").strip()
                if ser.isdigit():
                    customer.serial_no = int(ser)
                customer.save()
                try:
                    record_audit("update", customer, business=biz)
                except Exception:
                    pass
                messages.success(request, "Saved." if en else "সংরক্ষণ হয়েছে।")
                pdate = (request.POST.get("pdate") or "").strip()
                back = f"{request.path}?partner={selected.id}" + (f"&pdate={pdate}" if pdate else "")
                return redirect(back + f"&history={customer.id}")

            elif action == "adjust_baki":
                cust_id = (request.POST.get("customer_id") or "").strip()
                customer = Customer.objects.filter(pk=cust_id, business=biz).first()
                if not customer:
                    messages.error(request, "Name not found." if en else "নাম পাওয়া যায়নি।")
                    return redirect(f"{request.path}?partner={selected.id}")
                tx_date = request.POST.get("transaction_date") or date.today()
                set_val = (request.POST.get("set_baki_amount") or "").strip()
                try:
                    if set_val != "":
                        # direct set: compute delta from current due (ledger-safe adjustment)
                        asn = CustomerAssignment.objects.filter(
                            partner=selected, customer=customer).first()
                        current = asn.current_due() if asn else Decimal("0.00")
                        target = Decimal(str(set_val)).quantize(Decimal("0.01"))
                        if target < 0:
                            target = Decimal("0.00")
                        delta = (target - current).quantize(Decimal("0.01"))
                        if delta == 0:
                            messages.info(request,
                                ("Already set to that amount." if en else "ইতিমধ্যে এই টাকাই আছে।"))
                        else:
                            direction = "UP" if delta > 0 else "DOWN"
                            create_transaction(
                                actor, business=biz,
                                amount=abs(delta),
                                transaction_type=TransactionType.ADJUSTMENT,
                                adjustment_direction=direction,
                                transaction_date=tx_date,
                                partner_id=str(selected.id),
                                customer_id=str(customer.id),
                                payment_method="CASH",
                                description=(request.POST.get("description") or "বাকি সরাসরি সংশোধন").strip(),
                                client_reference="",
                            )
                            messages.success(request,
                                (f"Remaining baki set to ৳{target}." if en else f"বাকি এখন ৳{target} করা হয়েছে।"))
                    else:
                        direction = (request.POST.get("adjustment_direction") or "UP").strip()
                        if direction not in ("UP", "DOWN"):
                            direction = "UP"
                        create_transaction(
                            actor, business=biz,
                            amount=request.POST.get("adjust_amount"),
                            transaction_type=TransactionType.ADJUSTMENT,
                            adjustment_direction=direction,
                            transaction_date=tx_date,
                            partner_id=str(selected.id),
                            customer_id=str(customer.id),
                            payment_method="CASH",
                            description=(request.POST.get("description") or "বাকি সংশোধন").strip(),
                            client_reference="",
                        )
                        messages.success(request, "Baki adjusted." if en else "বাকি সংশোধন হয়েছে।")
                except Exception as exc:
                    err = getattr(exc, "errors", None) or str(exc)
                    if isinstance(err, dict):
                        err = "; ".join(f"{k}: {v}" for k, v in err.items())
                    messages.error(request, (f"Could not adjust: {err}" if en else f"সংশোধন করা যায়নি: {err}"))
                pdate = (request.POST.get("pdate") or "").strip()
                back = f"{request.path}?partner={selected.id}" + (f"&pdate={pdate}" if pdate else "")
                return redirect(back)

            elif action == "delete_name":
                if not can_manage:
                    raise PermissionError("Not allowed")
                from apps.transactions.services import reverse_transaction
                cust_id = (request.POST.get("customer_id") or "").strip()
                customer = Customer.objects.filter(pk=cust_id, business=biz).first()
                if not customer:
                    messages.error(request, "Name not found." if en else "নাম পাওয়া যায়নি।")
                    return redirect(f"{request.path}?partner={selected.id}")
                # reverse history (ledger-safe) so name + history are removed together
                request.business_membership = membership
                request.active_business = biz
                txns = (Transaction.objects
                        .filter(business=biz, partner=selected, customer=customer)
                        .exclude(status=TransactionStatus.REVERSED))
                # reverse non-credit first to free allocations, then credits
                order = {TransactionType.PAYMENT: 0, TransactionType.REFUND: 0,
                         TransactionType.ADJUSTMENT: 0, TransactionType.ADVANCE: 0,
                         TransactionType.CREDIT: 1}
                txns = sorted(txns, key=lambda t: (order.get(t.transaction_type, 0), t.created_at))
                rev_ok = rev_fail = 0
                last_err = ""
                for tx in txns:
                    try:
                        tx.refresh_from_db()
                        if tx.status == TransactionStatus.REVERSED:
                            continue
                        reverse_transaction(request, tx, reason=f"Deleted name: {customer.name}")
                        rev_ok += 1
                    except Exception as exc:
                        rev_fail += 1
                        err = getattr(exc, "errors", None) or str(exc)
                        if isinstance(err, dict):
                            err = "; ".join(f"{k}: {v}" for k, v in err.items())
                        last_err = err
                asn = CustomerAssignment.objects.filter(
                    partner=selected, customer=customer).first()
                if asn and asn.is_active:
                    asn.is_active = False
                    asn.save(update_fields=["is_active"])
                    try:
                        record_audit("update", asn, business=biz)
                    except Exception:
                        pass

                # The khata is closed.  If this was the customer's last open
                # book and nothing is owed anywhere, remove the customer too.
                # Otherwise a "deleted" name keeps showing up in searches,
                # exports and the audit as a partner-less customer.
                still_open = customer.assignments.filter(is_active=True).exists()
                still_due = customer.total_due()
                name_removed = False
                if not rev_fail and not still_open and still_due == 0:
                    customer.delete()  # soft delete: history rows stay intact
                    try:
                        record_audit("delete", customer, business=biz)
                    except Exception:
                        pass
                    name_removed = True

                if rev_fail:
                    messages.warning(request,
                        (f"{customer.name} removed; {rev_ok} entries reversed, {rev_fail} could not be reversed: {last_err}" if en else f"{customer.name} সরানো হয়েছে; {rev_ok}টি এন্ট্রি বাতিল, {rev_fail}টি বাতিল হয়নি: {last_err}"))
                elif name_removed:
                    messages.success(request,
                        (f"{customer.name} and their history were removed ({rev_ok} entries reversed)." if en else f"{customer.name} ও তার হিস্ট্রি সরানো হয়েছে ({rev_ok}টি এন্ট্রি বাতিল)।"))
                elif still_open:
                    messages.success(request,
                        (f"{customer.name}'s book with this partner is closed; the name was kept because another partner's book is still open." if en else f"{customer.name} এর এই খাতা বন্ধ হয়েছে; অন্য পার্টনারের খাতা খোলা আছে বলে নামটি রাখা হয়েছে।"))
                else:
                    messages.warning(request,
                        (f"{customer.name}'s book is closed, but the name was kept because {still_due:,.2f} is still due." if en else f"{customer.name} এর খাতা বন্ধ হয়েছে, তবে {still_due:,.2f} বাকি থাকায় নামটি মুছিনি।"))
                pdate = (request.POST.get("pdate") or "").strip()
                return redirect(f"{request.path}?partner={selected.id}" + (f"&pdate={pdate}" if pdate else ""))

            elif action == "delete_tx":
                from apps.transactions.services import reverse_transaction
                tx_id = (request.POST.get("tx_id") or "").strip()
                pdate = (request.POST.get("pdate") or "").strip()
                txn = Transaction.objects.filter(pk=tx_id, business=biz, partner=selected).first()
                if not txn:
                    messages.error(request, "Entry not found." if en else "এন্ট্রি পাওয়া যায়নি।")
                else:
                    try:
                        request.business_membership = membership
                        request.active_business = biz
                        reverse_transaction(request, txn, reason="Deleted from khata")
                        messages.success(request, "Entry removed/reversed." if en else "এন্ট্রি বাতিল/সরানো হয়েছে।")
                    except Exception as exc:
                        err = getattr(exc, "errors", None) or str(exc)
                        if isinstance(err, dict):
                            err = "; ".join(f"{k}: {v}" for k, v in err.items())
                        messages.error(request, (f"Could not delete: {err}" if en else f"মুছতে পারিনি: {err}"))
                return redirect(f"{request.path}?partner={selected.id}" + (f"&pdate={pdate}" if pdate else ""))

            elif action == "edit_tx_amount":
                if not can_manage:
                    raise PermissionError("Not allowed")
                from apps.transactions.services import reverse_transaction
                tx_id = (request.POST.get("tx_id") or "").strip()
                pdate = (request.POST.get("pdate") or "").strip()
                new_amount = (request.POST.get("new_amount") or "").strip()
                new_note = (request.POST.get("new_note") or "").strip()[:500]
                history_id = (request.POST.get("history") or "").strip()
                due_types = (TransactionType.CREDIT, TransactionType.PAYMENT,
                             TransactionType.ADJUSTMENT, TransactionType.ADVANCE,
                             TransactionType.REFUND)
                txn = Transaction.objects.filter(
                    pk=tx_id, business=biz, partner=selected,
                    transaction_type__in=due_types).exclude(
                    status=TransactionStatus.REVERSED).first()
                if not txn:
                    messages.error(request, "Entry not found." if en else "এন্ট্রি পাওয়া যায়নি।")
                    return redirect(f"{request.path}?partner={selected.id}" + (f"&pdate={pdate}" if pdate else ""))
                try:
                    new_amt = Decimal(str(new_amount)).quantize(Decimal("0.01"))
                    if new_amt <= 0:
                        raise ValueError("Amount must be positive")
                    old_note = (txn.description or "").strip()
                    if new_amt == txn.amount and (not new_note or new_note == old_note):
                        messages.info(request, ("Nothing changed." if en else "কিছু বদলায়নি।"))
                        return redirect(f"{request.path}?partner={selected.id}" + (f"&pdate={pdate}" if pdate else "") + (f"&history={history_id}" if history_id else ""))
                    old_amt = txn.amount
                    request.business_membership = membership
                    request.active_business = biz
                    # reverse the old entry and post the corrected one, so the
                    # ledger, the baki book and the cash balance stay in step
                    reverse_transaction(request, txn, reason=f"Amount corrected {old_amt}→{new_amt}")
                    if new_note:
                        desc = new_note
                    elif old_note:
                        desc = f"{old_note} (আগে ৳{old_amt})"
                    else:
                        desc = f"সংশোধিত (আগে ৳{old_amt})"
                    create_transaction(
                        actor, business=biz,
                        amount=new_amt,
                        transaction_type=txn.transaction_type,
                        transaction_date=txn.transaction_date,
                        partner_id=str(selected.id),
                        customer_id=str(txn.customer_id) if txn.customer_id else "",
                        payment_method=txn.payment_method or "CASH",
                        adjustment_direction=txn.adjustment_direction,
                        description=desc[:500],
                        client_reference="",
                    )
                    messages.success(request,
                        (f"Entry updated: ৳{old_amt} → ৳{new_amt}." if en else f"এন্ট্রি ঠিক হয়েছে: ৳{old_amt} → ৳{new_amt}।"))
                except Exception as exc:
                    err = getattr(exc, "errors", None) or str(exc)
                    if isinstance(err, dict):
                        err = "; ".join(f"{k}: {v}" for k, v in err.items())
                    messages.error(request, (f"Could not fix: {err}" if en else f"ঠিক করা যায়নি: {err}"))
                return redirect(f"{request.path}?partner={selected.id}" + (f"&pdate={pdate}" if pdate else "") + (f"&history={history_id}" if history_id else ""))

            elif action == "purge_tx":
                """মুছে ফেলা — the entry goes for good, the money stays right."""
                if not can_manage:
                    raise PermissionError("Not allowed")
                from apps.customers.services import can_purge, purge_due_transaction
                from apps.transactions.services import reverse_transaction
                tx_id = (request.POST.get("tx_id") or "").strip()
                pdate = (request.POST.get("pdate") or "").strip()
                history_id = (request.POST.get("history") or "").strip()
                due_types = (TransactionType.CREDIT, TransactionType.PAYMENT,
                             TransactionType.ADJUSTMENT, TransactionType.ADVANCE,
                             TransactionType.REFUND)
                txn = Transaction.objects.filter(
                    pk=tx_id, business=biz, partner=selected,
                    transaction_type__in=due_types).exclude(
                    status=TransactionStatus.REVERSED).first()
                if not txn:
                    messages.error(request, "Entry not found." if en else "এন্ট্রি পাওয়া যায়নি।")
                else:
                    try:
                        request.business_membership = membership
                        request.active_business = biz
                        if can_purge(txn):
                            # complete erasure: row, due movement and postings
                            purge_due_transaction(txn, user=request.user)
                            messages.success(request,
                                (f"Entry deleted (৳{txn.amount:,.2f})." if en
                                 else f"এন্ট্রি মুছে ফেলা হয়েছে (৳{txn.amount:,.2f})।"))
                        else:
                            # money is linked to it — reverse instead, so no
                            # taka is ever lost or doubled
                            reverse_transaction(request, txn,
                                                reason="Deleted from baki history")
                            messages.success(request,
                                (f"Entry cancelled (৳{txn.amount:,.2f}) — it was linked to a collection, so it was reversed to keep the money correct."
                                 if en else
                                 f"এন্ট্রি বাতিল হয়েছে (৳{txn.amount:,.2f}) — এটি আদায়ের সাথে জড়িত ছিল, তাই টাকার হিসাব ঠিক রাখতে বাতিল করা হয়েছে।"))
                    except Exception as exc:
                        err = getattr(exc, "errors", None) or str(exc)
                        if isinstance(err, dict):
                            err = "; ".join(f"{k}: {v}" for k, v in err.items())
                        messages.error(request, (f"Could not delete: {err}" if en else f"মুছতে পারিনি: {err}"))
                return redirect(f"{request.path}?partner={selected.id}" + (f"&pdate={pdate}" if pdate else "") + (f"&history={history_id}" if history_id else ""))
        except TransactionServiceError as exc:
            err = exc.errors
            if isinstance(err, dict):
                err = "; ".join(f"{k}: {v}" for k, v in err.items())
            messages.error(request, err or str(exc))
        except PermissionError:
            messages.error(request, "Not allowed." if en else "অনুমতি নেই।")
        except Exception as exc:
            messages.error(request, (f"Could not save: {exc}" if en else f"সংরক্ষণ করা যায়নি: {exc}"))
        return redirect(f"{request.path}?partner={selected.id}" if selected else request.path)

    # build partner book (book=active names; total_due=all assignments for accurate মোট বাকি)
    book = []
    total_due = Decimal("0.00")
    if selected:
        all_assigns = list(CustomerAssignment.objects
                           .filter(partner=selected, customer__business=biz)
                           .select_related("customer"))
        for a in all_assigns:
            total_due += a.current_due()
        assigns = [a for a in all_assigns if a.is_active]
        assigns.sort(key=lambda a: (a.customer.name or "").lower())
        for a in assigns:
            d = a.current_due()
            book.append({"assignment": a, "customer": a.customer, "due": d})

    # side history (customer click -> name history, as before)
    history_customer = None
    history_rows = []
    history_due = None
    history_reminders = []
    history_assignment_id = ""
    if selected and request.GET.get("history"):
        try:
            # only customers actually assigned to THIS partner's book —
            # naming another partner's customer in the URL renders nothing
            history_customer = (Customer.objects
                                .filter(pk=request.GET.get("history"),
                                        business=biz,
                                        assignments__partner=selected)
                                .first())
        except (ValueError, ValidationError):
            history_customer = None  # malformed id -> drawer simply stays shut
        if history_customer:
            history_rows = list(
                Transaction.objects.filter(
                    business=biz, partner=selected, customer=history_customer)
                .exclude(status=TransactionStatus.REVERSED)
                .order_by("-transaction_date", "-created_at")[:200]
            )
            asn = CustomerAssignment.objects.filter(
                partner=selected, customer=history_customer).first()
            history_due = asn.current_due() if asn else Decimal("0.00")
            history_assignment_id = str(asn.id) if asn else ""
            # reminders already sent for this customer in this partner's book
            history_reminders = list(
                DueReminder.objects.filter(business=biz, partner=selected,
                                          customer=history_customer)
                .select_related("sent_by")
                .order_by("-created_at")[:10]
            )

    if selected:
        mem = TransactionDateMemory.resolve(biz, partner=selected)
        default_date = mem.last_date.isoformat() if mem and mem.last_date else date.today().isoformat()
    else:
        mem = TransactionDateMemory.resolve(biz, partner=None)
        default_date = mem.last_date.isoformat() if mem and mem.last_date else date.today().isoformat()

    # overview counts for admin AND accountant (all partners) - total due includes all assignments for accuracy
    overview = []
    if membership.role in (BusinessMembership.Role.ADMIN, BusinessMembership.Role.ACCOUNTANT):
        for p in partners:
            all_a = list(CustomerAssignment.objects.filter(partner=p, customer__business=biz))
            asum = sum((a.current_due() for a in all_a), Decimal("0.00"))
            overview.append({"partner": p, "total_due": asum,
                             "count": sum(1 for a in all_a if a.is_active)})

    # partner-level date-wise বাকি খাতা (only baki/CREDIT) - payments show in customer side history
    partner_dates = []
    pdate = (request.GET.get("pdate") or "").strip()
    pfrom = (request.GET.get("pfrom") or "").strip()
    pto = (request.GET.get("pto") or "").strip()
    try:
        pfrom_d = date.fromisoformat(pfrom) if pfrom else None
    except ValueError:
        pfrom, pfrom_d = "", None
    try:
        pto_d = date.fromisoformat(pto) if pto else None
    except ValueError:
        pto, pto_d = "", None
    if pfrom_d and pto_d and pfrom_d > pto_d:
        # friendly — swap instead of erroring
        pfrom, pto = pto, pfrom
        pfrom_d, pto_d = pto_d, pfrom_d
    range_active = bool(pfrom_d or pto_d)
    partner_day_rows = []
    partner_day_total = Decimal("0.00")
    partner_day_remaining_total = Decimal("0.00")
    if selected:
        pbase = Transaction.objects.filter(
            business=biz, partner=selected,
            transaction_type=TransactionType.CREDIT).exclude(status=TransactionStatus.REVERSED)
        dates_qs = (pbase.order_by("-transaction_date")
                    .values_list("transaction_date", flat=True)
                    .distinct())
        if pfrom_d:
            dates_qs = dates_qs.filter(transaction_date__gte=pfrom_d)
        if pto_d:
            dates_qs = dates_qs.filter(transaction_date__lte=pto_d)
        # default stays the latest 14; an explicit range shows everything in it
        partner_dates = list(dates_qs[:366 if range_active else 14])
        if pdate:
            tx_rows = list(
                pbase.filter(transaction_date=pdate)
                .select_related("customer")
                .order_by("customer__name", "created_at")
            )
            grouped = {}
            for t in tx_rows:
                cid = str(t.customer_id)
                if cid not in grouped:
                    grouped[cid] = {
                        "customer": t.customer,
                        "amount": Decimal("0.00"),  # ager baki / accumulated dues today
                        "remaining": Decimal("0.00"),
                        "tx_ids": [],
                        "last_tx": t,
                        "first_at": t.created_at,  # stable: first added stays on top
                    }
                grouped[cid]["amount"] += t.amount
                grouped[cid]["tx_ids"].append(t.id)
                if t.created_at < grouped[cid]["first_at"]:
                    grouped[cid]["first_at"] = t.created_at
                if grouped[cid]["last_tx"].created_at < t.created_at:
                    grouped[cid]["last_tx"] = t
            # prefetch assignments once (avoid per-row queries)
            cust_ids = [g["customer"].id for g in grouped.values()]
            asn_map = {a.customer_id: a for a in CustomerAssignment.objects.filter(
                partner=selected, customer_id__in=cust_ids)}
            partner_day_rows = []
            for cid, g in grouped.items():
                asn = asn_map.get(g["customer"].id)
                g["remaining"] = asn.current_due() if asn else Decimal("0.00")
                g["assignment_id"] = str(asn.id) if asn else ""
                g["t"] = g["last_tx"]
                g["tx_count"] = len(g["tx_ids"])
                # only allow single-entry amount fix when one tx; otherwise fix via baki adjustment
                g["edit_tx_id"] = g["tx_ids"][0] if len(g["tx_ids"]) == 1 else ""
                g["edit_tx_amount"] = g["amount"] if len(g["tx_ids"]) == 1 else ""
                partner_day_rows.append(g)
            # first added stays first/top, new additions go below (stable position)
            partner_day_rows.sort(key=lambda x: x["first_at"])
            # last reminder per customer of this book (one query) — shown as a
            # small badge so the shop knows who was already chased
            if partner_day_rows:
                latest_rem = {}
                for rem in (DueReminder.objects
                            .filter(business=biz, partner=selected,
                                    customer_id__in=[g["customer"].id
                                                     for g in partner_day_rows])
                            .order_by("-created_at")):
                    latest_rem.setdefault(rem.customer_id, rem)
                for g in partner_day_rows:
                    rem = latest_rem.get(g["customer"].id)
                    g["last_reminder_at"] = rem.created_at if rem else None
                    g["last_reminder_channel"] = rem.channel if rem else ""
            partner_day_total = sum((r["amount"] for r in partner_day_rows), Decimal("0.00"))
            partner_day_remaining_total = sum((r["remaining"] for r in partner_day_rows), Decimal("0.00"))

    if request.GET.get("partial") == "history":
        # No-reload history drawer: return only the drawer fragment. The
        # client falls back to a full reload when this fails (400).
        if not (selected and history_customer):
            return HttpResponse(status=400)
        return render(request, "web/partials/history_drawer.html", {
            "selected": selected,
            "history_customer": history_customer,
            "history_rows": history_rows,
            "history_due": history_due,
            "history_reminders": history_reminders,
            "history_assignment_id": history_assignment_id,
            "pdate": pdate,
        })

    return render(request, "web/customers.html", {
        "partners": partners,
        "selected": selected,
        "book": book,
        "total_due": total_due,
        "default_date": default_date,
        "overview": overview,
        "can_manage": can_manage,
        "history_customer": history_customer,
        "history_rows": history_rows,
        "history_due": history_due,
        "history_reminders": history_reminders,
        "history_assignment_id": history_assignment_id,
        "partner_dates": partner_dates,
        "pdate": pdate,
        "pfrom": pfrom,
        "pto": pto,
        "range_active": range_active,
        "partner_day_rows": partner_day_rows,
        "partner_day_total": partner_day_total,
        "partner_day_remaining_total": partner_day_remaining_total,
        "rem_i18n": _reminder_i18n(request.session.get("ui_lang") == "en"),
    })


def _reminder_i18n(en=False):
    """UI strings for the reminder modal (bilingual, shipped as JSON)."""
    if en:
        return {
            "no_phone": "This customer has no usable phone number.",
            "no_email": "This customer has no email address.",
            "no_due": "This customer has no due right now.",
            "cooldown": "Already reminded {days} days ago ({date}). Send again anyway?",
            "cooldown_blocked": "Not sent — this customer was reminded recently.",
            "sending": "Sending…",
            "failed": "Could not send the reminder.",
            "copied": "Message copied.",
            "wa_sent": "WhatsApp opened with the message ready — press Send there.",
            "email_sent": "Reminder emailed and recorded.",
        }
    return {
        "no_phone": "এই কাস্টমারের কোনো ফোন নম্বর নেই।",
        "no_email": "এই কাস্টমারের ইমেইল নেই।",
        "no_due": "এই কাস্টমারের এখন কোনো বাকি নেই।",
        "cooldown": "{days} দিনের মধ্যেই মনে করিয়ে দেওয়া হয়েছে ({date})। তবুও পাঠাবেন?",
        "cooldown_blocked": "সাম্প্রতিক রিমাইন্ডারের কারণে পাঠানো হয়নি।",
        "sending": "পাঠানো হচ্ছে…",
        "failed": "রিমাইন্ডার পাঠানো যায়নি।",
        "copied": "মেসেজ কপি হয়েছে।",
        "wa_sent": "WhatsApp-এ মেসেজ খোলা হয়েছে — সেখানে Send চাপুন।",
        "email_sent": "ইমেইল পাঠানো হয়েছে এবং লেখা হয়েছে।",
    }


def _scoped_assignment(request, biz, membership, assignment_id):
    """Fetch one assignment the caller may act on — server-enforced book rule.

    Partner-role users are locked to their own partner's book; anything
    else (another partner's customer, another business) simply does not
    exist for them, so a guessed URL never leaks a name, a phone number
    or an amount.
    """
    assignment = (CustomerAssignment.objects
                  .select_related("customer", "partner")
                  .filter(pk=assignment_id, customer__business=biz)
                  .first())
    if assignment is None:
        raise Http404("No such assignment")
    if membership.role == BusinessMembership.Role.PARTNER:
        own = _own_partner(biz, membership, request.user)
        if own is None or assignment.partner_id != own.id:
            raise Http404("No such assignment")
    return assignment


def _reminder_state(biz, assignment, request):
    """Amount, message and cooldown state for one reminder preview."""
    customer = assignment.customer
    amount = assignment.current_due()
    lang = "en" if request.session.get("ui_lang") == "en" else "bn"
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


def _reminder_json(biz, assignment, state):
    customer = assignment.customer
    last = state["last"]
    return {
        "ok": True,
        "name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
        "amount": format_money(state["amount"]),
        "partner": assignment.partner.name,
        "business": biz.name,
        "message": state["message"],
        "lang": state["lang"],
        "can_whatsapp": bool(normalize_bd_phone(customer.phone)),
        "can_email": bool(customer.email),
        "cooldown_active": state["cooldown_active"],
        "cooldown_days": COOLDOWN_DAYS,
        "last_reminder": ({
            "at": last.created_at.isoformat(),
            "channel": last.channel,
            "amount": format_money(last.amount),
        } if last else None),
    }


@require_http_methods(["GET"])
def reminder_preview_view(request, assignment_id):
    """Preview the reminder text for one customer with an open due."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "login_required"}, status=401)
    biz, membership = _require_business(request)
    if not biz:
        return JsonResponse({"ok": False, "error": "no_business"}, status=400)
    assignment = _scoped_assignment(request, biz, membership, assignment_id)
    state = _reminder_state(biz, assignment, request)
    return JsonResponse(_reminder_json(biz, assignment, state))


@require_POST
def reminder_send_view(request, assignment_id):
    """Hand the reminder to WhatsApp or email, and log the attempt."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "login_required"}, status=401)
    biz, membership = _require_business(request)
    if not biz:
        return JsonResponse({"ok": False, "error": "no_business"}, status=400)
    assignment = _scoped_assignment(request, biz, membership, assignment_id)

    channel = (request.POST.get("channel") or "").strip().upper()
    if channel not in ("WHATSAPP", "EMAIL"):
        return JsonResponse({"ok": False, "error": "bad_channel"}, status=400)

    customer = assignment.customer
    state = _reminder_state(biz, assignment, request)
    amount = state["amount"]
    if amount <= 0:
        return JsonResponse({"ok": False, "error": "no_due"}, status=400)

    # Anti-spam: a customer reminded within the cooldown window needs an
    # explicit confirmation from the user before we send again.
    if state["cooldown_active"] and request.POST.get("confirm_repeat") != "1":
        return JsonResponse({
            "ok": False, "code": "cooldown", "error": "cooldown",
            "cooldown_days": COOLDOWN_DAYS,
            "last_reminder": {
                "at": state["last"].created_at.isoformat(),
                "channel": state["last"].channel,
                "amount": format_money(state["last"].amount),
            },
        }, status=409)

    lang = state["lang"]
    message = state["message"]
    wa_url = ""

    if channel == "WHATSAPP":
        digits = normalize_bd_phone(customer.phone)
        if not digits:
            return JsonResponse({"ok": False, "error": "no_phone"}, status=400)
        recipient = digits
        wa_url = build_whatsapp_url(digits, message)
    else:
        if not customer.email:
            return JsonResponse({"ok": False, "error": "no_email"}, status=400)
        recipient = customer.email
        subject = build_email_subject(business=biz, lang=lang)
        try:
            send_email_reminder(recipient=recipient, subject=subject,
                                message=message)
        except Exception as exc:  # noqa: BLE001 - log the failure, never crash
            logger.warning("due reminder email failed: %s", exc)
            DueReminder.objects.create(
                business=biz, partner=assignment.partner, customer=customer,
                assignment=assignment, channel=channel, recipient=recipient,
                amount=amount, message=message, language=lang,
                status=DueReminder.Status.FAILED, error=str(exc)[:500],
                sent_by=request.user,
            )
            return JsonResponse({
                "ok": False, "code": "email_failed", "error": "email_failed",
                "detail": str(exc)[:300],
            }, status=502)

    log = DueReminder.objects.create(
        business=biz, partner=assignment.partner, customer=customer,
        assignment=assignment, channel=channel, recipient=recipient,
        amount=amount, message=message, language=lang,
        status=DueReminder.Status.SENT, sent_by=request.user,
    )
    try:
        from apps.web import notifications as notif
        notif.notify_admins(
            biz, kind=notif.Notification.Kind.REMINDER_SENT,
            title=(f"রিমাইন্ডার পাঠানো হলো — {customer.name}"),
            body=(f"{channel} · ৳{amount:,.2f} · {customer.name} কে রিমাইন্ডার পাঠানো হয়েছে।"),
            link=f"/customers/?partner={assignment.partner.id}&history={customer.id}",
            amount=amount,
        )
    except Exception:
        logger.exception("reminder notification failed")
    record_audit("due_reminder", log, business=biz, changes={
        "channel": channel, "recipient": recipient,
        "amount": str(amount), "customer": customer.name,
    })
    return JsonResponse({
        "ok": True, "channel": channel, "status": log.status,
        "message": message, "wa_url": wa_url, "id": str(log.id),
        "amount": format_money(amount),
    })


@login_required
@require_http_methods(["GET"])
def export_khata_view(request, fmt):
    """Download one day's partner baki khata as PDF, Excel or Word."""
    from apps.web.exports import (
        DOCX_CONTENT_TYPE, PDF_CONTENT_TYPE, XLSX_CONTENT_TYPE,
        build_khata_export, export_khata_docx, export_khata_pdf,
        export_khata_xlsx,
    )

    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(
            request,
            "Select a business to export the baki khata." if en
            else "বাকির খাতা এক্সপোর্ট করতে একটি ব্যবসা নির্বাচন করুন।",
        )
        return redirect("web:dashboard")

    own = _own_partner(biz, membership, request.user)
    partner_id = (request.GET.get("partner") or "").strip()
    selected = None
    if own:
        if partner_id and str(partner_id) != str(own.id):
            raise PermissionDenied(
                "You can only export your own partner book."
            )
        selected = own
    elif partner_id:
        try:
            selected = (biz.partners
                        .filter(pk=partner_id, status="ACTIVE")
                        .first())
        except (ValueError, ValidationError):
            selected = None

    if selected is None:
        messages.error(
            request,
            "Select a partner first." if en else "আগে একটি পার্টনার নির্বাচন করুন।",
        )
        return redirect("web:customers")
    if membership.role == BusinessMembership.Role.PARTNER and (not own or selected.id != own.id):
        raise PermissionDenied("You can only export your own partner book.")

    pdate_raw = (request.GET.get("pdate") or "").strip()
    try:
        pdate = date.fromisoformat(pdate_raw)
    except ValueError:
        messages.error(
            request,
            "Select a valid baki date first." if en
            else "আগে একটি সঠিক বাকির তারিখ নির্বাচন করুন।",
        )
        return redirect(f"{reverse('web:customers')}?partner={selected.id}")

    back = (f"{reverse('web:customers')}?partner={selected.id}"
            f"&pdate={pdate.isoformat()}")
    fmt_key = (fmt or "").lower()
    if fmt_key in ("excel", "xlsx"):
        fmt_key, extension = "xlsx", "xlsx"
        content_type = XLSX_CONTENT_TYPE
    elif fmt_key in ("word", "docx"):
        fmt_key, extension = "docx", "docx"
        content_type = DOCX_CONTENT_TYPE
    elif fmt_key == "pdf":
        extension = "pdf"
        content_type = PDF_CONTENT_TYPE
    else:
        raise Http404("Unsupported export format")

    try:
        data = build_khata_export(
            biz, selected, pdate, request.session.get("ui_lang", "bn"),
        )
        if fmt_key == "pdf":
            content = export_khata_pdf(data)
        elif fmt_key == "xlsx":
            content = export_khata_xlsx(data)
        else:
            content = export_khata_docx(data)
    except Exception:
        logger.exception("Baki khata export failed (format=%s, partner=%s, date=%s)",
                         fmt_key, selected.id, pdate)
        messages.error(
            request,
            "Could not create the export. Please try again." if en
            else "এক্সপোর্ট তৈরি করা যায়নি। আবার চেষ্টা করুন।",
        )
        return redirect(back)

    filename = f"{data['filename']}.{extension}"
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def customer_statement_view(request, customer_id):
    """Download one customer's বাকি statement ("বাকি চালান" / Customer
    Statement) as a PDF — লোগো + ব্যবসার নামে সজ্জিত, গ্রাহকের পুরো খাতা।"""
    from apps.web.receipt_exports import (
        build_customer_statement, customer_statement_pdf,
    )
    from apps.web.exports import PDF_CONTENT_TYPE
    from apps.customers.models import Customer

    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business first." if en else "আগে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")

    own = _own_partner(biz, membership, request.user)
    partner_id = (request.GET.get("partner") or "").strip()
    selected = None
    if own:
        if partner_id and str(partner_id) != str(own.id):
            raise PermissionDenied(
                "You can only view your own partner book."
            )
        selected = own
    elif partner_id:
        try:
            selected = (biz.partners
                        .filter(pk=partner_id, status="ACTIVE")
                        .first())
        except (ValueError, ValidationError):
            selected = None

    if selected is None:
        messages.error(request, "Select a partner first." if en else "আগে একটি পার্টনার নির্বাচন করুন।")
        return redirect("web:customers")
    if membership.role == BusinessMembership.Role.PARTNER and (not own or selected.id != own.id):
        raise PermissionDenied("You can only view your own partner book.")

    customer = (Customer.objects
                .filter(pk=customer_id, business=biz,
                        assignments__partner=selected)
                .distinct().first())
    if customer is None:
        raise Http404("No matching customer.")

    try:
        data = build_customer_statement(
            biz, selected, customer, request.session.get("ui_lang", "bn"))
        content = customer_statement_pdf(data)
    except Exception:
        logger.exception("Customer statement PDF failed (customer=%s)", customer_id)
        messages.error(
            request,
            "Could not create the statement. Please try again." if en
            else "স্ট্যাটমেন্ট তৈরি করা যায়নি। আবার চেষ্টা করুন।",
        )
        return redirect(f"{reverse('web:customers')}?partner={selected.id}&history={customer.id}")

    filename = f"{data['filename']}.pdf"
    response = HttpResponse(content, content_type=PDF_CONTENT_TYPE)
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def money_receipt_view(request, tx_id):
    """Download a PAYMENT payment receipt ("পরিশোধ রশিকা" / Money Receipt)
    as a PDF — অঙ্কে ও কথায় টাকা লেখা থাকে।"""
    from apps.web.receipt_exports import (
        build_money_receipt, money_receipt_pdf,
    )
    from apps.web.exports import PDF_CONTENT_TYPE

    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business first." if en else "আগে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")

    own = _own_partner(biz, membership, request.user)

    tx = (Transaction.objects
          .filter(pk=tx_id, business=biz,
                  transaction_type=TransactionType.PAYMENT)
          .exclude(status=TransactionStatus.REVERSED)
          .select_related("customer", "partner")
          .first())
    if tx is None:
        raise Http404("No matching payment.")
    if own and tx.partner_id != own.id:
        raise PermissionDenied("You can only view your own partner book.")

    try:
        data = build_money_receipt(
            biz, tx.partner, tx, request.session.get("ui_lang", "bn"))
        content = money_receipt_pdf(data)
    except Exception:
        logger.exception("Money receipt PDF failed (tx=%s)", tx_id)
        messages.error(
            request,
            "Could not create the receipt. Please try again." if en
            else "রশিকা তৈরি করা যায়নি। আবার চেষ্টা করুন।",
        )
        return redirect(f"{reverse('web:customers')}?partner={tx.partner_id}&history={tx.customer_id}")

    filename = f"{data['filename']}.pdf"
    response = HttpResponse(content, content_type=PDF_CONTENT_TYPE)
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def export_supercalc_view(request, fmt):
    """Download the Super Calculator report as PDF, Excel or Word."""
    from apps.web.exports import (
        DOCX_CONTENT_TYPE, PDF_CONTENT_TYPE, XLSX_CONTENT_TYPE,
        build_supercalc_export, export_supercalc_docx, export_supercalc_pdf,
        export_supercalc_xlsx,
    )

    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(
            request,
            "Select a business to export the Super Calculator report." if en
            else "সুপার ক্যালকুলেটর এক্সপোর্ট করতে একটি ব্যবসা নির্বাচন করুন।",
        )
        return redirect("web:dashboard")

    today = timezone.localdate()

    def _parse(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    # Same defaults as the page: month-to-date, swapped if reversed.
    dfrom = _parse(request.GET.get("from"))
    dto = _parse(request.GET.get("to"))
    if dfrom is None:
        dfrom = today.replace(day=1)
    if dto is None:
        dto = today
    if dfrom > dto:
        dfrom, dto = dto, dfrom

    back = (f"{reverse('web:supercalc')}?preset=custom"
            f"&from={dfrom.isoformat()}&to={dto.isoformat()}")

    fmt_key = (fmt or "").lower()
    if fmt_key in ("excel", "xlsx"):
        fmt_key, extension = "xlsx", "xlsx"
        content_type = XLSX_CONTENT_TYPE
    elif fmt_key in ("word", "docx"):
        fmt_key, extension = "docx", "docx"
        content_type = DOCX_CONTENT_TYPE
    elif fmt_key == "pdf":
        extension = "pdf"
        content_type = PDF_CONTENT_TYPE
    else:
        raise Http404("Unsupported export format")

    try:
        data = build_supercalc_export(
            biz, dfrom, dto, request.session.get("ui_lang", "bn"),
        )
        if fmt_key == "pdf":
            content = export_supercalc_pdf(data)
        elif fmt_key == "xlsx":
            content = export_supercalc_xlsx(data)
        else:
            content = export_supercalc_docx(data)
    except Exception:
        logger.exception(
            "Super Calculator export failed (format=%s, business=%s, "
            "from=%s, to=%s)", fmt_key, biz.id, dfrom, dto)
        messages.error(
            request,
            "Could not create the export. Please try again." if en
            else "এক্সপোর্ট তৈরি করা যায়নি। আবার চেষ্টা করুন।",
        )
        return redirect(back)

    filename = f"{data['filename']}.{extension}"
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def personal_view(request):
    today = timezone.localdate()
    ensure_default_accounts_and_categories(request.user)
    if request.method == "POST":
        try:
            create_personal_transaction(
                request.user,
                transaction_type=request.POST.get("transaction_type"),
                amount=request.POST.get("amount"),
                transaction_date=request.POST.get("transaction_date")
                or date.today(),
                account_id=request.POST.get("account"),
                category_id=request.POST.get("category") or None,
                description=request.POST.get("description") or "",
                client_reference=request.POST.get("client_reference") or "",
            )
            messages.success(request, "Personal transaction recorded.")
            return redirect("web:personal")
        except PersonalServiceError as exc:
            err = exc.errors
            if isinstance(err, dict):
                err = "; ".join(f"{k}: {v}" for k, v in err.items())
            messages.error(request, err or str(exc))
        except Exception as exc:
            messages.error(request, f"Could not save: {exc}")

    accounts = list(request.user.personal_accounts.filter(
        status=PersonalAccount.Status.ACTIVE))
    acct_rows = [{"obj": a, "balance": a.balance()} for a in accounts]
    return render(request, "web/personal.html", {
        "net_worth": net_worth(request.user),
        "month": month_expense_summary(request.user, today.year, today.month),
        "accounts": acct_rows,
        "categories": PersonalCategory.objects.filter(user=request.user),
        "recent": request.user.personal_transactions.select_related(
            "account", "category").order_by(
            "-transaction_date", "-created_at")[:30],
        "today": today.isoformat(),
    })


@login_required
@require_http_methods(["GET", "POST"])
def loans_view(request, kind=LoanBookKind.LOAN):
    """ঋণ / বিনিয়োগ — two strictly separate books.

    * The **loan** book holds lenders: money borrowed (adds to the main
      account) and money repaid (subtracts from it).
    * The **investment** book holds investors: capital invested (adds) and
      capital withdrawn (subtracts).

    The two books never mix: each has its own parties, its own balances, its
    own totals and its own history. Every entry is posted through the one
    validated transaction writer, so the cash balance, the journal and the
    book always agree.
    """
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business to view this book." if request.session.get("ui_lang") == "en" else "এই হিসাব দেখতে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")

    en = request.session.get("ui_lang") == "en"
    is_admin = is_owner_role(membership)  # kept for partner management
    is_loan = kind == LoanBookKind.LOAN
    from apps.loans.models import LoanEntry, LoanParty
    from apps.loans.service import (
        create_entry, create_party, hard_delete_entry, reverse_entry,
        summarize, party_rows,
    )
    from apps.loans import schedule as loan_schedule

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        actor = SimpleNamespace(user=request.user, business=biz,
                                business_membership=membership)
        try:
            if action == "add_party":
                if not can_manage_partners(membership):
                    raise PermissionError("Not allowed")
                party = create_party(
                    actor, business=biz,
                    name=request.POST.get("name"),
                    kind=kind,
                    phone=request.POST.get("phone") or "",
                    email=request.POST.get("email") or "",
                    notes=request.POST.get("notes") or "",
                )
                messages.success(request, "Added." if en else "যোগ হয়েছে।")
                return redirect(request.path)

            if action == "add_entry":
                if not can_book(membership):
                    raise PermissionError("Not allowed")
                party = LoanParty.objects.filter(
                    business=biz, kind=kind,
                    pk=(request.POST.get("party_id") or "").strip()).first()
                if party is None:
                    raise TransactionServiceError(
                        {"party_id": "Choose a name from this book."})
                create_entry(
                    actor, business=biz, party=party,
                    kind=request.POST.get("kind") or LoanEntry.Kind.RECEIVED,
                    amount=request.POST.get("amount"),
                    transaction_date=request.POST.get("transaction_date"),
                    description=request.POST.get("description") or "",
                    notes=request.POST.get("notes") or "",
                    contract_total=(request.POST.get("contract_total")
                                    if is_loan else None),
                )
                # keep the date so the next entry opens on it, not today
                used_date = (request.POST.get("transaction_date") or "").strip()
                try:
                    date.fromisoformat(used_date)
                    request.session["loan_entry_date"] = used_date
                except ValueError:
                    pass
                messages.success(request, "Entry saved." if en else "এন্ট্রি সংরক্ষণ হয়েছে।")
                return redirect(request.path)

            if action == "add_schedule":
                if not can_book(membership):
                    raise PermissionError("Not allowed")
                if not is_loan:
                    raise loan_schedule.ScheduleError(
                        "Installment schedules are only for the loan book.")
                party = LoanParty.objects.filter(
                    business=biz, kind=kind,
                    pk=(request.POST.get("party_id") or "").strip()).first()
                if party is None:
                    raise loan_schedule.ScheduleError(
                        "Choose a lender from this book.")
                if party.kind != LoanParty.Kind.LOAN:
                    raise loan_schedule.ScheduleError(
                        "Installment schedules are only for loans.")
                first_due = (request.POST.get("first_due") or "").strip()
                if not first_due:
                    raise loan_schedule.ScheduleError(
                        "Pick the first installment due date.")
                try:
                    first_due = date.fromisoformat(first_due)
                except ValueError:
                    raise loan_schedule.ScheduleError(
                        "Pick a valid first due date.")
                loan_schedule.create_schedule(
                    actor, business=biz, party=party,
                    total=request.POST.get("total"),
                    count=request.POST.get("count"),
                    first_due=first_due,
                    notes=request.POST.get("notes") or "")
                messages.success(request,
                                 "Schedule created." if en
                                 else "কিস্তি প্ল্যান তৈরি হয়েছে।")
                return redirect(request.path)

            if action == "pay_installment":
                if not can_book(membership):
                    raise PermissionError("Not allowed")
                if not is_loan:
                    raise loan_schedule.ScheduleError(
                        "Only the loan book has installments.")
                loan_schedule.pay_installment(
                    actor, business=biz,
                    installment_id=request.POST.get("installment_id") or "")
                messages.success(request,
                                 "Installment checked off." if en
                                 else "কিস্তি পরিশোধ চিহ্নিত করা হলো।")
                return redirect(request.path)

            if action == "unpay_installment":
                if not can_delete(membership):
                    raise PermissionError("Not allowed")
                if not is_loan:
                    raise loan_schedule.ScheduleError(
                        "Only the loan book has installments.")
                loan_schedule.unpay_installment(
                    actor, business=biz,
                    installment_id=request.POST.get("installment_id") or "")
                messages.success(request,
                                 "Check-off removed; the repayment was reversed." if en
                                 else "পরিশোধ চিহ্নিত বাতিল, ফেরত এন্ট্রিও বাতিল হলো।")
                return redirect(request.path)

            if action == "delete_schedule":
                if not can_delete(membership):
                    raise PermissionError("Not allowed")
                if not is_loan:
                    raise loan_schedule.ScheduleError(
                        "Only the loan book has installments.")
                loan_schedule.delete_schedule(
                    actor, business=biz,
                    party_id=request.POST.get("party_id") or "")
                messages.success(request,
                                 "Schedule deleted (nothing was paid)." if en
                                 else "প্ল্যান মুছে দেওয়া হয়েছে (কিছুই পরিশোধ করা হয়নি)।")
                return redirect(request.path)

            if action in ("edit_entry", "delete_entry"):
                if action == "edit_entry":
                    if not can_book(membership):
                        raise PermissionError("Not allowed")
                else:
                    if not can_delete(membership):
                        raise PermissionError("Not allowed")
                statuses = ([LoanEntry.Status.POSTED, LoanEntry.Status.REVERSED]
                            if kind == LoanBookKind.INVESTMENT
                            else [LoanEntry.Status.POSTED])
                entry = LoanEntry.objects.filter(
                    business=biz, party__kind=kind,
                    pk=(request.POST.get("entry_id") or "").strip(),
                    status__in=statuses).first()
                if entry is None:
                    messages.error(request, "Entry not found." if en else "এন্ট্রি পাওয়া যায়নি।")
                    return redirect(request.path)
                reason = (request.POST.get("reason") or "").strip()
                if action == "delete_entry":
                    # Both books now erase the entry completely — row,
                    # cash transaction and ledger postings go together.
                    # Nothing stays behind marked "Reversed": what the shop
                    # deletes is gone from the history, like everywhere else.
                    hard_delete_entry(actor, entry, reason=reason or
                                      "Deleted from history")
                    messages.success(request, "Entry deleted." if en else "এন্ট্রি মুছে ফেলা হয়েছে।")
                    return redirect(request.path)

                party = entry.party
                reverse_entry(actor, entry, reason=f"Edit: {reason}")
                create_entry(
                    actor, business=biz, party=party,
                    kind=request.POST.get("kind") or entry.kind,
                    amount=request.POST.get("amount"),
                    transaction_date=request.POST.get("transaction_date"),
                    description=request.POST.get("description") or "",
                    notes=request.POST.get("notes") or "",
                    contract_total=(request.POST.get("contract_total")
                                    if is_loan else None),
                )
                messages.success(request, "Entry updated." if en else "এন্ট্রি আপডেট করা হয়েছে।")
                return redirect(request.path)
        except TransactionServiceError as exc:
            err = exc.errors
            if isinstance(err, dict):
                err = "; ".join(f"{k}: {v}" for k, v in err.items())
            messages.error(request, err or str(exc))
        except loan_schedule.ScheduleError as exc:
            messages.error(request, str(exc))
        except PermissionError:
            messages.error(request, "Not allowed." if en else "অনুমতি নেই।")
        except Exception as exc:
            messages.error(request, (f"Could not save: {exc}" if en else f"সংরক্ষণ করা যায়নি: {exc}"))
        return redirect(request.path)

    # ---- date filter ----------------------------------------------------
    def _parse_date(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    summary = summarize(biz, kind=kind, from_date=date_from, to_date=date_to)
    rows = party_rows(biz, kind=kind, from_date=date_from, to_date=date_to)

    entries_qs = (LoanEntry.objects.filter(business=biz, party__kind=kind)
                  .exclude(status=LoanEntry.Status.REVERSED)   # never show deleted rows
                  .select_related("party"))
    if date_from:
        entries_qs = entries_qs.filter(transaction_date__gte=date_from)
    if date_to:
        entries_qs = entries_qs.filter(transaction_date__lte=date_to)
    entries = list(entries_qs.order_by("-transaction_date", "-created_at")[:300])
    # per-row permissions: the investment book erases a row for good
    # (even an already reversed one), the loan book only reverses
    for entry in entries:
        entry.can_edit = bool(
            can_book(membership) and entry.status == LoanEntry.Status.POSTED)
        entry.can_delete = bool(
            can_delete(membership) and (kind == LoanBookKind.INVESTMENT
                                        or entry.status == LoanEntry.Status.POSTED))

    parties = list(LoanParty.objects.filter(
        business=biz, kind=kind).order_by("name"))

    today = date.today()
    # remember the date the shop last used, so consecutive backdated
    # entries don't reset to today
    last_used = _parse_date(request.session.get("loan_entry_date"))
    form_date = (last_used or today).isoformat()
    # ---- installment schedules (loan book only) --------------------------
    schedule_groups = None
    schedule_parties = []
    if is_loan:
        schedule_groups = loan_schedule.schedule_groups(biz)
        scheduled_ids = {g["party"].id for g in schedule_groups}
        schedule_parties = [p for p in parties if p.id not in scheduled_ids]

    return render(request, "web/loans.html", {
        "summary": summary,
        "book_kind": kind,
        "is_loan": is_loan,
        "party_rows": rows,
        "entries": entries,
        "parties": parties,
        "today": today.isoformat(),
        "form_date": form_date,
        "default_month_start": today.replace(day=1).isoformat(),
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "filter_active": bool(date_from or date_to),
        "can_manage": can_book(membership),
        "schedule_groups": schedule_groups,
        "schedule_parties": schedule_parties,
    })


@login_required
@require_http_methods(["GET", "POST"])
def investments_view(request):
    """বিনিয়োগ — the investor book (kept apart from the loan book)."""
    return loans_view(request, kind=LoanBookKind.INVESTMENT)


@login_required
def summary_view(request):
    """হিসাব সারাংশ — the real money position of the business.

    * **আমার টাকা** — cash in the drawer, mobile/bank money, what the
      customers owe, and the total (all read from the double-entry ledger).
    * **ড্রয়ার গোনা** — the owner's own count of the drawer compared with
      the book: the difference is the honest গরমিল / ঘাটতি.
    * **লাভ/লস** — sales minus the cost of what was actually sold (stock
      still on the shelf is an asset, not a loss).
    * **নগদের আনাগোনা** — day-wise movement plus the real closing cash.
    """
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business to see the summary." if request.session.get("ui_lang") == "en" else "হিসাব সারাংশ দেখতে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")

    en = request.session.get("ui_lang") == "en"
    from apps.cashbook import services as cashbook
    from apps.web.summary import (
        compare_periods, day_rows, month_rows, period_totals,
    )

    today = timezone.localdate()

    def _parse(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    # ---- actions: count the drawer / write the stock / repair the ledger --
    if request.method == "POST":
        if not can_write(membership):
            messages.error(request, "Permission denied." if en else "অনুমতি নেই — শুধু মালিক/হিসাবরক্ষক লেনদেন করতে পারবেন।")
            return redirect("web:summary")
        action = (request.POST.get("action") or "").strip()
        try:
            if action == "count_cash":
                counted = (request.POST.get("counted_amount") or "").strip()
                counted_on = (request.POST.get("counted_on") or "").strip()
                note = (request.POST.get("note") or "").strip()[:160]
                if not counted:
                    raise ValueError("counted_amount is required")
                amount = Decimal(str(counted))
                if amount < 0:
                    raise ValueError("counted_amount must not be negative")
                day = _parse(counted_on) or today
                # The drawer holds every rupee the book knows — the compare
                # must use the same figure the page shows, so a future-dated
                # entry can never make the difference meaningless.
                book = cashbook.cash_balance(biz)
                from apps.cashbook.models import CashCount
                CashCount.objects.create(
                    business=biz, counted_on=day, kind=CashCount.Kind.COUNT,
                    counted_amount=amount, book_amount=book,
                    difference=(amount - book).quantize(Decimal("0.01")),
                    note=note, counted_by=request.user,
                )
                diff = (amount - book).quantize(Decimal("0.01"))
                messages.success(request, (
                    f"Drawer counted: ৳{amount:,.2f} "
                    f"({'গরমিল' if diff >= 0 else 'ঘাটতি'} ৳{abs(diff):,.2f})"
                    if en else
                    f"ড্রয়ার গোনা হয়েছে: ৳{amount:,.2f} "
                    f"({'গরমিল' if diff >= 0 else 'ঘাটতি'} ৳{abs(diff):,.2f})"))
                return redirect("web:summary")

            if action == "set_stock":
                value = (request.POST.get("closing_value") or "").strip()
                snap_on = (request.POST.get("snapshot_on") or "").strip()
                note = (request.POST.get("note") or "").strip()[:160]
                if not value:
                    raise ValueError("closing_value is required")
                amount = Decimal(str(value))
                if amount < 0:
                    raise ValueError("closing_value must not be negative")
                day = _parse(snap_on) or today
                from apps.cashbook.models import StockSnapshot
                StockSnapshot.objects.create(
                    business=biz, snapshot_on=day, closing_value=amount,
                    note=note, created_by=request.user,
                )
                messages.success(request, (
                    f"Stock recorded: ৳{amount:,.2f}" if en else
                    f"মজুদ লেখা হয়েছে: ৳{amount:,.2f}"))
                return redirect("web:summary")

            if action == "repair_ledger":
                fixed = cashbook.repair_ledger(biz, user=request.user)
                messages.success(request, (
                    f"{fixed} posting(s) balanced." if en else
                    f"{fixed}টি জার্নাল মিলিয়ে ঠিক করা হয়েছে।"))
                return redirect("web:summary")

            if action == "rebuild_ledger":
                fixed = cashbook.rebuild_ledger_from_transactions(
                    biz, user=request.user)
                messages.success(request, (
                    f"{fixed} posting(s) rebuilt from transactions." if en else
                    f"{fixed}টি লেনদেনের জার্নাল পোস্ট হয়েছে — "
                    "হিসাব এখন আপডেট।"))
                return redirect("web:summary")
        except (ValueError, ArithmeticError) as exc:
            messages.error(request, (f"Could not save: {exc}" if en
                                     else f"সংরক্ষণ করা যায়নি: {exc}"))
            return redirect("web:summary")

    year = today.year

    preset = (request.GET.get("preset") or "month").strip()
    dfrom = _parse(request.GET.get("from"))
    dto = _parse(request.GET.get("to"))
    if preset == "today":
        dfrom = dto = today
    elif preset == "week":
        dfrom = today.fromordinal(today.toordinal() - today.weekday())
        dto = today
    elif preset == "last_month":
        first_this = today.replace(day=1)
        last_end = first_this.fromordinal(first_this.toordinal() - 1)
        dfrom = last_end.replace(day=1)
        dto = last_end
    elif preset == "year":
        dfrom = date(year, 1, 1)
        dto = date(year, 12, 31)
    else:
        preset = "month" if not (dfrom and dto) else "custom"
        if dfrom is None:
            dfrom = today.replace(day=1)
        if dto is None:
            dto = today
    if dfrom > dto:
        dfrom, dto = dto, dfrom

    totals = period_totals(biz, dfrom, dto)
    rows = day_rows(biz, dfrom, dto)
    months = month_rows(biz, year)
    comparison = compare_periods(biz, dfrom, dto)

    # Running cash at the end of the period (used in the day-table footer).
    closing_cash = rows[-1]["running_cash"] if rows else Decimal("0.00")
    # The REAL drawer balance at the end of the period (ledger-based).
    period_end_cash = rows[-1]["closing_cash"] if rows else Decimal("0.00")

    # ---- the real money position (one truth, from the ledger) -------------
    drawer = cashbook.drawer_cash(biz)
    position = cashbook.business_position(biz)
    profit = cashbook.profit_breakdown(biz, dfrom, dto)
    integrity = cashbook.integrity_check(biz)
    future_cash = cashbook.future_dated_cash(biz, today)
    # transactions whose double-entry journal was never posted — the books
    # show the money but the ledger (হাতে নগদ) cannot see them
    unposted = cashbook.unposted_transactions(biz)

    # loan & investment books: taken, returned and still outstanding
    from apps.loans.models import LoanParty
    from apps.loans.service import summarize
    loan_book = summarize(biz, kind=LoanParty.Kind.LOAN)
    invest_book = summarize(biz, kind=LoanParty.Kind.INVESTMENT)

    return render(request, "web/summary.html", {
        "dfrom": dfrom,
        "dto": dto,
        "today": today.isoformat(),
        "year": year,
        "years": list(range(year - 3, year + 1)),
        "table_year": year,
        "preset": preset,
        "totals": totals,
        "rows": rows,
        "months": months,
        "comparison": comparison,
        "closing_cash": closing_cash,
        "period_end_cash": rows[-1]["closing_cash"] if rows else Decimal("0.00"),
        "position": position,
        "drawer": drawer,
        "profit": profit,
        "integrity": integrity,
        "future_cash": future_cash,
        "unposted": unposted,
        "loan_book": loan_book,
        "invest_book": invest_book,
        "personal_money": net_worth(request.user).get("net_worth", Decimal("0.00")),
    })


@login_required
def supercalc_view(request):
    """সুপার ক্যালকুলেটর — মোট সেল − মোট খরচ = লাভ / লস.

    মোট সেল = নগদে সেল (INCOME) + বাকিতে সেল (CREDIT). A due collection
    (PAYMENT in the বাকির বই) is never counted as sell — it is an *older*
    sale's money returning, so the toplines can never double-count.
    """
    biz, membership = _require_business(request)
    if not biz:
        messages.info(
            request,
            "Open the Super Calculator after picking a business."
            if request.session.get("ui_lang") == "en"
            else "সুপার ক্যালকুলেটর দেখতে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")

    from apps.web.supercalc import (
        calc_day_rows, calc_month_rows, calc_totals, compare_calc,
    )

    today = timezone.localdate()
    year = today.year
    # the year the monthly profit table shows (a shop looks back a lot)
    try:
        table_year = int(request.GET.get("year") or year)
    except (TypeError, ValueError):
        table_year = year
    if table_year < 2000 or table_year > year + 1:
        table_year = year
    years = list(range(year - 3, year + 1))

    def _parse(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    preset = (request.GET.get("preset") or "month").strip()
    dfrom = _parse(request.GET.get("from"))
    dto = _parse(request.GET.get("to"))
    if preset == "today":
        dfrom = dto = today
    elif preset == "week":
        dfrom = today.fromordinal(today.toordinal() - today.weekday())
        dto = today
    elif preset == "last_month":
        first_this = today.replace(day=1)
        last_end = first_this.fromordinal(first_this.toordinal() - 1)
        dfrom = last_end.replace(day=1)
        dto = last_end
    elif preset == "year":
        dfrom = date(year, 1, 1)
        dto = date(year, 12, 31)
    else:
        preset = "month" if not (dfrom and dto) else "custom"
        if dfrom is None:
            dfrom = today.replace(day=1)
        if dto is None:
            dto = today
    if dfrom > dto:
        dfrom, dto = dto, dfrom

    totals = calc_totals(biz, dfrom, dto)
    rows = calc_day_rows(biz, dfrom, dto)
    comparison = compare_calc(biz, dfrom, dto)
    months = calc_month_rows(biz, table_year)
    year_totals = {
        "sell_total": sum((m["sell_total"] for m in months), Decimal("0.00")).quantize(Decimal("0.01")),
        "khoroch": sum((m["khoroch"] for m in months), Decimal("0.00")).quantize(Decimal("0.01")),
        "collection": sum((m["collection"] for m in months), Decimal("0.00")).quantize(Decimal("0.01")),
        "profit": sum((m["profit"] for m in months), Decimal("0.00")).quantize(Decimal("0.01")),
    }

    return render(request, "web/supercalc.html", {
        "dfrom": dfrom,
        "dto": dto,
        "today": today.isoformat(),
        "year": year,
        "table_year": table_year,
        "years": years,
        "preset": preset,
        "totals": totals,
        "rows": rows,
        "months": months,
        "year_totals": year_totals,
        "comparison": comparison,
    })


@login_required
@require_http_methods(["GET", "POST"])
def expenses_view(request):
    """খরচ / ক্রয় — what was bought and every other business expense.

    A deliberately small ledger: date, what was bought (name), amount and a
    short note. No stock, no supplier ledger, no invoice workflow — only the
    money that left the business. Entries are EXPENSE cash transactions, so
    they never touch the customer due book (বাকি) or the cash-in book (নগদ).
    """
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business to view expenses." if request.session.get("ui_lang") == "en" else "খরচ দেখতে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")

    en = request.session.get("ui_lang") == "en"
    from apps.transactions.models import PaymentMethod, TransactionType

    if request.method == "POST":
        if not can_write(membership):
            messages.error(request, "Permission denied." if en else "অনুমতি নেই — শুধু মালিক/হিসাবরক্ষক লেনদেন করতে পারবেন।")
            return redirect("web:expenses")
        action = (request.POST.get("action") or "").strip()
        actor = SimpleNamespace(user=request.user, business=biz,
                                business_membership=membership)
        try:
            if action == "add_expense":
                name = (request.POST.get("name") or "").strip()
                amount = (request.POST.get("amount") or "").strip()
                tx_date = (request.POST.get("transaction_date") or "").strip()
                note = (request.POST.get("description") or "").strip()
                if not name:
                    raise TransactionServiceError(
                        {"name": "What was bought is required."})
                if not tx_date:
                    raise TransactionServiceError(
                        {"transaction_date": "A date is required."})
                create_transaction(
                    actor, business=biz,
                    amount=amount,
                    transaction_type=TransactionType.EXPENSE,
                    transaction_date=tx_date,
                    payment_method=PaymentMethod.CASH,
                    description=name,
                    notes=note,
                    client_reference="",
                    expense_kind=(request.POST.get("expense_kind")
                                  or ExpenseKind.GOODS),
                )[0]
                # ---- if this category is now over its monthly budget ----
                try:
                    from apps.web.models import Notification
                    from apps.web import notifications as notif
                    kind_value = (request.POST.get("expense_kind")
                                  or ExpenseKind.GOODS)
                    month = date.today().replace(day=1)
                    budget = MonthlyBudget.objects.filter(
                        business=biz, category=kind_value, month=month).first()
                    if budget and budget.is_over:
                        already = Notification.objects.filter(
                            business=biz, kind=Notification.Kind.BUDGET_OVER,
                            created_at__gte=month).exists()
                        if not already:
                            notif.notify_admins(
                                biz, kind=Notification.Kind.BUDGET_OVER,
                                title=(f"বাজেট শেষ — {budget.get_category_display()}"),
                                body=(f"এই মাসে ৳{budget.spent:,.2f} খরচ, "
                                      f"সিলিং ৳{budget.limit_amount:,.2f}"),
                                link="/expenses/",
                                amount=budget.spent - budget.limit_amount,
                            )
                except Exception:
                    logger.exception("budget-over notification failed")
                messages.success(request, "Expense saved." if en else "খরচ সংরক্ষণ হয়েছে।")
                return redirect("web:expenses")

            if action in ("edit_expense", "delete_expense"):
                if action == "edit_expense":
                    if not can_write(membership):
                        raise PermissionError("Not allowed")
                else:
                    if not can_delete(membership):
                        raise PermissionError("Not allowed")
                tx_id = (request.POST.get("tx_id") or "").strip()
                reason = (request.POST.get("reason") or "").strip()
                txn = (Transaction.objects
                       .filter(pk=tx_id, business=biz,
                               payment_method=PaymentMethod.CASH,
                               transaction_type=TransactionType.EXPENSE)
                       .exclude(status=TransactionStatus.REVERSED)
                       .first())
                if not txn:
                    messages.error(request, "Entry not found." if en else "এন্ট্রি পাওয়া যায়নি।")
                    return redirect("web:expenses")
                if action == "delete_expense":
                    _reverse_cash_transaction(request, txn, reason or
                                              "Deleted from expense history")
                    messages.success(request, "Entry reversed." if en else "এন্ট্রি বাতিল করা হয়েছে।")
                    return redirect("web:expenses")

                new_amount = (request.POST.get("amount") or "").strip()
                new_date = (request.POST.get("transaction_date") or "").strip()
                new_name = (request.POST.get("name") or "").strip()
                if not new_name:
                    raise TransactionServiceError(
                        {"name": "What was bought is required."})
                if not new_date:
                    raise TransactionServiceError(
                        {"transaction_date": "A date is required."})
                from apps.transactions.services import _money
                amount = _money(new_amount)
                if amount <= 0:
                    raise TransactionServiceError(
                        "amount must be greater than zero.")
                _reverse_cash_transaction(
                    request, txn, reason=f"Edit: {reason}")
                create_transaction(
                    actor, business=biz,
                    amount=amount,
                    transaction_type=TransactionType.EXPENSE,
                    transaction_date=new_date,
                    payment_method=PaymentMethod.CASH,
                    description=new_name,
                    notes=(request.POST.get("description") or "").strip(),
                    client_reference="",
                    expense_kind=(request.POST.get("expense_kind")
                                  or ExpenseKind.GOODS),
                )[0]
                messages.success(request, "Entry updated." if en else "এন্ট্রি আপডেট করা হয়েছে।")
                return redirect("web:expenses")
        except TransactionServiceError as exc:
            err = exc.errors
            if isinstance(err, dict):
                err = "; ".join(f"{k}: {v}" for k, v in err.items())
            messages.error(request, err or str(exc))
        except PermissionError:
            messages.error(request, "Not allowed." if en else "অনুমতি নেই।")
        except Exception as exc:
            messages.error(request, (f"Could not save: {exc}" if en else f"সংরক্ষণ করা যায়নি: {exc}"))
        return redirect("web:expenses")

    # ---- date-wise filter (optional) ----
    def _parse_date(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    qs = (Transaction.objects
          .filter(business=biz, payment_method=PaymentMethod.CASH,
                  transaction_type=TransactionType.EXPENSE)
          .exclude(status=TransactionStatus.REVERSED))
    if date_from:
        qs = qs.filter(transaction_date__gte=date_from)
    if date_to:
        qs = qs.filter(transaction_date__lte=date_to)
    # ---- category filter (optional) ----
    category = (request.GET.get("category") or "").strip()
    valid_kinds = {k for k, _ in ExpenseKind.choices}
    if category and category in valid_kinds:
        qs = qs.filter(expense_kind=category)

    rows = qs.order_by("-transaction_date", "-created_at")[:500]

    row_list = list(rows)
    total = sum((r.amount for r in row_list), Decimal("0.00"))
    today = date.today()
    month_total = sum(
        (r.amount for r in row_list
         if getattr(r, "transaction_date", None)
         and r.transaction_date.year == today.year
         and r.transaction_date.month == today.month),
        Decimal("0.00"))
    all_total = sum(
        (Transaction.objects
         .filter(business=biz, payment_method=PaymentMethod.CASH,
                 transaction_type=TransactionType.EXPENSE)
         .exclude(status=TransactionStatus.REVERSED)
         .values_list("amount", flat=True)),
        Decimal("0.00"))

    # ---- pie chart: expense by category (over the filtered set) ----
    pie_rows = (qs.values("expense_kind")
                .annotate(s=Sum("amount"))
                .order_by("-s"))
    kind_label = dict(ExpenseKind.choices)
    pie_data = []
    pie_total = Decimal("0.00")
    for row in pie_rows:
        amount = (row["s"] or Decimal("0.00")).quantize(Decimal("0.01"))
        pie_data.append({
            "kind": row["expense_kind"],
            "label": kind_label.get(row["expense_kind"], row["expense_kind"]),
            "amount": float(amount),  # json.dumps will output a number
        })
        pie_total += amount
    for p in pie_data:
        p["pct"] = round(float(p["amount"] / float(pie_total) * 100), 1) if pie_total else 0.0

    # Format for JavaScript: list of {label, amount, pct}
    pie_for_js = json.dumps(pie_data)
    pie_total_str = str(pie_total.quantize(Decimal("0.01")))

    # ---- budget vs spent for the current month ----
    month_start = today.replace(day=1)
    budgets = (MonthlyBudget.objects
               .filter(business=biz, month=month_start)
               .order_by("category"))
    over_budget = [b for b in budgets if b.is_over]

    return render(request, "web/expenses.html", {
        "rows": row_list,
        "expense_total": total,
        "expense_all_total": all_total,
        "expense_month_total": month_total,
        "expense_count": len(row_list),
        "today": today.isoformat(),
        "default_month_start": today.replace(day=1).isoformat(),
        "can_manage": membership.role == "ADMIN",
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "category": category,
        "filter_active": bool(date_from or date_to or category),
        "expense_kinds": ExpenseKind.choices,
        "pie_data": pie_data,
        "pie_total": pie_total,
        "budgets": budgets,
        "over_budget": over_budget,
    })


@login_required
@require_http_methods(["GET", "POST"])
def assistant_view(request):
    biz, membership = _require_business(request)
    answer = None
    # Ctrl+K palette opens /assistant/?q=…&auto=1 for proposal creation
    auto_q = ((request.GET.get("q") or "").strip()
              if request.GET.get("auto") == "1" else "")
    question = auto_q
    scope = "BUSINESS" if biz else "PERSONAL"

    from apps.assistant.actions import (
        parse_command, create_proposed_action, execute_action,
    )
    from apps.assistant.models import AssistantAction

    # ---- confirm / discard a proposed action -------------------------------
    if request.method == "POST":
        do = (request.POST.get("action") or "").strip()
        if do in ("confirm_action", "discard_action"):
            if not can_write(membership):
                messages.error(request,
                               "Permission denied."
                               if request.session.get("ui_lang") == "en"
                               else "অনুমতি নেই।")
                return redirect("web:assistant")
            action_id = (request.POST.get("action_id") or "").strip()
            if not biz:
                messages.error(request, "Select a business first.")
                return redirect("web:assistant")
            target = AssistantAction.objects.filter(
                pk=action_id, business=biz, user=request.user,
                status=AssistantAction.Status.PROPOSED).first()
            if target is None:
                messages.error(request, "Proposal not found or already handled.")
                return redirect("web:assistant")
            if do == "discard_action":
                target.status = AssistantAction.Status.DISCARDED
                target.save(update_fields=["status"])
                messages.success(request, "Discarded."
                                 if request.session.get("ui_lang") == "en"
                                 else "বাতিল করা হলো।")
                return redirect("web:assistant")
            # confirm — post through the one validated writer
            try:
                execute_action(
                    target, user=request.user, business=biz,
                    membership=membership)
                messages.success(
                    request,
                    f"Done — {target.get_kind_display()} ৳{target.amount:,.2f} posted."
                    if request.session.get("ui_lang") == "en"
                    else f"করা হলো — ৳{target.amount:,.2f} লিখে দেওয়া হয়েছে।")
            except Exception as exc:
                logger.exception("assistant confirm failed")
                messages.error(request,
                               f"Could not post: {exc}" if False else
                               f"পোস্ট করা যায়নি: {exc}")
            return redirect("web:assistant")

        question = (request.POST.get("question") or "").strip()
        if question:
            # ---- action-style command? propose, don't answer --------------
            if biz is None or not can_write(membership):
                parsed = None
            else:
                parsed = parse_command(question, biz, membership)
            if parsed:
                pkind = parsed.get("kind")
                if pkind == "proposed":
                    try:
                        action = create_proposed_action(
                            user=request.user, membership=membership,
                            business=biz, raw_question=question, parsed=parsed)
                        messages.success(
                            request,
                            "Proposal created — review and confirm." if False else
                            "প্রস্তাবিত এন্ট্রি তৈরি হয়েছে — নিচে দেখুন ও কনফার্ম করুন।")
                        return redirect("web:assistant")
                    except Exception as exc:
                        logger.exception("assistant propose failed")
                        messages.error(request, f"Proposal failed: {exc}")
                        answer = {"answer": f"Could not parse that request: {exc}",
                                  "intent": "ACTION_FAILED",
                                  "facts_used": [], "scope": "BUSINESS",
                                  "data_basis": ["proposal failed before posting"]}
                        return render(request, "web/assistant.html", {
                            "answer": answer, "question": question,
                            "history": _assistant_history(request.user, biz),
                            "scope": "BUSINESS",
                            "pending_action": None,
                        })
                elif pkind in ("customer_not_found", "ambiguous_customer",
                               "no_partner", "invalid_amount"):
                    hint = {
                        "customer_not_found": (
                            f"Customer named “{parsed.get('hint')}” not found. "
                            "Check the name exactly as in the customer list."
                            if request.session.get("ui_lang") == "en" else
                            f"“{parsed.get('hint')}” নামে কোনো কাস্টমার পাওয়া যায়নি — পুরো নাম দিন।"),
                        "ambiguous_customer": (
                            "Several customers match — be more specific: " +
                            ", ".join(c["name"] for c in parsed.get("choices", []))
                            if request.session.get("ui_lang") == "en" else
                            "কয়েকজন কাস্টমার মিলেছে — পুরো নাম দিন: " +
                            ", ".join(c["name"] for c in parsed.get("choices", []))),
                        "no_partner": (
                            "No active partner is set up in this business — create one first."
                            if request.session.get("ui_lang") == "en" else
                            "এই ব্যবসায় এখন পার্টনার নেই — আগে পার্টনার যোগ করুন।"),
                        "invalid_amount": (
                            "An amount is required — e.g. 'বাকি Karim 500'."
                            if request.session.get("ui_lang") == "en" else
                            "টাকার অঙ্ক লাগবে — যেমন: 'বাকি Karim 500'"),
                    }[pkind]
                    messages.error(request, hint)
                    answer = {"answer": hint, "intent": "ACTION_CLARIFY",
                              "facts_used": [], "scope": "BUSINESS",
                              "data_basis": ["no ledger rows posted"]}
                    return render(request, "web/assistant.html", {
                        "answer": answer, "question": question,
                        "history": _assistant_history(request.user, biz),
                        "scope": "BUSINESS",
                        "pending_action": AssistantAction.objects.filter(
                            business=biz, user=request.user,
                            status=AssistantAction.Status.PROPOSED).first(),
                    })

            # ---- normal question/answer ----------------------------------
            try:
                from apps.assistant.facts import build_business_facts, build_personal_facts
                from apps.assistant.engine import answer_question
                from apps.assistant.models import AssistantQuery
                if biz:
                    facts = build_business_facts(biz, membership)
                    use_scope = "BUSINESS"
                else:
                    facts = build_personal_facts(request.user)
                    use_scope = "PERSONAL"
                result = answer_question(request.user, question, facts=facts, scope=use_scope)
                AssistantQuery.objects.create(
                    user=request.user, business=biz, question=question,
                    answer=result.get("answer", ""), intent=result.get("intent", ""),
                    facts_used=result.get("facts_used", []), scope=use_scope)
                answer = result
                scope = use_scope
            except Exception as e:
                messages.error(request, f"Assistant error: {e}")
        else:
            messages.info(request, "Type a question to ask FinCore.")
    history = _assistant_history(request.user, biz)

    # ---- pending proposal (if any) ---------------------------------------
    pending_action = None
    if biz:
        try:
            pending_action = AssistantAction.objects.filter(
                business=biz, user=request.user,
                status=AssistantAction.Status.PROPOSED).first()
        except Exception:
            pending_action = None

    return render(request, "web/assistant.html", {
        "answer": answer, "question": question, "history": history,
        "scope": scope, "pending_action": pending_action,
        "auto_q": auto_q,
    })


def _assistant_history(user, biz, limit: int = 8):
    try:
        from apps.assistant.models import AssistantQuery
        qs = user.assistant_queries.all()
        if biz is not None:
            qs = qs.filter(business=biz)
        return list(qs[:limit])
    except Exception:
        return []

@require_POST
def set_language(request):
    lang = (request.POST.get("lang") or request.GET.get("lang") or "bn")
    if lang not in ("bn", "en"):
        lang = "bn"
    request.session["ui_lang"] = lang
    nxt = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
    if not isinstance(nxt, str) or not nxt.startswith("/"):
        nxt = "/"
    return redirect(nxt)


@require_POST
def set_view_mode(request):
    mode = (request.POST.get("view") or request.GET.get("view") or "auto").strip().lower()
    if mode not in ("auto", "desktop", "mobile"):
        mode = "auto"
    request.session["ui_view"] = mode
    nxt = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
    if not isinstance(nxt, str) or not nxt.startswith("/"):
        nxt = "/"
    return redirect(nxt)

@require_http_methods(["GET", "POST"])
def register_view(request):
    if request.user.is_authenticated:
        return redirect("web:dashboard")
    form = {}
    if request.method == "POST":
        form = {
            "username": (request.POST.get("username") or "").strip(),
            "email": (request.POST.get("email") or "").strip(),
            "phone": (request.POST.get("phone") or "").strip(),
            "password": request.POST.get("password") or "",
            "password2": request.POST.get("password2") or "",
        }
        try:
            from django.db import transaction
            from apps.users.serializers import RegisterSerializer
            from apps.users.otp import issue_email_otp
            ser = RegisterSerializer(data=form)
            if ser.is_valid():
                with transaction.atomic():
                    user = ser.save()
                    user.is_active = False
                    user.save(update_fields=["is_active"])
                try:
                    issue_email_otp(user, purpose="register")
                except Exception as exc:
                    # keep inactive user; allow them to retry by registering again? delete to avoid lockout by email/username uniqueness
                    try:
                        user.delete()
                    except Exception:
                        pass
                    messages.error(request,
                        (f"Could not send verification email: {exc}. Configure Gmail SMTP in .env (EMAIL_HOST_USER, EMAIL_HOST_PASSWORD as Gmail App Password, EMAIL_HOST=smtp.gmail.com, EMAIL_PORT=587, EMAIL_USE_TLS=True, EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend)."
                         if request.session.get("ui_lang") == "en" else
                         f"যাচাইকরণ ইমেইল পাঠানো যায়নি: {exc}। .env-এ Gmail SMTP সেট করুন (EMAIL_HOST_USER, EMAIL_HOST_PASSWORD=Gmail App Password, EMAIL_HOST=smtp.gmail.com, EMAIL_PORT=587, EMAIL_USE_TLS=True, EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend)।"))
                    return render(request, "web/register.html", {"form": form})
                request.session["pending_verify_user_id"] = str(user.id)
                request.session["pending_verify_email"] = user.email
                messages.success(request,
                    "We sent a verification code to your email. Enter it below."
                    if request.session.get("ui_lang") == "en" else
                    "আপনার ইমেইলে একটি যাচাইকরণ কোড পাঠানো হয়েছে। নিচে কোডটি দিন।")
                return redirect("web:register_verify")
            errors = ser.errors
        except Exception as exc:
            errors = {"__all__": [str(exc)]}
        flat = []
        for k, v in (errors or {}).items():
            vals = v if isinstance(v, (list, tuple)) else ([v.values()] if isinstance(v, dict) else [v])
            for x in vals:
                if isinstance(x, (list, tuple)):
                    flat.extend(str(i) for i in x)
                else:
                    flat.append(str(x))
        if flat:
            messages.error(request, " ".join(flat[:3]))
    return render(request, "web/register.html", {"form": form})


def _pending_verify_user(request):
    from apps.users.models import User
    uid = request.session.get("pending_verify_user_id")
    if not uid:
        return None
    return User.objects.filter(id=uid, is_active=False).first()


@require_http_methods(["GET", "POST"])
def verify_email_view(request):
    if request.user.is_authenticated:
        return redirect("web:dashboard")
    user = _pending_verify_user(request)
    if user is None:
        messages.info(request,
            "No pending verification found. Please register." if request.session.get("ui_lang") == "en" else
            "কোনো অপেক্ষমাণ যাচাইকরণ নেই। অনুগ্রহ করে নিবন্ধন করুন।")
        return redirect("web:register")
    masked = request.session.get("pending_verify_email") or user.email
    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        from apps.users.otp import verify_email_otp
        ok, msg = verify_email_otp(user, "register", code)
        if ok:
            user.is_active = True
            user.save(update_fields=["is_active"])
            try:
                from apps.core.middleware import get_audit_context, record_audit
                from apps.users.models import LoginEvent
                ip = get_audit_context().get("ip")
                ua = get_audit_context().get("user_agent", "")
                LoginEvent.objects.create(user=user, username_attempted=user.username,
                                          result=LoginEvent.Result.SUCCESS, ip_address=ip, user_agent=ua)
                record_audit("create", user, resource_type="User", resource_id=user.pk)
                record_audit("login", user, resource_type="User", resource_id=user.pk)
            except Exception:
                pass
            for k in ("pending_verify_user_id", "pending_verify_email"):
                request.session.pop(k, None)
            login(request, user)
            messages.success(request,
                "Email verified. Welcome to FinCore!" if request.session.get("ui_lang") == "en" else
                "ইমেইল যাচাই হয়েছে। FinCore-এ স্বাগতম!")
            return redirect("web:dashboard")
        messages.error(request, msg)
    return render(request, "web/verify_email.html", {"email": masked, "username": user.username})


@require_POST
def resend_otp_view(request):
    user = _pending_verify_user(request)
    if user is None:
        return redirect("web:register")
    try:
        from apps.users.otp import issue_email_otp
        issue_email_otp(user, purpose="register")
        messages.success(request,
            "A new code was sent to your email." if request.session.get("ui_lang") == "en" else
            "আপনার ইমেইলে নতুন কোড পাঠানো হয়েছে।")
    except Exception as exc:
        messages.error(request,
            (f"Could not resend email: {exc}" if request.session.get("ui_lang") == "en" else f"ইমেইল আবার পাঠানো যায়নি: {exc}"))
    return redirect("web:register_verify")

@login_required
@require_http_methods(["GET", "POST"])
def manage_users_view(request):
    if not (request.user.is_staff or request.user.is_superuser):
        messages.error(request, "You do not have access to this page." if request.session.get("ui_lang") == "en" else "এই পেজে আপনার প্রবেশাধিকার নেই।")
        return redirect("web:dashboard")
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from django.utils import timezone
    U = get_user_model()
    en = request.session.get("ui_lang") == "en"

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        uid = request.POST.get("user_id")
        target = U.objects.filter(pk=uid).first()
        back = request.POST.get("next") or (reverse("web:manage_users") + f"?status={request.POST.get('status','all')}&q={request.POST.get('q','')}")
        if not (back.startswith("/") and "manage-users" in back):
            back = reverse("web:manage_users")
        if target is None:
            messages.error(request, "User not found." if en else "ব্যবহারকারী পাওয়া যায়নি।")
            return redirect(back)
        if target.pk == request.user.pk:
            messages.error(request, "You cannot modify your own account from here." if en else "আপনি এখান থেকে নিজের অ্যাকাউন্ট পরিবর্তন করতে পারবেন না।")
            return redirect(back)
        is_last_super = target.is_superuser and not U.objects.filter(is_superuser=True).exclude(pk=target.pk).exists()
        try:
            if action == "deactivate":
                if is_last_super:
                    messages.error(request, "Cannot deactivate the last superuser." if en else "শেষ superuser নিষ্ক্রিয় করা যাবে না।")
                else:
                    target.is_active = False
                    target.save(update_fields=["is_active"])
                    messages.success(request, f"{target.username} deactivated." if en else f"{target.username} নিষ্ক্রিয় করা হয়েছে।")
            elif action == "activate":
                target.is_active = True
                target.save(update_fields=["is_active"])
                messages.success(request, f"{target.username} activated." if en else f"{target.username} সক্রিয় করা হয়েছে।")
            elif action == "delete":
                if is_last_super:
                    messages.error(request, "Cannot delete the last superuser." if en else "শেষ superuser মুছে ফেলা যাবে না।")
                else:
                    from apps.core.reset import ResetFailed, wipe_user_data
                    uname = target.username
                    try:
                        summary = wipe_user_data(target)
                    except ResetFailed as exc:
                        messages.error(request, (f"কিছুই মোছা হয়নি — হিসাব অক্ষত আছে। কারণ: {exc}"))
                        return redirect(back)
                    except Exception as exc:
                        messages.error(request, (f"রিসেট ব্যর্থ হয়েছে, কিছুই মোছা হয়নি: {exc}"))
                        return redirect(back)
                    try:
                        target.delete()
                    except Exception as exc:
                        messages.error(request, (f"ডেটা মুছে দেওয়া হয়েছে ({summary['rows']} রো, {summary['businesses']} ব্যবসা) কিন্তু ইউজার অ্যাকাউন্ট মুছতে পারিনি: {exc}"))
                        return redirect(back)
                    messages.success(request, (f"{uname} মুছে ফেলা হয়েছে — {summary['rows']} রো + {summary['businesses']} ব্যবসা সম্পূর্ণ মুছে ফেলা হয়েছে।"))
            else:
                messages.error(request, "Unknown action." if en else "অজানা অ্যাকশন।")
        except Exception as exc:
            messages.error(request, (f"Action failed: {exc}" if en else f"অ্যাকশন ব্যর্থ: {exc}"))
        return redirect(back)

    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "all").strip()
    qs = U.objects.all().order_by("-date_joined")
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(phone__icontains=q))
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    elif status == "staff":
        qs = qs.filter(Q(is_staff=True) | Q(is_superuser=True))
    rows_raw = list(qs[:300])
    now = timezone.now()
    rows = []
    for u in rows_raw:
        name = (u.get_full_name() or "").strip() or u.username
        initial = (name[:1] or "U").upper()
        rows.append({"u": u, "name": name, "initial": initial})
    stats = {
        "total": U.objects.count(),
        "active": U.objects.filter(is_active=True).count(),
        "inactive": U.objects.filter(is_active=False).count(),
        "staff": U.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).count(),
        "new_7d": U.objects.filter(date_joined__gte=now - timezone.timedelta(days=7)).count(),
    }
    return render(request, "web/manage_users.html", {"rows": rows, "q": q, "stats": stats, "status": status, "shown": len(rows)})

@login_required
@require_http_methods(["GET", "POST"])
def reset_data_view(request):
    """Delete every record of the signed-in user, keeping the login account.

    The heavy lifting lives in `apps.core.reset` — it wipes *only this user's*
    businesses (plus their own personal/session rows), and raises instead of
    silently leaving half the data behind.
    """
    en = request.session.get("ui_lang") == "en"
    if request.method == "POST":
        from apps.core.reset import ResetFailed, wipe_user_data

        try:
            summary = wipe_user_data(request.user)
        except ResetFailed as exc:
            messages.error(request, (
                f"Nothing was deleted — the books are untouched. Reason: {exc}"
                if en else
                f"কিছুই মোছা হয়নি — হিসাব অক্ষত আছে। কারণ: {exc}"))
        except Exception as exc:  # pragma: no cover - safety net
            messages.error(request, (
                f"Reset failed and was rolled back: {exc}"
                if en else
                f"রিসেট ব্যর্থ হয়েছে, কিছুই মোছা হয়নি: {exc}"))
        else:
            request.session.pop("active_business_id", None)
            if summary["businesses"]:
                messages.success(request, (
                    f"Deleted {summary['businesses']} business(es) and "
                    f"{summary['rows']} records. Your login account was kept."
                    if en else
                    f"{summary['businesses']}টি ব্যবসা ও {summary['rows']}টি রেকর্ড "
                    f"মুছে ফেলা হয়েছে। আপনার লগইন অ্যাকাউন্ট রাখা হয়েছে।"))
            else:
                messages.success(request, (
                    "No business data was left; personal and log rows were cleared. "
                    "Your login account was kept."
                    if en else
                    "ব্যবসার কোনো ডাটা ছিল না; ব্যক্তিগত ও লগ রেকর্ড মুছে ফেলা হয়েছে। "
                    "আপনার লগইন অ্যাকাউন্ট রাখা হয়েছে।"))
        return redirect("web:dashboard")

    return render(request, "web/reset_data.html", {})


@login_required
@require_POST
def set_theme_view(request):
    """Save the chosen UI theme on the user account (follows every device)."""
    from apps.web.context_processors import THEME_IDS
    theme = (request.POST.get("theme") or "").strip()
    if theme not in THEME_IDS:
        messages.error(request, "Unknown theme." if request.session.get("ui_lang") == "en"
                       else "অজানা থিম।")
        return redirect(request.META.get("HTTP_REFERER") or "web:dashboard")
    request.user.theme = theme
    request.user.save(update_fields=["theme"])
    back = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
    if not back.startswith("/"):
        back = "/"
    return redirect(back)


@login_required
def export_summary_view(request, fmt):
    """Download the হিসাব সারাংশ report as PDF, Excel or Word."""
    from apps.web.summary_export import (
        build_summary_export, export_summary_docx, export_summary_pdf,
        export_summary_xlsx,
    )

    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(
            request,
            "Select a business to export the summary." if en
            else "হিসাব সারাংশ এক্সপোর্ট করতে একটি ব্যবসা নির্বাচন করুন।",
        )
        return redirect("web:dashboard")

    today = timezone.localdate()

    def _parse(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    dfrom = _parse(request.GET.get("from")) or today.replace(day=1)
    dto = _parse(request.GET.get("to")) or today
    if dfrom > dto:
        dfrom, dto = dto, dfrom

    fmt_key = (fmt or "").lower()
    if fmt_key in ("excel", "xlsx"):
        fmt_key, extension = "xlsx", "xlsx"
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif fmt_key in ("word", "docx"):
        fmt_key, extension = "docx", "docx"
        content_type = ("application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document")
    elif fmt_key == "pdf":
        extension = "pdf"
        content_type = "application/pdf"
    else:
        raise Http404("Unsupported export format")

    try:
        data = build_summary_export(
            biz, dfrom, dto, request.session.get("ui_lang", "bn"))
        if fmt_key == "pdf":
            content = export_summary_pdf(data)
        elif fmt_key == "xlsx":
            content = export_summary_xlsx(data)
        else:
            content = export_summary_docx(data)
    except Exception:
        logger.exception("Summary export failed (format=%s, business=%s)",
                         fmt_key, biz.id)
        messages.error(
            request,
            "Could not create the export. Please try again." if en
            else "এক্সপোর্ট তৈরি করা যায়নি। আবার চেষ্টা করুন।",
        )
        return redirect("web:summary")

    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = (
        f'attachment; filename="{data["filename"]}.{extension}"')
    return response


@login_required
@require_http_methods(["GET"])
def reports_view(request):
    """রিপোর্ট — লাভ-ক্ষতি (P&L), নগদের আনাগোনা (Cash flow), পার্টনার স্টেটমেন্ট.

    Plain GET renders the page with inline previews + export buttons.
    GET with ``?report=pnl|cashflow|partner&format=pdf|xlsx`` downloads the
    report as a PDF or Excel file. Partner-role users are locked to their
    own book; an admin may pick any partner with ``?partner=``.
    """
    from apps.web.report_exports import (
        build_cashflow_report, build_partner_statement, build_pnl_report,
        cashflow_pdf, cashflow_xlsx, partner_statement_pdf,
        partner_statement_xlsx, pnl_pdf, pnl_xlsx,
    )
    from apps.web.exports import PDF_CONTENT_TYPE
    from apps.partners.models import Partner

    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business first." if en else "আগে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")

    today = timezone.localdate()

    def _parse(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    preset = (request.GET.get("preset") or "month").strip()
    dfrom = _parse(request.GET.get("from"))
    dto = _parse(request.GET.get("to"))
    if preset == "year":
        dfrom = date(today.year, 1, 1)
        dto = date(today.year, 12, 31)
    elif preset == "last_month":
        first = today.replace(day=1)
        last_end = first.fromordinal(first.toordinal() - 1)
        dfrom = last_end.replace(day=1)
        dto = last_end
    elif preset == "month":
        dfrom = today.replace(day=1)
        dto = today
    else:  # custom (or all-time anchor)
        preset = "custom"
        if dfrom is None:
            dfrom = today.replace(day=1)
        if dto is None:
            dto = today
    if dfrom > dto:
        dfrom, dto = dto, dfrom

    # ---- partner resolution (book the statement is for) -------------------
    own = _own_partner(biz, membership, request.user)
    partner_id = (request.GET.get("partner") or "").strip()
    partner = None
    partners = list(biz.partners.filter(status="ACTIVE").order_by("name"))
    if own:
        if partner_id and str(partner_id) != str(own.id):
            raise PermissionDenied("You can only see your own partner book.")
        partner = own
    elif partner_id:
        partner = next((p for p in partners if str(p.id) == str(partner_id)), None)
    if partner is None and partners:
        partner = partners[0]

    report = (request.GET.get("report") or "").strip().lower()
    fmt = (request.GET.get("format") or request.GET.get("fmt") or "").strip().lower()
    lang = request.session.get("ui_lang", "bn")

    if report:
        if report not in ("pnl", "cashflow", "partner"):
            raise Http404("Unknown report")
        XLSX_TYPE = ("application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet")

        def _download(data, content, extension, content_type):
            response = HttpResponse(content, content_type=content_type)
            response["Content-Disposition"] = (
                f'attachment; filename="{data["filename"]}.{extension}"')
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response

        def _fail(msg_en, msg_bn):
            messages.error(request, msg_en if en else msg_bn)
            return redirect("web:reports")

        try:
            if report == "pnl":
                data = build_pnl_report(biz, dfrom, dto, lang)
                if fmt == "xlsx":
                    return _download(data, pnl_xlsx(data), "xlsx", XLSX_TYPE)
                return _download(data, pnl_pdf(data), "pdf", PDF_CONTENT_TYPE)
            if report == "cashflow":
                data = build_cashflow_report(biz, dfrom, dto, lang)
                if fmt == "xlsx":
                    return _download(data, cashflow_xlsx(data), "xlsx", XLSX_TYPE)
                return _download(data, cashflow_pdf(data), "pdf", PDF_CONTENT_TYPE)
            if report == "partner":
                if partner is None:
                    return _fail("Select a partner first.",
                                 "আগে একটি পার্টনার নির্বাচন করুন।")
                data = build_partner_statement(biz, partner, dfrom, dto, lang)
                if fmt == "xlsx":
                    return _download(data, partner_statement_xlsx(data),
                                     "xlsx", XLSX_TYPE)
                return _download(data, partner_statement_pdf(data),
                                 "pdf", PDF_CONTENT_TYPE)
        except Exception:
            logger.exception("Report export failed (report=%s, format=%s, business=%s)",
                             report, fmt, biz.id)
            messages.error(request,
                           "Could not create the report. Please try again." if en
                           else "রিপোর্ট তৈরি করা যায়নি। আবার চেষ্টা করুন।")
            return redirect("web:reports")

    pnl = build_pnl_report(biz, dfrom, dto, lang)
    cashflow = build_cashflow_report(biz, dfrom, dto, lang)
    partner_statement = (build_partner_statement(biz, partner, dfrom, dto, lang)
                         if partner else None)

    return render(request, "web/reports.html", {
        "preset": preset,
        "dfrom": dfrom,
        "dto": dto,
        "today": today.isoformat(),
        "default_month_start": today.replace(day=1).isoformat(),
        "default_year_start": date(today.year, 1, 1).isoformat(),
        "partners": partners,
        "partner": partner,
        "pnl": pnl,
        "cashflow": cashflow,
        "partner_statement": partner_statement,
        "can_pick_partner": own is None,
        "export_query": f"from={dfrom.isoformat()}&to={dto.isoformat()}"
                        + (f"&preset={preset}" if preset != "custom" else "")
                        + (f"&partner={partner.id}" if partner else ""),
    })


@login_required
@require_http_methods(["GET"])
def notifications_view(request):
    """All notifications, in a focused reading view.

    The drawer (in every page's chrome) shows the newest few; this page is the
    full log with filters + live alerts on top.
    """
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business first.")
        return redirect("web:dashboard")

    from apps.web import notifications as notif
    from apps.web.models import Notification

    filter_kind = (request.GET.get("kind") or "").strip()
    allowed_kinds = {k for k, _ in Notification.Kind.choices}
    if filter_kind not in allowed_kinds and filter_kind != "unread":
        filter_kind = ""

    timeline = notif.timeline(
        biz, request.user,
        kind=None if filter_kind in ("", "unread") else filter_kind,
        unread_only=(filter_kind == "unread"),
        limit=80,
    )
    alerts = notif.merge_sort(notif.live_alerts(biz))

    ctx_now = timezone.now()
    return render(request, "web/notifications.html", {
        "timeline": timeline,
        "alerts": alerts,
        "unread": notif.unread_count(biz, request.user),
        "current_filter": filter_kind,
        "now": ctx_now,
        "total_shown": sum(len(g["items"]) for g in timeline),
        "week_count": Notification.objects.filter(
            business=biz, recipient=request.user,
            created_at__gte=ctx_now - timedelta(days=7)).count(),
    })


@login_required
@require_http_methods(["POST"])
def notif_mark_read_view(request, notification_id):
    from apps.web.models import Notification
    biz, _membership = _require_business(request)
    if not biz:
        return JsonResponse({"ok": False}, status=400)
    n = Notification.objects.filter(
        pk=notification_id, business=biz, recipient=request.user).first()
    if not n:
        return JsonResponse({"ok": False, "error": "not_found"},
                            status=404)
    if not n.is_read:
        n.is_read = True
        n.read_at = timezone.now()
        n.save(update_fields=["is_read", "read_at"])
    return JsonResponse({"ok": True,
                         "unread": Notification.objects.filter(
                             business=biz, recipient=request.user,
                             is_read=False).count()})


@login_required
@require_http_methods(["POST"])
def notif_mark_all_read_view(request):
    from apps.web.models import Notification
    biz, _membership = _require_business(request)
    if not biz:
        return JsonResponse({"ok": False}, status=400)
    Notification.objects.filter(
        business=biz, recipient=request.user, is_read=False).update(
            is_read=True, read_at=timezone.now())
    return JsonResponse({"ok": True, "unread": 0})


@login_required
@require_http_methods(["POST"])
def notif_clear_view(request):
    """Clear (delete) every stored notification for this user.

    Scoped to the active business — same scope as the badge. Live alerts
    (budget over / installment due) are computed on the fly and cannot be
    cleared; they disappear by themselves once the condition is resolved.
    """
    from apps.web.models import Notification
    biz, _membership = _require_business(request)
    if not biz:
        return JsonResponse({"ok": False}, status=400)
    deleted, _ = Notification.objects.filter(
        business=biz, recipient=request.user).delete()
    return JsonResponse({"ok": True, "unread": 0, "deleted": deleted})


@login_required
@require_http_methods(["GET"])
def notif_drawer_view(request):
    """HTML fragment for the bell drawer — newest events + live alerts."""
    biz, _membership = _require_business(request)
    from apps.web import notifications as notif
    from apps.web.models import Notification

    en = request.session.get("ui_lang") == "en"
    events = []
    alerts = []
    if biz:
        for n in Notification.objects.filter(
                business=biz, recipient=request.user).order_by(
                    "-created_at")[:8]:
            events.append({
                "id": n.id,
                "title": n.title,
                "body": n.body,
                "link": n.link,
                "amount": n.amount,
                "is_read": n.is_read,
                "created_at": n.created_at,
                "icon": notif.kind_icon(n.kind),
                "tone": notif.kind_tone(n.kind),
                "rel": notif.rel_time(n.created_at),
            })
        # unread first, newest first inside each bucket
        events.sort(key=lambda e: (not e["is_read"], -e["created_at"].timestamp()))
        alerts = notif.merge_sort(notif.live_alerts(biz))

    return render(request, "web/partials/notif_drawer.html", {
        "events": events,
        "alerts": alerts,
        "unread": notif.unread_count(biz, request.user) if biz else 0,
        "en": en,
    })


@login_required
@require_http_methods(["GET", "POST"])
def business_users_view(request):
    """টিম ও পারমিশন — the owner manages who can do what in this business.

    Only the OWNER (ADMIN role) may open this page. An owner can add
    existing registered users (by username or email), set their role
    (Accountant / Viewer / keep Partner), remove them, and each change is
    notified to the affected user.
    """
    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business first." if en else "আগে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")
    if membership.role != BusinessMembership.Role.ADMIN:
        messages.error(request,
                       "Only the business owner can manage the team." if en
                       else "শুধু ব্যবসার মালিকই টিম পরিচালনা করতে পারবেন।")
        return redirect("web:dashboard")

    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from apps.web import notifications as notif

    U = get_user_model()
    back = reverse("web:business_users")

    ROLE_LABELS = {
        BusinessMembership.Role.ADMIN: "মালিক",
        BusinessMembership.Role.ACCOUNTANT: "হিসাবরক্ষক",
        BusinessMembership.Role.VIEWER: "দর্শক",
        BusinessMembership.Role.PARTNER: "পার্টনার",
    }

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        try:
            if action == "add_member":
                query = (request.POST.get("member") or "").strip()
                role = (request.POST.get("role") or "").strip()
                if role not in (BusinessMembership.Role.ACCOUNTANT,
                                BusinessMembership.Role.VIEWER):
                    role = BusinessMembership.Role.ACCOUNTANT
                if not query:
                    messages.error(request, "Enter a username or email." if en
                                   else "ইউজার আইডি বা ইমেইল দিন।")
                    return redirect(back)
                target = (U.objects.filter(Q(username__iexact=query) |
                                            Q(email__iexact=query))
                           .filter(is_active=True).first())
                if target is None:
                    messages.error(request, "User not found. Ask them to register first." if en
                                   else "ইউজার পাওয়া যায়নি — আগে রেজিস্টার করান।")
                    return redirect(back)
                if target.pk == request.user.pk:
                    messages.error(request, "You are already on the team." if en
                                   else "আপনি ইতিপূর্বেই টিমে আছেন।")
                    return redirect(back)
                existing = BusinessMembership.objects.filter(
                    business=biz, user=target).first()
                if existing:
                    if not existing.is_active:
                        existing.is_active = True
                        existing.role = role
                        existing.save(update_fields=["role", "is_active"])
                        messages.success(request,
                                         f"{target.username} re-activated as {ROLE_LABELS.get(role)}." if en
                                         else f"{target.username} আবার যোগ হয়েছে — {ROLE_LABELS.get(role)}।")
                        try:
                            notif.notify_team_update(
                                biz, recipient=target,
                                title=f"{biz.name}-এ আবার যুক্ত হয়েছেন",
                                body=f"{request.user.username} আপনাকে {ROLE_LABELS.get(role)} হিসেবে ব্যবসায় যুক্ত করেছেন।",
                                link="/notifications/")
                        except Exception:
                            pass
                        return redirect(back)
                    messages.error(request, "Already a member." if en
                                   else "আগে থেকেই টিমে আছে।")
                    return redirect(back)
                BusinessMembership.objects.create(
                    user=target, business=biz, role=role, is_active=True)
                messages.success(request,
                                 f"{target.username} added as {ROLE_LABELS.get(role)}." if en
                                 else f"{target.username} টিমে যোগ হয়েছে — {ROLE_LABELS.get(role)}।")
                try:
                    notif.notify_team_update(
                        biz, recipient=target,
                        title=f"{biz.name}-এ যুক্ত হয়েছেন",
                        body=f"{request.user.username} আপনাকে {ROLE_LABELS.get(role)} হিসেবে ব্যবসায় যুক্ত করেছেন।",
                        link="/notifications/")
                except Exception:
                    pass
                return redirect(back)

            elif action == "set_role":
                uid = (request.POST.get("user_id") or "").strip()
                role = (request.POST.get("role") or "").strip()
                target = U.objects.filter(pk=uid).first()
                if target is None:
                    messages.error(request, "User not found." if en else "সদস্য পাওয়া যায়নি।")
                    return redirect(back)
                if role not in ROLE_LABELS:
                    messages.error(request, "Unknown role." if en else "অজানা রোল।")
                    return redirect(back)
                membership_target = BusinessMembership.objects.filter(
                    business=biz, user=target).first()
                if membership_target is None:
                    messages.error(request, "Not a member of this business." if en
                                   else "এই ব্যবসার সদস্য নয়।")
                    return redirect(back)
                if membership_target.user_id == request.user.pk:
                    messages.error(request, "You cannot change your own role here." if en
                                   else "এখান থেকে নিজের রোল বদলাতে পারবেন না।")
                    return redirect(back)
                if membership_target.role == BusinessMembership.Role.ADMIN and \
                        role != BusinessMembership.Role.ADMIN:
                    remaining = BusinessMembership.objects.filter(
                        business=biz, role=BusinessMembership.Role.ADMIN,
                        is_active=True).count()
                    if remaining <= 1:
                        messages.error(request, "At least one owner must remain." if en
                                       else "অন্তত একজন মালিক থাকতেই হবে।")
                        return redirect(back)
                previous_label = ROLE_LABELS.get(membership_target.role)
                membership_target.role = role
                membership_target.save(update_fields=["role"])
                messages.success(request,
                                 f"{target.username} → {ROLE_LABELS.get(role)}." if en
                                 else f"{target.username} → {ROLE_LABELS.get(role)} হয়েছে।")
                try:
                    notif.notify_team_update(
                        biz, recipient=target,
                        title=f"রোল পরিবর্তন — {biz.name}",
                        body=f"{request.user.username} আপনার রোল বদলেছেন: {previous_label} → {ROLE_LABELS.get(role)}।",
                        link="/notifications/")
                except Exception:
                    pass
                return redirect(back)

            elif action == "remove_member":
                uid = (request.POST.get("user_id") or "").strip()
                target = U.objects.filter(pk=uid).first()
                membership_target = BusinessMembership.objects.filter(
                    business=biz, user=target).first()
                if membership_target is None:
                    messages.error(request, "Not a member." if en else "সদস্য নয়।")
                    return redirect(back)
                if membership_target.user_id == request.user.pk:
                    messages.error(request, "You cannot remove yourself." if en
                                   else "নিজেকে সরাতে পারবেন না।")
                    return redirect(back)
                if membership_target.role == BusinessMembership.Role.ADMIN:
                    remaining = BusinessMembership.objects.filter(
                        business=biz, role=BusinessMembership.Role.ADMIN,
                        is_active=True).count()
                    if remaining <= 1:
                        messages.error(request, "At least one owner must remain." if en
                                       else "অন্তত একজন মালিক থাকতেই হবে।")
                        return redirect(back)
                membership_target.is_active = False
                membership_target.save(update_fields=["is_active"])
                messages.success(request,
                                 f"{target.username} removed." if en
                                 else f"{target.username} সরানো হয়েছে।")
                try:
                    notif.notify_team_update(
                        biz, recipient=target,
                        title=f"টিম থেকে সরানো হলো — {biz.name}",
                        body=f"{request.user.username} আপনাকে {biz.name}-এর টিম থেকে সরিয়ে দিয়েছেন।",
                        link="/notifications/")
                except Exception:
                    pass
                return redirect(back)

            else:
                messages.error(request, "Unknown action." if en else "অজানা অ্যাকশন।")
                return redirect(back)
        except Exception as exc:
            logger.exception("business_users_view POST failed")
            messages.error(request, f"Failed: {exc}" if en else f"ব্যর্থ: {exc}")
            return redirect(back)

    members = list(BusinessMembership.objects.filter(
        business=biz, is_active=True).select_related("user")
        .order_by("role", "user__username"))
    rows = []
    for m in members:
        rows.append({
            "membership": m,
            "user": m.user,
            "name": (m.user.get_full_name() or "").strip() or m.user.username,
            "initial": ((m.user.get_full_name() or "") or m.user.username)[:1].upper(),
            "role_label": ROLE_LABELS.get(m.role, m.role),
            "is_self": m.user_id == request.user.pk,
        })
    stats = {
        "total": len(rows),
        "owners": sum(1 for r in rows if r["membership"].role == BusinessMembership.Role.ADMIN),
        "accountants": sum(1 for r in rows if r["membership"].role == BusinessMembership.Role.ACCOUNTANT),
        "viewers": sum(1 for r in rows if r["membership"].role == BusinessMembership.Role.VIEWER),
        "partners": sum(1 for r in rows if r["membership"].role == BusinessMembership.Role.PARTNER),
    }
    return render(request, "web/business_users.html", {
        "rows": rows,
        "stats": stats,
        "role_labels": ROLE_LABELS,
    })


@login_required
@require_http_methods(["GET", "POST"])
def business_settings_view(request):
    """ব্যবসার সেটিংস — নাম, ফোন, ইমেইল ও ঠিকানা (শুধু অ্যাডমিন).

    Renaming is display-only: the slug, the books, the ledger and every
    stored record stay exactly as they are, so no link and no balance
    breaks — only the name shown in the sidebar, the chips, the export
    headers and the reminder messages changes.
    """
    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(request, "Select a business first." if en
                     else "প্রথমে একটি ব্যবসা নির্বাচন করুন।")
        return redirect("web:dashboard")
    if membership.role != BusinessMembership.Role.ADMIN:
        messages.error(request, "Only the business admin can change these settings."
                       if en else "শুধু ব্যবসার অ্যাডমিনই এই সেটিংস বদলাতে পারবেন।")
        return redirect("web:dashboard")

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        contact_phone = (request.POST.get("contact_phone") or "").strip()
        contact_email = (request.POST.get("contact_email") or "").strip()
        address = (request.POST.get("address") or "").strip()
        if not name:
            messages.error(request, "Business name is required." if en
                           else "ব্যবসার নাম দিন।")
            return render(request, "web/business_settings.html", {
                "business": biz, "form": request.POST})
        previous = {
            "name": biz.name,
            "contact_phone": biz.contact_phone,
            "contact_email": biz.contact_email,
            "address": biz.address,
        }
        biz.name = name[:160]
        biz.contact_phone = contact_phone[:32]
        biz.contact_email = contact_email[:254]
        biz.address = address
        biz.save(update_fields=["name", "contact_phone", "contact_email",
                                 "address", "updated_at"])
        record_audit("business_update", biz, business=biz, previous=previous,
                     changes={"name": biz.name, "contact_phone": biz.contact_phone,
                              "contact_email": biz.contact_email,
                              "address": biz.address})
        messages.success(request, f"Business renamed to '{biz.name}'." if en
                         else f"ব্যবসার নাম বদলে '{biz.name}' করা হয়েছে।")
        return redirect("web:business_settings")

    return render(request, "web/business_settings.html", {
        "business": biz,
        "form": {
            "name": biz.name,
            "contact_phone": biz.contact_phone,
            "contact_email": biz.contact_email,
            "address": biz.address,
        },
    })


def create_business_view(request):
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        contact_phone = (request.POST.get("contact_phone") or "").strip()
        contact_email = (request.POST.get("contact_email") or "").strip()
        address = (request.POST.get("address") or "").strip()
        en = request.session.get("ui_lang") == "en"
        if not name:
            messages.error(request, "Business name is required." if en else "ব্যবসার নাম দিন।")
            return render(request, "web/create_business.html", {"form": request.POST})
        try:
            from django.db import transaction
            from django.utils.text import slugify
            base = slugify(name)[:150] or "business"
            slug, i = base, 1
            while Business.objects.filter(slug=slug).exists():
                i += 1
                slug = f"{base}-{i}"
            with transaction.atomic():
                business = Business.objects.create(
                    owner=request.user,
                    name=name,
                    slug=slug,
                    contact_phone=contact_phone,
                    contact_email=contact_email,
                    address=address,
                    currency="BDT",
                    timezone="Asia/Dhaka",
                    is_active=True,
                )
                BusinessMembership.objects.create(
                    user=request.user,
                    business=business,
                    role=BusinessMembership.Role.ADMIN,
                    partner_permissions={
                        "can_create_transactions": True,
                        "can_edit_own_records": True,
                        "can_edit_other_partners_dues": True,
                        "can_view_all_reports": True,
                        "can_manage_customers": True,
                        "can_view_partners": True,
                    },
                )
                try:
                    from apps.core.middleware import record_audit
                    record_audit("create", None, business=business,
                                 resource_type="Business", resource_id=business.pk)
                except Exception:
                    pass
            request.session["active_business_id"] = str(business.id)
            messages.success(request,
                f"Business '{business.name}' created. You are now the admin." if en else
                f"ব্যবসা '{business.name}' তৈরি হয়েছে। আপনি এখন অ্যাডমিন।")
            return redirect("web:dashboard")
        except Exception as exc:
            messages.error(request, (f"Could not create business: {exc}" if en else f"ব্যবসা তৈরি করা যায়নি: {exc}"))
            return render(request, "web/create_business.html", {"form": request.POST})
    return render(request, "web/create_business.html", {"form": {}})


@require_http_methods(["GET"])
def landing_view(request):
    """Public marketing landing page (Light Clean concept)."""
    return render(request, "web/landing.html")

@require_http_methods(["GET"])
def home_view(request):
    """Landing page for visitors; dashboard for signed-in users."""
    # Lazy (opportunistic) auto-backup: at most once per 24h the site writes
    # a local snapshot per user — and, if Google Drive is connected, pushes
    # each connected user's snapshot to their Drive. Free hosting, no paid
    # scheduler required. Never raises; the page is unaffected either way.
    try:
        from apps.drivesync.autobackup import (
            maybe_run_auto_backup, maybe_sync_connected_drives,
        )
        maybe_run_auto_backup()
        maybe_sync_connected_drives()
    except Exception:
        pass
    if request.user.is_authenticated:
        return dashboard(request)
    return render(request, "web/landing.html")


# ---------------------------------------------------------------- palette --

@login_required
@require_http_methods(["GET"])
def search_view(request):
    """Ctrl+K command palette — pages, live data lookups, actions.

    Returns JSON groups; every query is role-scoped (same limits as
    everywhere else in the app). Deterministic matching only — no LLM,
    no network calls. Actions never post anything by themselves: they
    navigate to /assistant/ with the command prefilled, where the usual
    proposal → confirm flow applies.
    """
    import re

    from apps.assistant.actions import parse_command

    q = (request.GET.get("q") or "").strip()
    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    groups = []
    lq = q.lower()

    def item(icon, title, sub, url, tone="slate", action=None, question=None):
        it = {"icon": icon, "title": title, "sub": sub, "url": url,
              "tone": tone}
        if action:
            it["action"] = action
        if question:
            it["question"] = question
        return it

    if not q:
        groups.append({
            "key": "hints",
            "label": "কীভাবে ব্যবহার করবেন" if not en else "How to use",
            "items": [
                item("fa-magnifying-glass",
                     "গ্রাহক/পার্টনার খুঁজুন — নাম বা ফোন" if not en else "Find customers/partners by name or phone",
                     "", "/customers/"),
                item("fa-bolt",
                     "বাকি <customer> <amount>" if not en else "baki <customer> <amount> — add a due",
                     "Enter", "/assistant/"),
                item("fa-cloud-arrow-up",
                     "Drive-এ সিঙ্ক" if not en else "Sync to Drive",
                     "", "/drive/"),
            ],
        })
        return JsonResponse({"ok": True, "groups": groups})

    # ---- 1) pages --------------------------------------------------------
    PAGES = [
        ("/", "fa-chart-pie", "ড্যাশবোর্ড", "Dashboard", "ড্যাশবোর্ড dashboard home"),
        ("/transactions/", "fa-arrow-right-arrow-left", "লেনদেন", "Transactions", "লেনদেন transactions ledger"),
        ("/customers/", "fa-book", "পার্টনার ও বাকি খাতা", "Baki khata", "বাকি baki khata customers কাস্টমার due"),
        ("/personal/", "fa-wallet", "ব্যক্তিগত হিসাব", "Personal", "ব্যক্তিগত personal home"),
        ("/summary/", "fa-layer-group", "সারসংক্ষেপ", "Summary", "সারসংক্ষেপ summary"),
        ("/reports/", "fa-file-chart-column", "রিপোর্ট", "Reports", "রিপোর্ট reports profit লাভ"),
        ("/expenses/", "fa-cart-shopping", "খরচ / ক্রয়", "Expenses", "খরচ expenses cost ক্রয়"),
        ("/loans/", "fa-hand-holding-dollar", "ঋণ ও কিস্তি", "Loans", "ঋণ loans installment কিস্তি"),
        ("/investments/", "fa-chart-line", "বিনিয়োগ", "Investments", "বিনিয়োগ investments বিনিয়োগ profit"),
        ("/notifications/", "fa-bell", "নোটিফিকেশন", "Notifications", "নোটি notification alerts"),
        ("/supercalculator/", "fa-calculator", "সুপার ক্যালকুলেটর", "Super calculator", "ক্যালকুলেটর calculator math"),
        ("/assistant/", "fa-wand-magic-sparkles", "ফিনকোরকে জিজ্ঞেস করুন", "Ask FinCore", "assistant প্রশ্ন ask baki command"),
        ("/drive/", "fa-cloud-arrow-up", "অটো ব্যাকআপ / Drive সিঙ্ক", "Backup & Drive", "ব্যাকআপ backup সিঙ্ক sync drive পারসনাল"),
        ("/profile/", "fa-user", "প্রোফাইল", "Profile", "প্রোফাইল profile settings আমার"),
        ("/business/settings/", "fa-gear", "ব্যবসার সেটিংস", "Business settings", "সেটিংস settings ব্যবসায়িক"),
    ]
    page_hits = []
    for url, icon, bn, en_label, keywords in PAGES:
        hay = f"{bn} {en_label} {keywords}".lower()
        if lq in hay:
            page_hits.append(item(icon, en_label if en else bn,
                                  "", url, "blue"))
        if len(page_hits) >= 5:
            break
    if page_hits:
        groups.append({"key": "pages",
                       "label": "পেজ" if not en else "Pages",
                       "items": page_hits})

    # ---- 2) customers / partners (business context) -----------------------
    if biz and len(q) >= 2:
        from apps.customers.models import Customer
        from apps.partners.models import Partner
        from apps.assistant.actions import (
            _own_partner_for, _resolve_assignment, business_first_active_partner,
        )

        def _customer_url(c):
            own = _own_partner_for(biz, membership)
            partner = _resolve_assignment(c, None, own)
            if partner is None:
                partner = business_first_active_partner(business=biz)
            if partner is not None:
                return f"/customers/?partner={partner.id}&history={c.id}"
            return "/customers/"

        cust_hits = []
        try:
            for c in Customer.objects.filter(
                    business=biz, status="ACTIVE", name__icontains=q)[:4]:
                cust_hits.append(
                    item("fa-user", c.name, c.phone or c.email or "",
                         _customer_url(c), "amber"))
        except Exception:
            pass
        try:
            for c in Customer.objects.filter(
                    business=biz, status="ACTIVE",
                    phone__icontains=re.sub(r"\D", "", q) or "~~~")[:2]:
                url = _customer_url(c)
                if all(h["url"] != url for h in cust_hits):
                    cust_hits.append(
                        item("fa-phone", c.name, c.phone or "", url, "amber"))
        except Exception:
            pass
        if cust_hits:
            groups.append({"key": "customers",
                           "label": "গ্রাহক" if not en else "Customers",
                           "items": cust_hits[:5]})

        part_hits = []
        try:
            for p in Partner.objects.filter(
                    business=biz, status="ACTIVE", name__icontains=q)[:3]:
                part_hits.append(
                    item("fa-user-tie", p.name,
                         "বাকি খাতা খুলুন" if not en else "Open the due book",
                         f"/customers/?partner={p.id}", "violet"))
        except Exception:
            pass
        if part_hits:
            groups.append({"key": "partners",
                           "label": "পার্টনার" if not en else "Partners",
                           "items": part_hits})

    # ---- 3) transactions --------------------------------------------------
    if biz and len(q) >= 2:
        from apps.transactions.models import Transaction

        tx_hits = []
        try:
            for t in (Transaction.objects.filter(business=biz)
                      .exclude(status="REVERSED")
                      .filter(description__icontains=q)
                      .order_by("-created_at")[:4]):
                tx_hits.append(
                    item("fa-arrow-right-arrow-left",
                         f"৳{t.amount:,.2f} — {t.description[:44]}",
                         f"{t.transaction_date:%d %b %Y}",
                         f"/transactions/?q={quote(q)}", "slate"))
        except Exception:
            pass
        amount_match = re.fullmatch(r"\s*([0-9][0-9,\.]*)\s*", q)
        if amount_match:
            try:
                from decimal import Decimal
                amt = Decimal(amount_match.group(1).replace(",", ""))
                for t in (Transaction.objects.filter(business=biz, amount=amt)
                          .exclude(status="REVERSED")
                          .order_by("-created_at")[:2]):
                    url = f"/transactions/?q={quote(q)}"
                    if all(h["url"] != url for h in tx_hits):
                        tx_hits.append(
                            item("fa-bangladeshi-taka-sign",
                                 f"৳{t.amount:,.2f} — {t.description[:44] or ('—' if not en else 'no note')}",
                                 f"{t.transaction_date:%d %b %Y}",
                                 url, "slate"))
            except Exception:
                pass
        if tx_hits:
            groups.append({"key": "transactions",
                           "label": "লেনদেন" if not en else "Transactions",
                           "items": tx_hits[:4]})

    # ---- 4) assistant action shortcut --------------------------------------
    if biz and len(q) >= 3 and can_write(membership):
        try:
            parsed = parse_command(q, biz, membership)
        except Exception:
            parsed = None
        if parsed and parsed.get("kind") == "proposed":
            action_kind = str(parsed.get("action_kind") or "")
            label_map = {
                "ADD_BAKI": "বাকি যোগ" if not en else "Add baki",
                "ADD_PAYMENT": "পরিশোধ যোগ" if not en else "Add payment",
                "ADD_EXPENSE": "খরচ যোগ" if not en else "Add expense",
            }
            kind_label = label_map.get(action_kind, action_kind)
            amount = parsed.get("amount")
            cust = parsed.get("customer")
            desc = cust.name if cust else (parsed.get("description") or "")
            groups.append({
                "key": "actions",
                "label": "অ্যাকশন" if not en else "Actions",
                "items": [item(
                    "fa-bolt",
                    f"{kind_label}: {desc} — ৳{amount:,.2f}",
                    "Enter → /assistant/-এ প্রস্তাবিত হওয়ার পরে এক ক্লিকে নিশ্চিত করুন" if not en
                    else "Enter → propose on /assistant/, then confirm",
                    "/assistant/?q=" + quote(q) + "&auto=1",
                    "green",
                    action="assistant", question=q)],
            })

    return JsonResponse({"ok": True, "groups": groups})


@require_http_methods(["GET"])
def robots_view(request):
    """robots.txt — private ERP: only the public pages are listed."""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /landing/\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        f"Sitemap: {request.scheme}://{request.get_host()}/sitemap.xml\n"
    )
    return HttpResponse(body, content_type="text/plain")


@require_http_methods(["GET"])
def service_worker_view(request):
    """Root-scope service worker for the PWA app-shell.

    Served at /sw.js with ``Service-Worker-Allowed: /`` so the worker can
    control the whole site (not just /static/). Public: the worker only
    precaches static assets and the offline page — it never holds ledger
    data, so no login is required to download it.
    """
    from pathlib import Path

    from django.conf import settings as _settings

    path = (Path(_settings.BASE_DIR) / "apps" / "web" / "static"
            / "web" / "sw.js")
    try:
        body = path.read_bytes()
    except OSError:
        return HttpResponse("sw.js not found", status=404,
                            content_type="text/plain")
    resp = HttpResponse(body, content_type="application/javascript; charset=utf-8")
    resp["Service-Worker-Allowed"] = "/"
    resp["Cache-Control"] = "no-cache, must-revalidate"
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required
@require_http_methods(["GET"])
def position_view(request):
    """হিসাব কমান্ড সেন্টার — the self-auditing net-position statement.

    Read-only: nothing here posts, edits or deletes a transaction.  Every
    figure comes from ``build_statement`` (the same data the exports use).
    """
    from apps.web.position_statement import build_statement

    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(
            request,
            "Select a business to see the position statement." if en
            else "হিসাব কমান্ড সেন্টার দেখতে একটি ব্যবসা নির্বাচন করুন।",
        )
        return redirect("web:dashboard")

    def _parse(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    today = timezone.localdate()
    as_of = _parse(request.GET.get("as_of")) or today
    compare = _parse(request.GET.get("compare"))
    statement = build_statement(
        biz, as_of=as_of, compare=compare,
        lang="en" if en else "bn")

    return render(request, "web/position.html", {
        "st": statement,
        "as_of": statement["as_of"],
        "compare": statement["comparison"]["compare_date"]
        if statement["comparison"] else None,
        "ui_lang": "en" if en else "bn",
    })


@login_required
@require_http_methods(["GET"])
def export_position_view(request, fmt):
    """Download the হিসাব কমান্ড সেন্টার statement as PDF, Excel or Word."""
    from apps.web.position_export import (
        build_position_export, export_position_docx, export_position_pdf,
        export_position_xlsx,
    )
    from apps.web.position_statement import build_statement

    en = request.session.get("ui_lang") == "en"
    biz, membership = _require_business(request)
    if not biz:
        messages.info(
            request,
            "Select a business to export the statement." if en
            else "হিসাব কমান্ড সেন্টার এক্সপোর্ট করতে একটি ব্যবসা নির্বাচন করুন।",
        )
        return redirect("web:dashboard")

    def _parse(raw_value):
        raw_value = (raw_value or "").strip()
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError:
            return None

    fmt_key = (fmt or "").lower()
    if fmt_key in ("excel", "xlsx"):
        fmt_key, extension = "xlsx", "xlsx"
        content_type = ("application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")
    elif fmt_key in ("word", "docx"):
        fmt_key, extension = "docx", "docx"
        content_type = ("application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document")
    elif fmt_key == "pdf":
        extension = "pdf"
        content_type = "application/pdf"
    else:
        raise Http404("Unsupported export format")

    try:
        statement = build_statement(
            biz, as_of=_parse(request.GET.get("as_of")),
            lang="en" if en else "bn")
        data = build_position_export(
            biz, statement, "en" if en else "bn")
        if fmt_key == "pdf":
            content = export_position_pdf(data)
        elif fmt_key == "xlsx":
            content = export_position_xlsx(data)
        else:
            content = export_position_docx(data)
    except Exception:
        logger.exception("Position export failed (format=%s, business=%s)",
                         fmt_key, biz.id)
        messages.error(
            request,
            "Could not create the export. Please try again." if en
            else "এক্সপোর্ট তৈরি করা যায়নি। আবার চেষ্টা করুন।",
        )
        return redirect("web:position")

    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = (
        f'attachment; filename="{data["filename"]}.{extension}"')
    return response


@require_http_methods(["GET"])
def sitemap_view(request):
    """Minimal sitemap - the public marketing page only."""
    base = f"{request.scheme}://{request.get_host()}"
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{base}/</loc><changefreq>monthly</changefreq><priority>1.0</priority></url>\n"
        f"  <url><loc>{base}/landing/</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>\n"
        "</urlset>\n"
    )
    return HttpResponse(body, content_type="application/xml")
