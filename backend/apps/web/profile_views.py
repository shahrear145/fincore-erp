"""Profile & account-security pages (session-authenticated web UI).

Everything here is strictly user-scoped: the signed-in user can only touch
their own account. Email changes and password resets are verified with a
6-digit OTP sent by email (hashed at rest, expiring, max 5 attempts).
Changing the password keeps the current browser session alive
(`update_session_auth_hash`) while blacklisting every API (JWT) refresh
token and revoking every recorded device session.
"""
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction as db_transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.core.middleware import get_audit_context, record_audit
from apps.users.models import LoginEvent, User

AVATAR_MAX_BYTES = 3 * 1024 * 1024
AVATAR_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _is_en(request):
    return request.session.get("ui_lang") == "en"


def _say(request, en, bn, tag="success"):
    getattr(messages, tag)(request, en if _is_en(request) else bn)


def _flatten_errors(errors):
    """Turn Django password-validation errors into one friendly string."""
    out = []
    for err in errors:
        out.extend(getattr(err, "messages", None) or [str(err)])
    return " ".join(out[:3])


def _blacklist_jti(user, jti):
    """Blacklist one API refresh token (best effort; web session unaffected)."""
    if not jti:
        return
    try:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken, OutstandingToken,
        )
        outstanding = OutstandingToken.objects.filter(user=user, jti=jti).first()
        if outstanding:
            BlacklistedToken.objects.get_or_create(token=outstanding)
    except Exception:
        pass


def _blacklist_all_tokens(user):
    try:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken, OutstandingToken,
        )
        for token in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=token)
    except Exception:
        pass


def _revoke_all_device_sessions(user):
    for session in user.device_sessions.filter(is_active=True):
        session.revoke()


def _validate_password_or_messages(request, raw, user):
    from django.contrib.auth.password_validation import validate_password
    try:
        validate_password(raw, user=user)
        return True
    except ValidationError as exc:
        _say(request, _flatten_errors(exc.error_list),
             _flatten_errors(exc.error_list), tag="error")
        return False


def _mask_email(email):
    try:
        name, domain = email.rsplit("@", 1)
    except ValueError:
        return email
    visible = name[:2]
    return f"{visible}{'•' * max(len(name) - 2, 1)}@{domain}"


