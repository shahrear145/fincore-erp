"""Drive sync views — session-auth pages for the site."""
import json
import uuid

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.contrib import messages
from django.shortcuts import render
from django.views.decorators.http import require_POST, require_http_methods

from apps.drivesync import oauth
from apps.drivesync import service
from apps.drivesync.crypto import encrypt
from apps.drivesync.models import DriveAccount

from apps.drivesync.drive_push import (
    FILE_NAME, drive_credentials, ensure_backup_folder,
    push_user_snapshot,
)


def _get_account(user):
    return DriveAccount.objects.filter(user=user).first()


def _read_body(request):
    try:
        return json.loads(request.body.decode()) if request.body else {}
    except Exception:
        return {}


@login_required
def status_view(request):
    account = _get_account(request.user)
    configured = oauth.is_configured()
    try:
        from apps.drivesync.autobackup import (
            last_auto_backup_at, is_enabled,
        )
        last_auto = last_auto_backup_at()
        auto_backup_enabled = is_enabled()
    except Exception:
        last_auto = None
        auto_backup_enabled = True

    can_toggle = _can_toggle_auto_backup(request)

    return render(request, "drivesync/drive.html", {
        "account": account,
        "configured": configured,
        "redirect_uri": oauth.redirect_uri(),
        "last_auto": last_auto,
        "auto_backup_enabled": auto_backup_enabled,
        "can_toggle": can_toggle,
    })


def _can_toggle_auto_backup(request) -> bool:
    if request.user.is_superuser:
        return True
    from apps.organizations.models import BusinessMembership
    from apps.core.capabilities import can_manage_settings

    bid = request.session.get("active_business_id")
    qs = (BusinessMembership.objects
          .filter(user=request.user, is_active=True,
                  business__is_active=True)
          .select_related("business"))
    if bid:
        m = qs.filter(business_id=bid).first() or qs.first()
    else:
        m = qs.first()
    return bool(m and can_manage_settings(m))


@login_required
@require_POST
def auto_backup_toggle_view(request):
    """Flip the site-wide auto-backup switch (owner/admin only)."""
    if not _can_toggle_auto_backup(request):
        messages.error(request,
                       "শুধু মালিক/এডমিনই এই সুইচ বদলাতে পারেন।"
                       if request.session.get("ui_lang") != "en"
                       else "Only owners/admins can change this switch.")
        return HttpResponseRedirect(reverse("drivesync:status"))

    from apps.drivesync.autobackup import is_enabled, set_enabled

    new_state = not is_enabled()
    set_enabled(new_state)
    messages.success(
        request,
        ("অটো-ব্যাকআপ চালু করা হয়েছে।" if request.session.get("ui_lang") != "en"
         else "Auto-backup enabled.") if new_state else
        ("অটো-ব্যাকআপ বন্ধ করা হয়েছে।" if request.session.get("ui_lang") != "en"
         else "Auto-backup disabled."))
    return HttpResponseRedirect(reverse("drivesync:status"))


