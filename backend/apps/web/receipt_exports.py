"""Invoice-style PDFs for customers.

 দুই ধরনের ডকুমেন্ট:

1. ``customer_statement_pdf`` — "বাকি চালান" (customer statement / dues invoice)
   ব্যবসায়ের লোগো + নাম, গ্রাহকের খাতার সব লেনদেন, চলতি বাকি ও স্বাক্ষর রেখা।

2. ``money_receipt_pdf`` — "পরিশোধ রশিকা" (payment money receipt)
   একটা PAYMENT লেনদেনের রশিকা; টাকার কথায় (বাংলা বানানে) ও অঙ্কে যোগ লেখা থাকে।

BNGL-ল্যাটিন-ফন্ট (NotoSansBengaliLatin) বান্ডেলড — fpdf2 এর text-shaping দিয়ে
বাংলা শব্দ সঠিকভাবে ছাপা হয়। ফন্ট ফাইলগুলি ``apps/web/fonts/``-এ বান্ডেল করা।
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import os
from typing import Any

from django.utils import timezone

from apps.customers.models import CustomerAssignment
from apps.transactions.models import (
    CUSTOMER_REQUIRED_TYPES, Transaction, TransactionStatus, TransactionType,
)
from apps.web.exports import (
    FONT_BOLD, FONT_REGULAR, PDF_CONTENT_TYPE,
    _clean_text, money, short_customer_id,
)

# ---------------------------------------------------------------------------
# বাংলা সংখ্যা → শব্দ (টাকা ও পয়সার জন্য)
# ---------------------------------------------------------------------------
_ONES_BN = ["", "এক", "দুই", "তিন", "চার", "পাঁচ",
            "ছয়", "সাত", "আট", "নয়"]
_TEENS_BN = ["দশ", "এগারো", "বারো", "তেরো", "চৌদ্দ", "পনেরো",
             "ষোলো", "সতেরো", "আঠারো", "ঊনিশ"]
_TENS_BN = ["", "", "বিশ", "ত্রিশ", "চল্লিশ",
            "পঞ্চাশ", "ষাট", "সত্তর", "আশি", "নব্বই"]


def _two_digit_bn(n: int) -> str:
    if n < 10:
        return _ONES_BN[n]
    if n < 20:
        return _TEENS_BN[n - 10]
    tens, ones = divmod(n, 10)
    word = _TENS_BN[tens]
    if ones:
        word = f"{word} {_ONES_BN[ones]}"
    return word


def _three_digit_bn(n: int) -> str:
    if n == 0:
        return ""
    word = ""
    hundreds, rest = divmod(n, 100)
    if hundreds:
        word += f"{_ONES_BN[hundreds]} শত"
    if rest:
        word += (" " if word else "") + _two_digit_bn(rest)
    return word


def _int_to_bengali_words(number: int) -> str:
    if number == 0:
        return "শূন্য"

    if number >= 10**7:  # এক কোটির ওপরে গাইড-আপ করবে (অত্যন্ত বিরল)
        return "এক কোটি বা তার বেশি"

    parts = []
    crore, rest = divmod(number, 10**7)
    if crore:
        parts.append(f"{_three_digit_bn(crore)} কোটি")
    lakh, rest = divmod(rest, 10**5)
    if lakh:
        parts.append(f"{_two_digit_bn(lakh)} লক্ষ")
    thousand, rest = divmod(rest, 1000)
    if thousand:
        parts.append(f"{_three_digit_bn(thousand)} হাজার")
    if rest:
        parts.append(_three_digit_bn(rest))
    return " ".join(parts)


def to_bengali_words(value: Decimal) -> str:
    """Decimal টাকা → কথায় ("তিন হাজার পাঁচশ টাকা")।"""
    try:
        value = Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception:
        value = Decimal("0.00")
    taka = int(value)
    paisa = int((value - Decimal(taka)) * 100)
    words = _int_to_bengali_words(taka) + " টাকা"
    if paisa:
        words += f" {_int_to_bengali_words(paisa)} পয়সা"
    return words


# ---------------------------------------------------------------------------
# সাধারণ হিসাব-সূত্র
# ---------------------------------------------------------------------------
def _logo_path(business) -> str:
    """একান্ত আপলোড করা থাকলে (এবং ফাইলটা থাকলে) লোগোর পথ ফেরত দেয়।"""
    logo = getattr(business, "logo", None)
    name = getattr(logo, "name", "") or ""
    if not name:
        return ""
    try:
        path = getattr(logo, "path", "") or ""
    except Exception:
        return ""
    return path if path and os.path.exists(path) else ""


_TYPE_LABEL_BN = {
    "CREDIT": "বাকি",
    "PAYMENT": "পরিশোধ",
    "ADVANCE": "অগ্রিম",
    "REFUND": "ফেরত",
    "ADJUSTMENT": "সংশোধন",
}
_TYPE_LABEL_EN = {
    "CREDIT": "Due",
    "PAYMENT": "Payment",
    "ADVANCE": "Advance",
    "REFUND": "Refund",
    "ADJUSTMENT": "Adjust",
}


def _txn_delta(txn) -> Decimal:
    """Positive = বাকি বাড়ে, negative = বাকি কমে।"""
    amount = txn.amount or Decimal("0.00")
    if txn.transaction_type == TransactionType.CREDIT:
        return amount
    if txn.transaction_type == TransactionType.ADJUSTMENT:
        direction = (txn.adjustment_direction or "").upper()
        return amount if direction == "UP" else -amount
    return -amount  # PAYMENT, ADVANCE, REFUND — বাকি কমে


def _signed_type(txn, lang: str) -> str:
    key = txn.transaction_type
    if lang == "en":
        return _TYPE_LABEL_EN.get(key, key)
    return _TYPE_LABEL_BN.get(key, key)


def _rows_for_customer(business, partner, customer):
    return list(Transaction.objects
                .filter(business=business, partner=partner,
                        customer=customer,
                        transaction_type__in=CUSTOMER_REQUIRED_TYPES)
                .exclude(status=TransactionStatus.REVERSED)
                .order_by("transaction_date", "created_at"))


def build_customer_statement(business, partner, customer,
                             lang: str = "bn") -> dict:
    """এক গ্রাহকের পুরো খাতা — PDF পাঠানোর জন্য প্রস্তুত ডেটা।"""
    lang = "en" if str(lang).lower().startswith("en") else "bn"
    txs = _rows_for_customer(business, partner, customer)

    rows = []
    running = Decimal("0.00")
    total_due = Decimal("0.00")
    total_paid = Decimal("0.00")
    for index, txn in enumerate(txs, start=1):
        delta = _txn_delta(txn)
        if delta > 0:
            total_due += delta
        else:
            total_paid += -delta
        running += delta
        rows.append({
            "sl": index,
            "date": txn.transaction_date.strftime("%d %b %Y"),
            "type": _signed_type(txn, lang),
            "note": _clean_text(txn.description),
            "amount": money(txn.amount),
            "running": running,
        })

    assignment = (CustomerAssignment.objects
                  .filter(partner=partner, customer=customer).first())
    current_due = assignment.current_due() if assignment else running

    phone = (getattr(customer, "phone", "") or "").strip()
    address = (getattr(customer, "address", "") or "").strip() \
        if hasattr(customer, "address") else ""
    now = timezone.localtime()
    return {
        "business": business.name,
        "business_phone": (getattr(business, "contact_phone", "") or "").strip(),
        "business_address": (getattr(business, "address", "") or "").strip(),
        "business_logo": _logo_path(business),
        "partner": partner.name,
        "customer_name": _clean_text(getattr(customer, "name", "")),
        "customer_phone": phone,
        "customer_id": short_customer_id(getattr(customer, "id", "")),
        "date_label": now.strftime("%d %b %Y"),
        "generated_label": now.strftime("%d %b %Y, %I:%M %p"),
        "rows": rows,
        "count": len(rows),
        "total_due": total_due,
        "total_due_display": money(total_due),
        "total_paid": total_paid,
        "total_paid_display": money(total_paid),
        "running": running,
        "running_display": money(running),
        "current_due": current_due,
        "current_due_display": money(current_due),
        "lang": lang,
        "en": lang == "en",
        "filename": f"baki-statement-{short_customer_id(customer.id)}",
    }


def build_money_receipt(business, partner, txn, lang: str = "bn") -> dict:
    """একটা PAYMENT লেনদেনের রশিকা — PDF পাঠানোর জন্য প্রস্তুত ডেটা।"""
    lang = "en" if str(lang).lower().startswith("en") else "bn"
    customer = getattr(txn, "customer", None)
    amount = txn.amount or Decimal("0.00")
    now = timezone.localtime()
    return {
        "business": business.name,
        "business_phone": (getattr(business, "contact_phone", "") or "").strip(),
        "business_address": (getattr(business, "address", "") or "").strip(),
        "business_logo": _logo_path(business),
        "partner": partner.name,
        "customer_name": _clean_text(getattr(customer, "name", "") or ""),
        "customer_id": short_customer_id(getattr(customer, "id", "") or ""),
        "date_label": txn.transaction_date.strftime("%d %b %Y"),
        "generated_label": now.strftime("%d %b %Y, %I:%M %p"),
        "amount": amount,
        "amount_display": money(amount),
        "amount_words": to_bengali_words(amount),
        "note": _clean_text(txn.description),
        "receipt_no": f"RCPT-{short_customer_id(txn.id)}",
        "lang": lang,
        "en": lang == "en",
        "filename": f"receipt-{short_customer_id(txn.id)}",
    }


# ---------------------------------------------------------------------------
# PDF rendering (fpdf2 + bundled Noto Bengali)
# ---------------------------------------------------------------------------
_NAVY = (11, 37, 69)
_GOLD = (201, 150, 27)
_CREAM = (244, 236, 221)
_MUTED = (148, 163, 184)
_INK = (15, 23, 42)
_SOFT = (247, 250, 253)
_LINE = (226, 232, 240)
_DANGER = (220, 38, 38)
_OK = (5, 150, 105)


def _header(pdf, title: str, subtitle: str, data: dict[str, Any] | None = None) -> None:
    data = data or {}
    pdf.set_fill_color(*_NAVY)
    pdf.rect(0, 0, pdf.w, 24, "F")
    pdf.set_fill_color(*_GOLD)
    pdf.rect(0, 24, pdf.w, 1.1, "F")

    # logo or brand mark
    logo = (data.get("business_logo") or "").strip()
    x = pdf.l_margin
    y = 6
    try:
        if logo and os.path.exists(logo):
            pdf.image(logo, x=x, y=y, h=12)
            x = x + 15
        else:
            pdf.set_fill_color(*_CREAM)
            pdf.rect(x, 6, 12, 12, "F")
            pdf.set_xy(x, 9.2)
            pdf.set_font("Noto", "B", 9)
            pdf.set_text_color(74, 52, 8)
            pdf.cell(12, 6, "৳", align="C")
            x = x + 16
    except Exception:
        pdf.set_fill_color(*_CREAM)
        pdf.rect(pdf.l_margin, 6, 12, 12, "F")
        pdf.set_xy(pdf.l_margin, 9.2)
        pdf.set_font("Noto", "B", 9)
        pdf.set_text_color(74, 52, 8)
        pdf.cell(12, 6, "৳", align="C")
        x = pdf.l_margin + 16

    pdf.set_xy(x, 5.4)
    pdf.set_font("Noto", "B", 15)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(120, 7.5, title)
    pdf.set_xy(x, 12.6)
    pdf.set_font("Noto", "", 8.5)
    pdf.set_text_color(216, 189, 138)
    pdf.cell(120, 5.5, subtitle)

    pdf.set_xy(pdf.w - pdf.r_margin - 110, 7)
    pdf.set_font("Noto", "", 7)
    pdf.set_text_color(163, 140, 100)
    pdf.cell(110, 4.5, "BUSINESS", align="R")
    pdf.set_xy(pdf.w - pdf.r_margin - 110, 12)
    pdf.set_font("Noto", "B", 9)
    pdf.set_text_color(244, 236, 221)
    pdf.cell(110, 6, (data.get("business") or ""), align="R")

    pdf.set_y(30)


def _footer(pdf, page_label: str) -> None:
    pdf.set_y(-13)
    pdf.set_draw_color(*_LINE)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.set_y(-10.5)
    pdf.set_font("Noto", "", 7.3)
    pdf.set_text_color(*_MUTED)
    pdf.cell(0, 5, "FinCore ERP থেকে স্বয়ংক্রিয়ভাবে তৈরি")
    pdf.set_xy(pdf.w - pdf.r_margin - 32, pdf.get_y())
    pdf.cell(32, 5, f"{page_label} {pdf.page_no()}", align="R")


def _business_strip(pdf, data: dict[str, Any]) -> None:
    """দুই-লাইনের ঠিকানা/ফোন স্ট্রিপ — optional business info."""
    lines = []
    if data.get("business_address"):
        lines.append(data["business_address"])
    if data.get("business_phone"):
        lines.append(f"ফোন: {data['business_phone']}")
    if not lines:
        return
    pdf.set_xy(pdf.l_margin, pdf.get_y() + 4)
    pdf.set_font("Noto", "", 7.8)
    pdf.set_text_color(*_MUTED)
    pdf.multi_cell(pdf.epw, 4.2, "\n".join(lines))
    pdf.set_y(pdf.get_y() + 1)


def _meta_boxes(pdf, items: list[tuple[str, str]]) -> None:
    """দুই-চারটি ছোট meta বাক্স (label + bold value)."""
    y = pdf.get_y()
    width = pdf.epw
    pdf.set_fill_color(*_SOFT)
    pdf.set_draw_color(*_LINE)
    pdf.rect(pdf.l_margin, y, width, 13, "DF")
    col = width / len(items)
    for index, (label, value) in enumerate(items):
        x = pdf.l_margin + index * col
        pdf.set_xy(x + 2, y + 2)
        pdf.set_font("Noto", "", 6.6)
        pdf.set_text_color(*_MUTED)
        pdf.cell(col - 4, 3.8, label)
        pdf.set_xy(x + 2, y + 6.2)
        pdf.set_font("Noto", "B", 8.4)
        pdf.set_text_color(*_INK)
        pdf.cell(col - 4, 5, str(value)[:34])
    pdf.set_y(y + 16)


def _table_header(pdf, widths: list[float], labels: list[str]) -> None:
    y = pdf.get_y()
    x = pdf.l_margin
    pdf.set_fill_color(*_NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Noto", "B", 8.2)
    for label, width in zip(labels, widths):
        pdf.set_xy(x, y)
        pdf.cell(width, 8.6, label, align="C", fill=True)
        x += width
    pdf.set_y(y + 8.6)


def _table_row(pdf, widths: list[float], cells: list[str],
               fill: bool, aligns: list[str] | None = None) -> None:
    y = pdf.get_y()
    x = pdf.l_margin
    aligns = aligns or ["L"] * len(cells)
    pdf.set_font("Noto", "", 7.8)
    if fill:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_text_color(*_INK)
    else:
        pdf.set_text_color(*_INK)
    for text, width, align in zip(cells, widths, aligns):
        pdf.set_xy(x, y)
        pdf.multi_cell(width, 5.4, str(text), align=align,
                       fill=fill)
        x += width
    pdf.set_y(max(pdf.get_y(), y + 6.6))


def _stat_box(pdf, label: str, value: str, color: tuple[int, int, int]) -> None:
    y = pdf.get_y()
    width = pdf.epw
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(*_LINE)
    pdf.rect(pdf.l_margin, y, width, 12, "DF")
    pdf.set_fill_color(*color)
    pdf.rect(pdf.l_margin, y, 3, 12, "F")
    pdf.set_xy(pdf.l_margin + 6, y + 1.8)
    pdf.set_font("Noto", "", 6.8)
    pdf.set_text_color(*_MUTED)
    pdf.cell(0, 4, label)
    pdf.set_xy(pdf.l_margin + 6, y + 6.2)
    pdf.set_font("Noto", "B", 11.5)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 5.2, value)
    pdf.set_y(y + 15)


def customer_statement_pdf(data: dict[str, Any]) -> bytes:
    """বাকি চালান / Customer statement — fpdf2 দিয়ে তৈরি একটা PDF."""
    from fpdf import FPDF

    en = data.get("en")
    title = "Customer Statement" if en else "বাকি চালান"
    labels = ["SL", "Date" if en else "তারিখ", "Type" if en else "ধরন",
              "Particular" if en else "নামে", "Amount" if en else "টাকা",
              "Balance" if en else "বাকি"]
    page_label = labels[0]

    class Statement(FPDF):
        def header(self):
            _header(self, title,
                    (data.get("partner") or "") + "  ·  " + data["date_label"],
                    data)

        def footer(self):
            _footer(self, "Page" if en else "পৃষ্ঠা")

    pdf = Statement(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(10, 10, 10)
    pdf.alias_nb_pages()
    pdf.add_font("Noto", "", str(FONT_REGULAR))
    pdf.add_font("Noto", "B", str(FONT_BOLD))
    pdf.set_text_shaping(True)
    pdf.set_title(f"{title} — {data['customer_name']}")
    pdf.set_author("FinCore ERP")
    pdf.add_page()

    _business_strip(pdf, data)
    _meta_boxes(pdf, [
        ("CUSTOMER" if en else "গ্রাহক", data["customer_name"] + " #" + data["customer_id"]),
        ("PHONE" if en else "ফোন", data["customer_phone"] or "—"),
        ("DATE" if en else "তারিখ", data["date_label"]),
        ("GENERATED" if en else "তৈরি", data["generated_label"]),
    ])

    pdf.set_y(pdf.get_y() + 2)
    widths = [12, 28, 26, 66, 29, 29]
    _table_header(pdf, widths, labels)
    for i, row in enumerate(data["rows"]):
        running = row["running"]
        balance = money(abs(running)) + ("" if running >= 0 else " (Cr)")
        cells = [
            str(row["sl"]),
            row["date"],
            row["type"],
            row["note"],
            row["amount"],
            balance,
        ]
        _table_row(pdf, widths, cells, fill=bool(i % 2),
                   aligns=["C", "L", "L", "L", "R", "R"])
    if not data["rows"]:
        pdf.set_font("Noto", "", 10)
        pdf.set_text_color(*_MUTED)
        pdf.cell(0, 12, "এই গ্রাহকের কোনো লেনদেন নেই।" if not en
                 else "No transactions for this customer.",
                 align="C")
        pdf.ln(6)

    pdf.set_y(pdf.get_y() + 4)
    _stat_box(pdf,
              ("Total due given" if en else "মোট বাকি দেওয়া"),
              data["total_due_display"], _DANGER)
    _stat_box(pdf,
              ("Total paid" if en else "মোট পরিশোধ"),
              data["total_paid_display"], _OK)
    overdue = data["current_due"] > Decimal("0.00")
    _stat_box(pdf,
              ("Current due" if en else "চলতি বাকি"),
              data["current_due_display"],
              _DANGER if overdue else _OK)

    # signature area
    pdf.set_y(-34)
    pdf.set_font("Noto", "", 9)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 6, ("Customer sign: ____________________" if en
                    else "গ্রাহকের স্বাক্ষর: ____________________"))
    pdf.set_xy(pdf.l_margin, pdf.get_y() + 8)
    pdf.cell(0, 6, ("Business sign: ____________________" if en
                    else "ব্যবসায়ীর স্বাক্ষর: ____________________"))

    out = pdf.output()
    return bytes(out)


def money_receipt_pdf(data: dict[str, Any]) -> bytes:
    """পরিশোধ রশিকা / Money receipt — fpdf2 দিয়ে একটা PDF."""
    from fpdf import FPDF

    en = data.get("en")
    title = "Money Receipt" if en else "পরিশোধ রশিকা"

    class Receipt(FPDF):
        def header(self):
            _header(self, title,
                    (data.get("partner") or ""), data)

        def footer(self):
            _footer(self, "Page" if en else "পৃষ্ঠা")

    pdf = Receipt(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(14, 10, 14)
    pdf.alias_nb_pages()
    pdf.add_font("Noto", "", str(FONT_REGULAR))
    pdf.add_font("Noto", "B", str(FONT_BOLD))
    pdf.set_text_shaping(True)
    pdf.set_title(f"Receipt {data['receipt_no']}")
    pdf.set_author("FinCore ERP")
    pdf.add_page()

    _business_strip(pdf, data)
    pdf.set_y(pdf.get_y() + 6)

    # BIG "RECEIPT" banner
    banner_y = pdf.get_y()
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(*_LINE)
    pdf.rect(pdf.l_margin, banner_y, pdf.epw, 26, "DF")
    pdf.set_xy(pdf.l_margin, banner_y + 4)
    pdf.set_font("Noto", "B", 20)
    pdf.set_text_color(*_NAVY)
    pdf.cell(pdf.epw, 9, title, align="C")
    pdf.set_xy(pdf.l_margin, banner_y + 15)
    pdf.set_font("Noto", "", 8)
    pdf.set_text_color(*_MUTED)
    pdf.cell(pdf.epw, 5,
             (f"Receipt No: {data['receipt_no']}" if en
              else f"রশিকা নং: {data['receipt_no']}"), align="C")
    pdf.set_y(banner_y + 32)

    def row(label, value, gap=7):
        pdf.set_xy(pdf.l_margin, pdf.get_y())
        pdf.set_font("Noto", "", 8.6)
        pdf.set_text_color(*_MUTED)
        pdf.cell(52, 6, label)
        pdf.set_font("Noto", "B", 9.5)
        pdf.set_text_color(*_INK)
        pdf.cell(pdf.epw - 52, 6, str(value))
        pdf.set_y(pdf.get_y() + gap)

    row(("Date" if en else "তারিখ"), data["date_label"], gap=9)
    row(("Received from" if en else "যার থেকে মিলল"),
        f"{data['customer_name']} (#{data['customer_id']})", gap=9)
    row(("Particulars" if en else "বাবদ"),
        ("Payment against baki dues" if en else "বাকি পরিশোধ"), gap=9)
    row(("Note" if en else "বিবরণ"), data["note"], gap=9)

    # amount figure + words
    pdf.set_y(pdf.get_y() + 2)
    pdf.set_fill_color(255, 255, 255)
    pdf.rect(pdf.l_margin, pdf.get_y(), pdf.epw, 30, "DF")
    y = pdf.get_y()
    pdf.set_xy(pdf.l_margin, y + 3)
    pdf.set_font("Noto", "", 7.6)
    pdf.set_text_color(*_MUTED)
    pdf.cell(pdf.epw, 4,
             ("Amount in figures" if en else "টাকা (অঙ্কে)"), align="C")
    pdf.set_xy(pdf.l_margin, y + 8)
    pdf.set_font("Noto", "B", 24)
    pdf.set_text_color(*_GOLD)
    pdf.cell(pdf.epw, 10, data["amount_display"], align="C")
    pdf.set_xy(pdf.l_margin, y + 20)
    pdf.set_font("Noto", "", 8.6)
    pdf.set_text_color(*_INK)
    pdf.cell(pdf.epw, 5,
             ("in words: " + data["amount_words"]) if en
             else ("কথায়: " + data["amount_words"]), align="C")
    pdf.set_y(y + 36)

    # thank-you + signature
    pdf.set_xy(pdf.l_margin, pdf.get_y() + 2)
    pdf.set_font("Noto", "", 9)
    pdf.set_text_color(*_INK)
    pdf.cell(pdf.epw, 5,
             ("Thank you for your payment." if en else "ধন্যবাদ।"),
             align="C")

    pdf.set_y(-36)
    pdf.set_font("Noto", "", 9)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 6,
             ("Customer sign: ____________________" if en
              else "গ্রাহকের স্বাক্ষর: ____________________"))
    pdf.set_xy(pdf.l_margin, pdf.get_y() + 9)
    pdf.cell(0, 6,
             ("Authorised sign & stamp: ____________" if en
              else "অনুমোদিত স্বাক্ষর ও সিল: __________"))

    out = pdf.output()
    return bytes(out)