@login_required
@require_http_methods(["GET", "POST"])
def profile_view(request):
    user = request.user

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "update_info":
            full = (request.POST.get("full_name") or "").strip()
            phone = (request.POST.get("phone") or "").strip()
            if len(full) > 150 or len(phone) > 32:
                _say(request, "Name or phone is too long.", "নাম বা ফোন নম্বর অনেক বড়।", "error")
            else:
                parts = full.split(" ", 1)
                user.first_name = parts[0][:150]
                user.last_name = (parts[1] if len(parts) > 1 else "")[:150]
                user.phone = phone
                user.save(update_fields=["first_name", "last_name", "phone"])
                record_audit("update", user, resource_type="User",
                             resource_id=user.pk, changes={"phone": phone})
                _say(request, "Profile updated.", "প্রোফাইল আপডেট হয়েছে।")

        elif action == "upload_avatar":
            file = request.FILES.get("avatar")
            if file is None:
                _say(request, "Choose an image first.", "আগে একটি ছবি বাছুন।", "error")
            elif file.size > AVATAR_MAX_BYTES:
                _say(request, "Image is larger than 3 MB.", "ছবিটি ৩ MB-এর বেশি বড়।", "error")
            elif not any(file.name.lower().endswith(ext) for ext in AVATAR_EXTS):
                _say(request, "Use a JPG, PNG or WebP image.",
                     "JPG, PNG বা WebP ছবি দিন।", "error")
            else:
                if user.avatar:
                    try:
                        user.avatar.delete(save=False)
                    except Exception:
                        pass
                user.avatar = file
                user.save(update_fields=["avatar"])
                record_audit("update", user, resource_type="User",
                             resource_id=user.pk, changes={"avatar": "uploaded"})
                _say(request, "Profile picture updated.", "প্রোফাইল ছবি আপডেট হয়েছে।")

        elif action == "remove_avatar":
            if user.avatar:
                try:
                    user.avatar.delete(save=False)
                except Exception:
                    pass
                user.avatar = None
                user.save(update_fields=["avatar"])
            _say(request, "Profile picture removed.", "প্রোফাইল ছবি সরানো হয়েছে।", "info")

        elif action == "change_password":
            current = request.POST.get("current_password") or ""
            p1 = request.POST.get("new_password1") or ""
            p2 = request.POST.get("new_password2") or ""
            if not user.check_password(current):
                _say(request, "Current password is incorrect.",
                     "বর্তমান পাসওয়ার্ড সঠিক নয়।", "error")
            elif p1 != p2:
                _say(request, "The two new passwords do not match.",
                     "নতুন পাসওয়ার্ড দুটি মিলছে না।", "error")
            elif _validate_password_or_messages(request, p1, user):
                user.set_password(p1)
                user.save(update_fields=["password"])
                # Stay signed in here; kill every other session/token.
                update_session_auth_hash(request, user)
                with db_transaction.atomic():
                    _blacklist_all_tokens(user)
                    _revoke_all_device_sessions(user)
                record_audit("update", user, resource_type="User",
                             resource_id=user.pk,
                             changes={"password": "***changed***"})
                _say(request,
                     "Password changed. Other devices were signed out.",
                     "পাসওয়ার্ড বদলানো হয়েছে। অন্য ডিভাইসগুলো থেকে লগআউট করা হয়েছে।")

        elif action == "email_request":
            current = request.POST.get("current_password") or ""
            new_email = (request.POST.get("new_email") or "").strip().lower()
            if not user.check_password(current):
                _say(request, "Current password is incorrect.",
                     "বর্তমান পাসওয়ার্ড সঠিক নয়।", "error")
            elif not new_email:
                _say(request, "Enter the new email.", "নতুন ইমেইল লিখুন।", "error")
            else:
                try:
                    validate_email(new_email)
                except ValidationError:
                    _say(request, "That is not a valid email address.",
                         "এটি সঠিক ইমেইল ঠিকানা নয়।", "error")
                if new_email.lower() == user.email.lower():
                    _say(request, "This is already your email.",
                         "এটি এখন আপনার বর্তমান ইমেইলই।", "error")
                elif User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
                    _say(request, "Another account already uses this email.",
                         "এই ইমেইল অন্য একটি অ্যাকাউন্টে ব্যবহৃত।", "error")
                else:
                    try:
                        from apps.users.otp import issue_email_otp
                        issue_email_otp(user, purpose="email_change",
                                        send_to=new_email)
                        request.session["pending_email_change"] = new_email
                        request.session["pending_email_change_at"] = timezone.now().isoformat()
                        _say(request,
                             f"A confirmation code was sent to {new_email}. Enter it below.",
                             f"নতুন ইমেইল {new_email}-এ একটি কোড পাঠানো হয়েছে। নিচে কোডটি দিন।")
                    except Exception as exc:
                        _say(request, f"Could not send the email: {exc}",
                             f"ইমেইল পাঠানো যায়নি: {exc}", "error")

        elif action == "email_resend":
            pending = request.session.get("pending_email_change")
            if not pending:
                _say(request, "No pending email change.", "কোনো অপেক্ষমাণ ইমেইল পরিবর্তন নেই।", "info")
            else:
                try:
                    from apps.users.otp import issue_email_otp
                    issue_email_otp(user, purpose="email_change", send_to=pending)
                    _say(request, "A new code was sent.", "নতুন কোড পাঠানো হয়েছে।")
                except Exception as exc:
                    _say(request, f"Could not resend: {exc}", f"আবার পাঠানো যায়নি: {exc}", "error")

        elif action == "email_verify":
            pending = request.session.get("pending_email_change")
            code = (request.POST.get("code") or "").strip()
            if not pending:
                _say(request, "No pending email change.", "কোনো অপেক্ষমাণ ইমেইল পরিবর্তন নেই।", "info")
            else:
                from apps.users.otp import verify_email_otp
                ok, msg = verify_email_otp(user, "email_change", code)
                if ok:
                    old = user.email
                    user.email = pending
                    user.save(update_fields=["email"])
                    request.session.pop("pending_email_change", None)
                    request.session.pop("pending_email_change_at", None)
                    record_audit("update", user, resource_type="User",
                                 resource_id=user.pk,
                                 changes={"email": f"{old} -> {pending}"})
                    _say(request, "Email updated.", "ইমেইল আপডেট হয়েছে।")
                else:
                    _say(request, msg, msg, "error")

        elif action == "email_cancel":
            request.session.pop("pending_email_change", None)
            request.session.pop("pending_email_change_at", None)
            _say(request, "Email change cancelled.", "ইমেইল পরিবর্তন বাতিল হয়েছে।", "info")

        elif action == "pin_set":
            p1 = (request.POST.get("pin1") or "").strip()
            p2 = (request.POST.get("pin2") or "").strip()
            if p1 != p2:
                _say(request, "The two PINs do not match.", "দুটি PIN মিলছে না।", "error")
            elif not (p1.isdigit() and len(p1) in (4, 5, 6)):
                _say(request, "PIN must be 4-6 digits.", "PIN ৪-৬ সংখ্যার হতে হবে।", "error")
            else:
                user.set_pin(p1)
                user.save(update_fields=["pin_hash"])
                record_audit("update", user, resource_type="User",
                             resource_id=user.pk, changes={"pin": "***set***"})
                _say(request, "PIN saved.", "PIN সংরক্ষিত হয়েছে।")

        elif action == "device_logout":
            sid = request.POST.get("session_id") or ""
            session = user.device_sessions.filter(id=sid, is_active=True).first()
            if session:
                _blacklist_jti(user, session.refresh_jti)
                session.revoke()
                _say(request, "Device signed out.", "ডিভাইস থেকে লগআউট করা হয়েছে।")
            else:
                _say(request, "That device session is gone.", "সেই ডিভাইস সেশনটি আর নেই।", "info")

        elif action == "device_logout_all":
            for session in user.device_sessions.filter(is_active=True):
                _blacklist_jti(user, session.refresh_jti)
                session.revoke()
            _say(request, "All devices were signed out.",
                 "সব ডিভাইস থেকে লগআউট করা হয়েছে।")

        return redirect("web:profile")

    return render(request, "web/profile.html", _profile_ctx(request))