@login_required
def connect_view(request):
    if not oauth.is_configured():
        messages.error(request,
                       "গুগল Drive সংযোগ যোগ করা হয়নি। .env-এ "
                       "GOOGLE_CLIENT_ID ও GOOGLE_CLIENT_SECRET বসান।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    state = uuid.uuid4().hex
    request.session["drive_oauth_state"] = state
    return HttpResponseRedirect(oauth.authorization_url(state))


@login_required
def callback_view(request):
    state = request.GET.get("state")
    code = request.GET.get("code")
    if state != request.session.get("drive_oauth_state"):
        messages.error(request, "অনুরোধটি মেলেনি। আবার চেষ্টা করুন।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    request.session.pop("drive_oauth_state", None)
    if not code:
        messages.error(request, "গুগল থেকে অনুমোদন আসেনি।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    try:
        tokens = oauth.exchange_code(code)
    except oauth.DriveError as e:
        messages.error(request, e.message)
        return HttpResponseRedirect(reverse("drivesync:status"))
    refresh = tokens.get("refresh_token")
    scope = tokens.get("scope", "")
    account = _get_account(request.user)
    if account is None:
        account = DriveAccount(user=request.user)
    if refresh:
        account.refresh_token_encrypted = encrypt(refresh)
        account.status = DriveAccount.Status.CONNECTED
    account.scope = scope
    account.save()
    messages.success(request, "গুগল Drive সংযুক্ত হয়েছে। 'সিঙ্ক' চাপুন।")
    return HttpResponseRedirect(reverse("drivesync:status"))


@login_required
def download_backup_view(request):
    """Download the snapshot as a local .json file — no cloud account needed."""
    import json
    from django.utils import timezone
    snapshot = service.build_snapshot(request.user)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=1).encode("utf-8")
    fname = f"fincore-backup-{timezone.now():%Y%m%d-%H%M%S}.json"
    response = HttpResponse(payload, content_type="application/json")
    response["Content-Disposition"] = (
        f'attachment; filename="{fname}"; '
        f"filename*=UTF-8''{fname}"
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def upload_restore_view(request):
    """Restore from a locally-uploaded backup file (no Drive needed)."""
    upload = request.FILES.get("backup_file")
    if not upload:
        messages.error(request, "কোনো ব্যাকআপ ফাইল বাছাই করা হয়নি।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    if not upload.name.lower().endswith(".json"):
        messages.error(request, "ফাইলটি .json হওয়া উচিত।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    if request.POST.get("confirm") != "RESTORE":
        messages.error(request, "রিস্টোর নিশ্চিত করতে 'RESTORE' লিখে জমা দিন।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    try:
        payload = upload.read().decode("utf-8")
        snapshot = json.loads(payload)
    except Exception:
        messages.error(request, "ফাইলটি পড়া যায়নি — সঠিক JSON ব্যাকআপ ফাইল জমা দিন।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    try:
        report = service.restore_snapshot(request.user, snapshot)
        parts = [f"{report['personal_created']} ব্যক্তিগত রেকর্ড ফিরে এসেছে"
                 f" ({report['personal_deleted']} পুরনো মুছে গেছে)"]
        for b in report["businesses"]:
            line = (f"'{b['name']}': {b['inserted']} রো ফিরে এসেছে"
                    + (" (সম্পূর্ণ ওভাররাইট)" if b["overwrite"] else " (মিলিয়ে)"))
            if b.get("failed"):
                line += f" — {b['failed']} রো বসাতে পারেনি"
                if b.get("first_error"):
                    line += f": {b['first_error']}"
            parts.append(line)
        if report.get("personal_failed") and report.get("personal_first_error"):
            parts.append(f"ব্যক্তিগত error: {report['personal_first_error']}")
        messages.success(request, "✓ ঠিক আছে:\n" + "; ".join(parts))
    except Exception as e:
        messages.error(request, f"রিস্টোর করা যায়নি: {e}")
    return HttpResponseRedirect(reverse("drivesync:status"))


@login_required
@require_POST
def sync_view(request):
    account = _get_account(request.user)
    if account is None or not account.refresh_token_encrypted:
        messages.error(request, "প্রথমে গুগল Drive সংযুক্ত করুন।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    try:
        push_user_snapshot(account)
        messages.success(
            request,
            "✓ আপনার ডেটার স্ন্যাপশট Drive-এ আপলোড হয়েছে।"
            if request.session.get("ui_lang") != "en"
            else "✓ Your snapshot was uploaded to Drive.")
    except oauth.DriveError as e:
        messages.error(request, e.message)
    except Exception as e:
        messages.error(request, f"সিঙ্ক করা যায়নি: {e}")
    return HttpResponseRedirect(reverse("drivesync:status"))


@login_required
@require_POST
def inspect_view(request):
    """Show what the Drive backup file contains — without touching data."""
    account = _get_account(request.user)
    if account is None or not account.refresh_token_encrypted:
        messages.error(request, "প্রথমে গুগল Drive সংযুক্ত করুন।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    try:
        token = drive_credentials(account)
        folder_id = ensure_backup_folder(token)
        f = oauth.find_file(FILE_NAME, parent=folder_id, token=token)
        if not f:
            messages.error(request, "Drive-এ কোনো ব্যাকআপ ফাইল পাওয়া যায়নি।")
            return HttpResponseRedirect(reverse("drivesync:status"))
        payload = oauth.download_file(f["id"], token=token)
        snapshot = json.loads(payload.decode("utf-8"))
        info = service.summarize_snapshot(snapshot)
        labels = {
            "personal_transactions": "ব্যক্তিগত লেনদেন",
            "personal_budgets": "ব্যক্তিগত বাজেট",
            "savings_goals": "সঞ্চয় লক্ষ্য",
            "personal_loans": "ব্যক্তিগত ঋণ",
            "personal_accounts": "ব্যক্তিগত অ্যাকাউন্ট",
            "personal_categories": "ব্যক্তিগত ক্যাটাগরি",
            "transactions": "লেনদেন",
            "transaction_date_memory": "তারিখ স্মৃতি",
            "recurring_transactions": "পৌনঃপুনিক লেনদেন",
            "due_entries": "বাকি এন্ট্রি",
            "payment_allocations": "পরিশোধ বণ্টন",
            "customer_documents": "কাস্টমার ডকুমেন্ট",
            "due_reminders": "বাকি রিমাইন্ডার",
            "customer_assignments": "কাস্টমার অ্যাসাইনমেন্ট",
            "customers": "কাস্টমার",
            "partners": "পার্টনার",
            "journal_lines": "জার্নাল লাইন",
            "journal_entries": "জার্নাল এন্ট্রি",
            "accounting_accounts": "হিসাব অ্যাকাউন্ট",
            "loan_entries": "ঋণ/বিনিয়োগ এন্ট্রি",
            "loan_parties": "ঋণদাতা/বিনিয়োগকারী",
            "cash_counts": "ক্যাশ কাউন্ট",
            "stock_snapshots": "স্টক",
            "financial_periods": "আর্থিক পিরিয়ড",
            "memberships": "মেম্বার",
            "businesses": "ব্যবসা",
        }

        def fmt(items):
            return ", ".join(
                f"{labels.get(k, k)} {v}" for k, v in items.items())

        parts = []
        personal = fmt(info["personal"])
        parts.append(f"ব্যক্তিগত — {personal or 'কিছু নেই'}")
        for b in info["businesses"]:
            parts.append(
                f"ব্যবসা '{b['name']}' — {fmt(b['rows']) or 'কিছু নেই'}"
                + (" (একক মালিকানা)" if b["sole_owner"] else " (শেয়ার করা)"))
        when = (info["created_at"] or "")[:19].replace("T", " ")
        messages.info(request,
                      f"ব্যাকআপ ফাইলে যা আছে (তারিখ {when}, ইউজার "
                      f"{info['user']}):\n" + " | ".join(parts))
    except oauth.DriveError as e:
        messages.error(request, e.message)
    except Exception as e:
        messages.error(request, f"ব্যাকআপ ফাইল পড়া যায়নি: {e}")
    return HttpResponseRedirect(reverse("drivesync:status"))


@login_required
@require_POST
def restore_view(request):
    account = _get_account(request.user)
    if account is None or not account.refresh_token_encrypted:
        messages.error(request, "প্রথমে গুগল Drive সংযুক্ত করুন।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    if request.POST.get("confirm") != "RESTORE":
        messages.error(request, "রিস্টোর নিশ্চিত করতে 'RESTORE' লিখে জমা দিন।")
        return HttpResponseRedirect(reverse("drivesync:status"))
    try:
        token = drive_credentials(account)
        folder_id = ensure_backup_folder(token)
        f = oauth.find_file(FILE_NAME, parent=folder_id, token=token)
        if not f:
            messages.error(request, "Drive-এ কোনো ব্যাকআপ ফাইল পাওয়া যায়নি।")
            return HttpResponseRedirect(reverse("drivesync:status"))
        payload = oauth.download_file(f["id"], token=token)
        snapshot = json.loads(payload.decode("utf-8"))
        report = service.restore_snapshot(request.user, snapshot)
        from django.utils import timezone
        account.last_restore_at = timezone.now()
        account.save()
        parts = [f"{report['personal_created']} ব্যক্তিগত রেকর্ড ফিরে এসেছে"
                 f" ({report['personal_deleted']} পুরনো মুছে গেছে)"]
        for b in report["businesses"]:
            line = (f"'{b['name']}': {b['inserted']} রো ফিরে এসেছে"
                    + (" (সম্পূর্ণ ওভাররাইট)" if b["overwrite"] else " (মিলিয়ে)"))
            if b.get("failed"):
                line += f" — {b['failed']} রো বসাতে পারেনি"
                if b.get("first_error"):
                    line += f": {b['first_error']}"
            parts.append(line)
        if report.get("personal_failed"):
            if report.get("personal_first_error"):
                parts.append(f"ব্যক্তিগত error: "
                             f"{report['personal_first_error']}")
        messages.success(request, "✓ ঠিক আছে:\n" + "; ".join(parts))
    except oauth.DriveError as e:
        messages.error(request, e.message)
    except Exception as e:
        messages.error(request, f"রিস্টোর করা যায়নি: {e}")
    return HttpResponseRedirect(reverse("drivesync:status"))


@login_required
@require_POST
def disconnect_view(request):
    account = _get_account(request.user)
    if account:
        account.delete()
        messages.success(request, "গুগল Drive সংযোগ বিচ্ছিন্ন হয়েছে।")
    return HttpResponseRedirect(reverse("drivesync:status"))
