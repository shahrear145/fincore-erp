import json
from decimal import Decimal, InvalidOperation

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def money(value):
    try:
        v = Decimal(str(value)).quantize(Decimal("0.01"))
        neg = v < 0
        s = f"{abs(v):,.2f}"
        return f"-৳{s}" if neg else f"৳{s}"
    except (InvalidOperation, TypeError, ValueError):
        return f"৳{value}"


@register.filter
def signed(value):
    """-৳500.00 / ৳600.00 — the minus sits in front of the sign."""
    try:
        v = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return f"৳{value}"
    if v < 0:
        return f"-৳{abs(v):,.2f}"
    return f"৳{v:,.2f}"


@register.filter
def rawnum(value):
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except Exception:
        return str(value)


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return ""
    if isinstance(mapping, dict):
        return mapping.get(key, "")
    return ""


@register.filter
def signclass(value):
    try:
        v = Decimal(str(value))
        if v < 0:
            return "neg"
        if v > 0:
            return "pos"
    except Exception:
        s = str(value)
        if s.startswith("-"):
            return "neg"
    return ""


@register.filter
def tojson(value):
    def _d(o):
        if isinstance(o, Decimal):
            return str(o)
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)
    return mark_safe(json.dumps(value, default=_d))