def _profile_ctx(request):
    user = request.user
    pending_email = request.session.get("pending_email_change")
    return {
        "devices": user.device_sessions.filter(is_active=True)[:12],
        "events": user.login_events.order_by("-created_at")[:12],
        "pending_email": pending_email,
        "pending_email_masked": _mask_email(pending_email or ""),
        "email_masked": _mask_email(user.email),
        "full_name": user.get_full_name() or user.username,
        "has_pin": bool(user.pin_hash),
    }


@require_http_methods(["GET", "POST"])
def password_forgot_view(request):
    """Step 1: ask for the account email; never reveal whether it exists."""
    if request.user.is_authenticated:
        return redirect("web:dashboard")
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            try:
                from apps.users.otp import issue_email_otp
                issue_email_otp(user, purpose="password_reset")
                request.session["pwreset_user_id"] = str(user.id)
                request.session["pwreset_email"] = user.email
                return redirect("web:password_reset")
            except Exception as exc:
                _say(request, f"Could not send the email: {exc}",
                     f"ইমেইল পাঠানো যায়নি: {exc}", "error")
        else:
            _say(request,
                 "If that email belongs to an account, a reset code was sent. Check inbox and Spam.",
                 "ইমেইলটি যদি কোনো অ্যাকাউন্টে থাকে, রিসেট কোড পাঠানো হয়েছে। ইনবক্স ও স্প্যাম দেখুন।",
                 "info")
    return render(request, "web/forgot_password.html")


def _pwreset_user(request):
    uid = request.session.get("pwreset_user_id")
    if not uid:
        return None
    return User.objects.filter(id=uid, is_active=True).first()


@require_http_methods(["GET", "POST"])
def password_reset_view(request):
    """Step 2: the emailed code + a brand-new password."""
    if request.user.is_authenticated:
        return redirect("web:dashboard")
    user = _pwreset_user(request)
    if user is None:
        _say(request, "Start the reset by entering your email first.",
             "রিসেট শুরু করতে আগে আপনার ইমেইল দিন।", "info")
        return redirect("web:password_forgot")

    masked = _mask_email(request.session.get("pwreset_email") or user.email)
    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        p1 = request.POST.get("new_password1") or ""
        p2 = request.POST.get("new_password2") or ""
        if p1 != p2:
            _say(request, "The two new passwords do not match.",
                 "নতুন পাসওয়ার্ড দুটি মিলছে না।", "error")
        elif not _validate_password_or_messages(request, p1, user):
            pass
        else:
            from apps.users.otp import verify_email_otp
            ok, msg = verify_email_otp(user, "password_reset", code)
            if not ok:
                _say(request, msg, msg, "error")
            else:
                user.set_password(p1)
                user.save(update_fields=["password"])
                with db_transaction.atomic():
                    _blacklist_all_tokens(user)
                    _revoke_all_device_sessions(user)
                try:
                    ip = get_audit_context().get("ip")
                    ua = get_audit_context().get("user_agent", "")
                    LoginEvent.objects.create(user=user,
                                              username_attempted=user.username,
                                              result=LoginEvent.Result.SUCCESS,
                                              ip_address=ip, user_agent=ua)
                    record_audit("update", user, resource_type="User",
                                 resource_id=user.pk,
                                 changes={"password": "***reset***"})
                except Exception:
                    pass
                request.session.pop("pwreset_user_id", None)
                request.session.pop("pwreset_email", None)
                _say(request,
                     "Password reset. Sign in with your new password.",
                     "পাসওয়ার্ড রিসেট হয়েছে। নতুন পাসওয়ার্ড দিয়ে লগইন করুন।")
                return redirect("web:login")
    return render(request, "web/password_reset.html", {"email": masked})


@require_POST
def password_reset_resend_view(request):
    user = _pwreset_user(request)
    if user is None:
        return redirect("web:password_forgot")
    try:
        from apps.users.otp import issue_email_otp
        issue_email_otp(user, purpose="password_reset")
        _say(request, "A new code was sent to your email.",
             "আপনার ইমেইলে নতুন কোড পাঠানো হয়েছে।")
    except Exception as exc:
        _say(request, f"Could not resend: {exc}", f"আবার পাঠানো যায়নি: {exc}", "error")
    return redirect("web:password_reset")
