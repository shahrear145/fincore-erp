"""Email OTP for registration, email change, and password reset (Gmail/SMTP)."""
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

# Bilingual subject + body per purpose. All codes are 6 digits and expire in
# EMAIL_OTP_EXPIRE_MINUTES minutes; the OTP record itself is hashed at rest.
_PURPOSE_TEXT = {
    "register": (
        "FinCore — Verify your email / আপনার ইমেইল যাচাই করুন",
        "Hello {username},\n\n"
        "Your FinCore verification code is: {code}\n"
        "This code expires in {minutes} minutes.\n\n"
        "আপনার FinCore যাচাইকরণ কোড: {code}\n"
        "এই কোডটি {minutes} মিনিটের মধ্যে মেয়াদ শেষ হবে।\n\n"
        "If you did not create this account, you can ignore this email.\n"
        "আপনি যদি এই অ্যাকাউন্ট না করে থাকেন, এই ইমেইলটি উপেক্ষা করুন।\n",
    ),
    "email_change": (
        "FinCore — Confirm your new email / নতুন ইমেইল নিশ্চিত করুন",
        "Hello {username},\n\n"
        "Your FinCore code to confirm this new email is: {code}\n"
        "This code expires in {minutes} minutes.\n\n"
        "নতুন ইমেইলটি নিশ্চিত করতে আপনার FinCore কোড: {code}\n"
        "এই কোডটি {minutes} মিনিটের মধ্যে মেয়াদ শেষ হবে।\n\n"
        "If you did not request this change, secure your account and ignore "
        "this email.\n"
        "আপনি যদি এই পরিবর্তন চাননি, নিরাপত্তা নিশ্চিত করুন এবং এই ইমেইলটি "
        "উপেক্ষা করুন।\n",
    ),
    "password_reset": (
        "FinCore — Password reset code / পাসওয়ার্ড রিসেট কোড",
        "Hello {username},\n\n"
        "Your FinCore password reset code is: {code}\n"
        "This code expires in {minutes} minutes.\n\n"
        "আপনার FinCore পাসওয়ার্ড রিসেট কোড: {code}\n"
        "এই কোডটি {minutes} মিনিটের মধ্যে মেয়াদ শেষ হবে।\n\n"
        "If you did not request a password reset, ignore this email — your "
        "password stays unchanged.\n"
        "আপনি যদি পাসওয়ার্ড রিসেট চাননি, এই ইমেইলটি উপেক্ষা করুন — পাসওয়ার্ড "
        "অপরিবর্তিত থাকবে।\n",
    ),
}


class MailSendError(RuntimeError):
    """Verification email could not be handed over to the SMTP server."""


def _explain(exc):
    """Turn cryptic smtplib errors into an actionable, human message."""
    text = str(exc) or exc.__class__.__name__
    name = exc.__class__.__name__
    if "Authentication Required" in text:
        return ("Gmail অনুমোদন দেয়নি (530) — .env-এ EMAIL_HOST_USER বা "
                "EMAIL_HOST_PASSWORD খালি আছে।")
    if name == "SMTPAuthenticationError" or text.startswith("(535"):
        return ("Gmail লগইন ব্যর্থ (535) — EMAIL_HOST_USER / "
                "Gmail App Password ঠিক নেই।")
    if name == "SMTPServerDisconnected" or "Connection unexpectedly closed" in text:
        return ("Gmail সংযোগ কেটে দিয়েছে — সাধারণত EMAIL_HOST_PASSWORD "
                "(Gmail App Password) ভুল বা খালি থাকলে এমন হয়। "
                "App Password হুবহু বসিয়ে Web ট্যাবে Reload দিন।")
    if "timed out" in text.lower():
        return "Gmail SMTP-তে পৌঁছানো যাচ্ছে না (timeout) — নেটওয়ার্ক দেখুন।"
    return text


def issue_email_otp(user, purpose="register", send_to=None):
    from apps.users.models import EmailOTP

    code = f"{secrets.randbelow(1_000_000):06d}"
    minutes = int(getattr(settings, "EMAIL_OTP_EXPIRE_MINUTES", 10) or 10)
    # expire older unused OTPs for same purpose
    EmailOTP.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True).update(
        consumed_at=timezone.now())
    otp = EmailOTP.objects.create(
        user=user,
        code_hash=EmailOTP.hash_code(code),
        purpose=purpose,
        expires_at=timezone.now() + timedelta(minutes=minutes),
    )

    subject, body = _PURPOSE_TEXT.get(purpose, _PURPOSE_TEXT["register"])
    text = body.format(username=user.username, code=code, minutes=minutes)
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or None
    try:
        send_mail(subject, text, from_email, [send_to or user.email],
                  fail_silently=False)
    except Exception as exc:
        raise MailSendError(_explain(exc)) from exc
    return otp


def verify_email_otp(user, purpose, raw_code):
    from apps.users.models import EmailOTP

    otp = (EmailOTP.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True)
           .order_by("-created_at").first())
    if otp is None:
        if purpose == "register":
            return False, "No verification code found. Please register again. / কোনো যাচাইকরণ কোড পাওয়া যায়নি।"
        return False, ("No active code found. Please request a new code. / "
                       "কোনো সক্রিয় কোড পাওয়া যায়নি। নতুন কোড নিন।")
    if not otp.is_valid():
        return False, "Code expired or too many attempts. Please request a new code. / কোডের মেয়াদ শেষ বা অনেকবার চেষ্টা হয়েছে।"
    if not otp.check_code(raw_code):
        otp.attempts += 1
        otp.save(update_fields=["attempts"])
        return False, "Invalid code. Please check and try again. / কোড সঠিক নয়। আবার চেষ্টা করুন।"
    otp.consumed_at = timezone.now()
    otp.save(update_fields=["consumed_at"])
    return True, "ok"
