"""Payment reminders for customers who still carry a due (বাকি).

A reminder never touches the books — it only tells the customer what is
owed. Two hand-off channels:

- WhatsApp — the shop opens `wa.me` with the message pre-written; the
  owner presses Send in their own WhatsApp. No API key, no cost.
- Email — sent through the same SMTP settings the OTP mail already uses.

Every attempt is written to `DueReminder` so the shop can see when it
last chased a customer, and `COOLDOWN_DAYS` keeps the same customer from
being chased every single day.
"""
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django.conf import settings
from django.core.mail import send_mail

# How long to wait before the same customer may be reminded again.
COOLDOWN_DAYS = 7

BD_COUNTRY_CODE = "880"


def format_money(value) -> str:
    """৳2,000.00 — same shape as the `money` template filter."""
    try:
        v = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return f"৳{value}"
    return f"-৳{abs(v):,.2f}" if v < 0 else f"৳{abs(v):,.2f}"


def normalize_bd_phone(raw) -> str:
    """International digits for a WhatsApp link, or '' when unusable.

    01712345678 → 8801712345678, 1712345678 → 8801712345678,
    8801712345678 → 8801712345678. Anything else is rejected instead of
    guessing (a wrong number is worse than no reminder).
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith(BD_COUNTRY_CODE) and len(digits) == 13:
        return digits
    if len(digits) == 11 and digits.startswith("0"):
        return BD_COUNTRY_CODE + digits[1:]
    if len(digits) == 10 and digits.startswith("1"):
        return BD_COUNTRY_CODE + digits
    return ""


def build_reminder_message(*, customer, business, partner, amount, lang="bn") -> str:
    """The basic reminder: name + business + due + a polite request."""
    money = format_money(amount)
    if lang == "en":
        return (
            f"Dear {customer.name},\n"
            f"You have an outstanding balance of {money} at {business.name}.\n"
            f"Please arrange the payment. Thank you!\n"
            f"— {partner.name}"
        )
    return (
        f"প্রিয় {customer.name},\n"
        f"{business.name}-এর খাতায় আপনার বাকি {money}।\n"
        f"অনুগ্রহ করে পরিশোধ করুন। ধন্যবাদ!\n"
        f"— {partner.name}"
    )


def build_email_subject(*, business, lang="bn") -> str:
    if lang == "en":
        return f"Payment reminder — {business.name}"
    return f"{business.name} — বাকি পরিশোধের অনুরোধ"


def build_whatsapp_url(phone_digits: str, message: str) -> str:
    """Deep link that opens WhatsApp with the message already typed."""
    return f"https://wa.me/{phone_digits}?text={quote(message)}"


def send_email_reminder(*, recipient: str, subject: str, message: str) -> None:
    """Send the reminder mail. Raises on failure so the caller can log it."""
    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None) or None
    send_mail(subject, message, from_email, [recipient], fail_silently=False)
