"""Export helpers for the partner-wise baki khata.

The exports are generated from the same ledger rows the web UI shows:
non-reversed CREDIT transactions for one partner on one date, grouped by
customer.  ``current_due`` is the live assignment balance, so it reflects
payments made on any later date.

Supported formats:
    * PDF  — fpdf2 with bundled Noto Sans Bengali (proper Bangla shaping)
    * XLSX — openpyxl
    * DOCX — python-docx
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from django.utils import timezone

from apps.customers.models import CustomerAssignment
from apps.transactions.models import (
    ExpenseKind, PaymentMethod, Transaction, TransactionStatus, TransactionType,
)

logger = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_REGULAR = FONT_DIR / "NotoSansBengaliLatin-Regular.ttf"
FONT_BOLD = FONT_DIR / "NotoSansBengaliLatin-Bold.ttf"

PDF_CONTENT_TYPE = "application/pdf"
XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

LABELS: dict[str, dict[str, str]] = {
    "bn": {
        "title": "বাকির খাতা",
        "partner_book": "পার্টনার ও বাকি",
        "business": "ব্যবসা",
        "partner": "পার্টনার",
        "date": "তারিখ",
        "generated": "তৈরি",
        "names": "নাম",
        "sl": "ক্রমিক",
        "id": "আইডি",
        "name": "নাম",
        "contact": "ফোন / ইমেইল",
        "previous": "আগের বাকি",
        "current": "বর্তমান বাকি",
        "note": "বিবরণ",
        "total_previous": "মোট আগের বাকি",
        "total_current": "মোট বর্তমান বাকি",
        "empty": "এই তারিখে কোনো বাকি নেই।",
        "footer": "FinCore ERP থেকে স্বয়ংক্রিয়ভাবে তৈরি",
        "amount_note": "সব টাকা বাংলাদেশি টাকায় (৳)",
        "page": "পৃষ্ঠা",
        "sheet": "বাকির খাতা",
        "total": "মোট",
    },
    "en": {
        "title": "Baki Khata",
        "partner_book": "Partner & Dues",
        "business": "Business",
        "partner": "Partner",
        "date": "Date",
        "generated": "Generated",
        "names": "Names",
        "sl": "SL",
        "id": "ID",
        "name": "Name",
        "contact": "Phone / Email",
        "previous": "Previous baki",
        "current": "Current baki",
        "note": "Note",
        "total_previous": "Total previous baki",
        "total_current": "Total current baki",
        "empty": "No baki on this date.",
        "footer": "Generated automatically by FinCore ERP",
        "amount_note": "All amounts are in Bangladeshi Taka (৳)",
        "page": "Page",
        "sheet": "Baki Khata",
        "total": "Total",
    },
}


def short_customer_id(customer_id: Any) -> str:
    """First eight characters of the UUID, matching the web table."""
    return str(customer_id or "").replace("-", "")[:8].upper() or "—"


def money(value: Any) -> str:
    """Format a Decimal as ৳1,234.56 (two decimal places)."""
    try:
        amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except Exception:
        amount = Decimal("0.00")
    return f"৳{amount:,.2f}"


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "—"


def build_khata_export(
    business,
    partner,
    pdate: date,
    lang: str = "bn",
) -> dict[str, Any]:
    """Build the same day-khata rows as ``customers_view`` for export."""
    lang = "en" if str(lang).lower().startswith("en") else "bn"
    labels = LABELS[lang]

    tx_rows = list(
        Transaction.objects
        .filter(
            business=business,
            partner=partner,
            transaction_type=TransactionType.CREDIT,
            transaction_date=pdate,
        )
        .exclude(status=TransactionStatus.REVERSED)
        .select_related("customer")
        .order_by("created_at")
    )

    grouped: dict[str, dict[str, Any]] = {}
    for tx in tx_rows:
        cid = str(tx.customer_id)
        bucket = grouped.get(cid)
        if bucket is None:
            bucket = {
                "customer": tx.customer,
                "amount": Decimal("0.00"),
                "tx_ids": [],
                "last_tx": tx,
                "first_at": tx.created_at,
                "note": "",
            }
            grouped[cid] = bucket
        bucket["amount"] += tx.amount
        bucket["tx_ids"].append(tx.id)
        if tx.created_at < bucket["first_at"]:
            bucket["first_at"] = tx.created_at
        if tx.created_at >= bucket["last_tx"].created_at:
            bucket["last_tx"] = tx
        if (tx.description or "").strip():
            bucket["note"] = tx.description.strip()

    cust_ids = [g["customer"].id for g in grouped.values() if g["customer"]]
    assignments = {
        a.customer_id: a
        for a in CustomerAssignment.objects
        .filter(partner=partner, customer_id__in=cust_ids)
        .prefetch_related("due_entries")
    }

    ordered = sorted(grouped.values(), key=lambda g: g["first_at"])
    rows: list[dict[str, Any]] = []
    total_previous = Decimal("0.00")
    total_current = Decimal("0.00")
    for index, bucket in enumerate(ordered, start=1):
        customer = bucket["customer"]
        assignment = assignments.get(getattr(customer, "id", None))
        previous = Decimal(str(bucket["amount"]))
        current = assignment.current_due() if assignment else Decimal("0.00")
        total_previous += previous
        total_current += current
        contact_parts = []
        if customer is not None:
            if getattr(customer, "phone", ""):
                contact_parts.append(customer.phone)
            if getattr(customer, "email", ""):
                contact_parts.append(customer.email)
        rows.append({
            "sl": index,
            "id": short_customer_id(getattr(customer, "id", "")),
            "name": _clean_text(getattr(customer, "name", "")),
            "contact": "\n".join(contact_parts) if contact_parts else "—",
            "previous": previous,
            "previous_display": money(previous),
            "current": current,
            "current_display": money(current),
            "note": _clean_text(bucket["note"]),
            "tx_count": len(bucket["tx_ids"]),
        })

    now = timezone.localtime()
    date_label = pdate.strftime("%d %b %Y")
    generated_label = now.strftime("%d %b %Y, %I:%M %p")
    return {
        "business": business.name,
        "partner": partner.name,
        "pdate": pdate,
        "date_label": date_label,
        "generated_label": generated_label,
        "title": labels["title"],
        "subtitle": f"{partner.name}  ·  {date_label}",
        "rows": rows,
        "count": len(rows),
        "total_previous": total_previous,
        "total_current": total_current,
        "total_previous_display": money(total_previous),
        "total_current_display": money(total_current),
        "labels": labels,
        "lang": lang,
        "filename": f"baki-khata-{str(partner.id)[:8]}-{pdate.isoformat()}",
    }


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

class _KhataPDF:
    """Small wrapper that keeps the fpdf2 import lazy."""

    def __new__(cls, data: dict[str, Any]):
        from fpdf import FPDF

        labels = data["labels"]

        class KhataPDF(FPDF):
            def __init__(self):
                super().__init__(orientation="L", unit="mm", format="A4")
                self.set_auto_page_break(auto=True, margin=17)
                self.set_margins(10, 10, 10)
                self.alias_nb_pages()
                self.add_font("Noto", "", str(FONT_REGULAR))
                self.add_font("Noto", "B", str(FONT_BOLD))
                self.set_text_shaping(True)
                self.set_title(f"{data['title']} — {data['partner']}")
                self.set_author("FinCore ERP")
                self.set_subject(f"{data['title']} {data['date_label']}")
                self.set_creator("FinCore ERP")

            def header(self):
                # Premium amber banner with a gold accent bar.
                self.set_fill_color(28, 18, 5)
                self.rect(0, 0, self.w, 26, "F")
                self.set_fill_color(47, 36, 16)
                self.rect(0, 0, self.w, 14, "F")
                self.set_fill_color(201, 150, 27)
                self.rect(0, 26, self.w, 1.4, "F")

                # Brand mark
                self.set_fill_color(253, 230, 138)
                self.rect(self.l_margin, 6, 12, 12, "F")
                self.set_xy(self.l_margin, 9.2)
                self.set_font("Noto", "B", 10)
                self.set_text_color(74, 52, 8)
                self.cell(12, 6, "৳", align="C")

                self.set_xy(self.l_margin + 16, 5.4)
                self.set_font("Noto", "B", 17)
                self.set_text_color(255, 255, 255)
                self.cell(130, 8, data["title"])

                self.set_xy(self.l_margin + 16, 13.4)
                self.set_font("Noto", "", 9.5)
                self.set_text_color(216, 189, 138)
                self.cell(130, 6, data["subtitle"])

                self.set_xy(self.w - self.r_margin - 110, 8)
                self.set_font("Noto", "", 7.6)
                self.set_text_color(163, 140, 100)
                self.cell(110, 5, "BUSINESS", align="R")
                self.set_xy(self.w - self.r_margin - 110, 13)
                self.set_font("Noto", "B", 10)
                self.set_text_color(244, 236, 221)
                self.cell(110, 6, data["business"], align="R")

                self.set_y(32)

            def footer(self):
                self.set_y(-12.5)
                self.set_draw_color(226, 232, 240)
                self.line(self.l_margin, self.get_y(), self.w - self.r_margin,
                          self.get_y())
                self.set_y(-10)
                self.set_font("Noto", "", 7.3)
                self.set_text_color(100, 116, 139)
                self.cell(0, 5, f"{labels['footer']}  ·  {data['generated_label']}")
                self.set_xy(self.w - self.r_margin - 32, self.get_y())
                self.cell(32, 5, f"{labels['page']} {self.page_no()}", align="R")

        return KhataPDF()


def _pdf_text_height(pdf, width: float, text: str, style: str = "",
                     size: float = 8.2, line: float = 4.0) -> float:
    pdf.set_font("Noto", style, size)
    return float(pdf.multi_cell(width, line, text, dry_run=True, output="HEIGHT"))


def _pdf_table_header(pdf, data: dict[str, Any], widths: list[float]) -> None:
    labels = data["labels"]
    y = pdf.get_y()
    x = pdf.l_margin
    pdf.set_fill_color(11, 37, 69)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Noto", "B", 8.3)
    for label, width in zip(
        [labels["sl"], labels["id"], labels["name"], labels["contact"],
         labels["previous"], labels["current"], labels["note"]],
        widths,
    ):
        pdf.set_xy(x, y)
        pdf.cell(width, 9.5, label, align="C", fill=True)
        x += width
    pdf.set_draw_color(11, 37, 69)
    pdf.rect(pdf.l_margin, y, sum(widths), 9.5)
    x = pdf.l_margin
    for width in widths[:-1]:
        x += width
        pdf.line(x, y, x, y + 9.5)
    pdf.set_y(y + 9.5)


def _pdf_meta(pdf, data: dict[str, Any]) -> None:
    """Meta cards + premium stat strip."""
    labels = data["labels"]
    y = pdf.get_y()
    width = pdf.epw
    pdf.set_fill_color(247, 250, 253)
    pdf.set_draw_color(230, 237, 245)
    pdf.rect(pdf.l_margin, y, width, 13, "DF")

    columns = [
        (labels["business"], data["business"], 82),
        (labels["partner"], data["partner"], 62),
        (labels["date"], data["date_label"], 42),
        (labels["generated"], data["generated_label"], width - 192),
    ]
    x = pdf.l_margin + 3
    for label, value, col_width in columns:
        pdf.set_xy(x, y + 2)
        pdf.set_font("Noto", "", 6.8)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(col_width - 4, 4, label)
        pdf.set_xy(x, y + 6.2)
        pdf.set_font("Noto", "B", 8.6)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(col_width - 4, 5, str(value)[:60])
        x += col_width
    pdf.set_y(y + 16)

    # ---- Stat strip (names / total previous / total current) ----
    y = pdf.get_y() + 1
    box_w = 62.0
    gap = 4.0
    total_w = box_w * 3 + gap * 2
    x = pdf.w - pdf.r_margin - total_w

    stat_items = [
        (labels["names"], str(data["count"]), (56, 189, 248)),
        (labels["total_previous"], money(data["total_previous"]), (201, 150, 27)),
        (labels["total_current"], money(data["total_current"]),
         (185, 28, 28) if data["total_current"] > 0 else (4, 120, 87)),
    ]
    for key, value, color in stat_items:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(226, 232, 240)
        pdf.rect(x, y, box_w, 15, "DF")
        # Coloured left edge
        pdf.set_fill_color(*color)
        pdf.rect(x, y, 1.6, 15, "F")
        pdf.set_xy(x + 4, y + 2.4)
        pdf.set_font("Noto", "", 7)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(box_w - 8, 4, key.upper())
        pdf.set_xy(x + 4, y + 6.6)
        pdf.set_font("Noto", "B", 11)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(box_w - 8, 6, value, align="R")
        x += box_w + gap
    pdf.set_y(y + 18)


def _pdf_row(pdf, data: dict[str, Any], row: dict[str, Any],
             widths: list[float], index: int) -> None:
    name_h = _pdf_text_height(pdf, widths[2] - 3, row["name"], "B", 8.4, 4.1)
    contact_h = _pdf_text_height(pdf, widths[3] - 3, row["contact"], "", 7.3, 3.6)
    note_h = _pdf_text_height(pdf, widths[6] - 3, row["note"], "", 7.3, 3.6)
    row_h = max(8.6, name_h + 3.2, contact_h + 3.2, note_h + 3.2)
    if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
        pdf.add_page()
        _pdf_table_header(pdf, data, widths)

    y = pdf.get_y()
    if index % 2 == 0:
        pdf.set_fill_color(248, 250, 252)
        pdf.rect(pdf.l_margin, y, sum(widths), row_h, "F")
    pdf.set_draw_color(226, 232, 240)
    pdf.rect(pdf.l_margin, y, sum(widths), row_h)
    x = pdf.l_margin
    for width in widths[:-1]:
        x += width
        pdf.line(x, y, x, y + row_h)

    x = pdf.l_margin
    pdf.set_text_color(71, 85, 105)
    pdf.set_font("Noto", "", 7.8)
    pdf.set_xy(x, y + 1.6)
    pdf.cell(widths[0], 4.6, str(row["sl"]), align="C")
    x += widths[0]

    pdf.set_font("Noto", "", 7.3)
    pdf.set_xy(x, y + 1.6)
    pdf.cell(widths[1], 4.6, row["id"], align="C")
    x += widths[1]

    pdf.set_text_color(15, 23, 42)
    pdf.set_font("Noto", "B", 8.4)
    pdf.set_xy(x, y + 1.4)
    pdf.multi_cell(widths[2] - 3, 4.1, row["name"], align="L")
    x += widths[2]

    pdf.set_font("Noto", "", 7.3)
    pdf.set_text_color(71, 85, 105)
    pdf.set_xy(x, y + 1.5)
    pdf.multi_cell(widths[3] - 3, 3.6, row["contact"], align="L")
    x += widths[3]

    pdf.set_font("Noto", "", 8.2)
    pdf.set_text_color(15, 23, 42)
    pdf.set_xy(x, y + 1.6)
    pdf.cell(widths[4] - 2, 4.6, money(row["previous"]), align="R")
    x += widths[4]

    current_color = (185, 28, 28) if row["current"] > 0 else (4, 120, 87)
    pdf.set_font("Noto", "B", 8.2)
    pdf.set_text_color(*current_color)
    pdf.set_xy(x, y + 1.6)
    pdf.cell(widths[5] - 2, 4.6, money(row["current"]), align="R")
    x += widths[5]

    pdf.set_font("Noto", "", 7.3)
    pdf.set_text_color(71, 85, 105)
    pdf.set_xy(x, y + 1.5)
    pdf.multi_cell(widths[6] - 3, 3.6, row["note"], align="L")
    pdf.set_y(y + row_h)


def _pdf_totals(pdf, data: dict[str, Any]) -> None:
    labels = data["labels"]
    y = pdf.get_y() + 6
    box_w = 66
    gap = 4
    total_w = box_w * 2 + gap
    if y + 20 > pdf.h - pdf.b_margin:
        pdf.add_page()
        y = pdf.get_y() + 2
    x = pdf.w - pdf.r_margin - total_w

    pdf.set_fill_color(253, 243, 214)
    pdf.set_draw_color(253, 230, 138)
    pdf.rect(x, y, box_w, 17, "DF")
    pdf.set_xy(x + 4, y + 2.5)
    pdf.set_font("Noto", "", 7.3)
    pdf.set_text_color(146, 88, 11)
    pdf.cell(10, 4, labels["total"])
    pdf.cell(box_w - 14, 4, labels["previous"])
    pdf.set_xy(x + 4, y + 7)
    pdf.set_font("Noto", "B", 11.5)
    pdf.set_text_color(15, 23, 42)
    pdf.cell(box_w - 8, 7, money(data["total_previous"]), align="R")

    x2 = x + box_w + gap
    pdf.set_fill_color(254, 242, 242)
    pdf.set_draw_color(254, 202, 202)
    pdf.rect(x2, y, box_w, 17, "DF")
    pdf.set_xy(x2 + 4, y + 2.5)
    pdf.set_font("Noto", "", 7.3)
    pdf.set_text_color(185, 28, 28)
    pdf.cell(10, 4, labels["total"])
    pdf.cell(box_w - 14, 4, labels["current"])
    pdf.set_xy(x2 + 4, y + 7)
    pdf.set_font("Noto", "B", 11.5)
    current_color = (185, 28, 28) if data["total_current"] > 0 else (4, 120, 87)
    pdf.set_text_color(*current_color)
    pdf.cell(box_w - 8, 7, money(data["total_current"]), align="R")

    pdf.set_xy(pdf.l_margin, y + 1.5)
    pdf.set_font("Noto", "", 8)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(total_w - 10, 4, f"{labels['names']}: {data['count']}")
    pdf.set_xy(pdf.l_margin, y + 6.5)
    pdf.cell(total_w - 10, 4, labels["amount_note"])
    pdf.set_y(y + 21)


def _pdf_empty(pdf, data: dict[str, Any]) -> None:
    y = pdf.get_y() + 8
    pdf.set_fill_color(248, 250, 252)
    pdf.set_draw_color(226, 232, 240)
    pdf.rect(pdf.l_margin, y, pdf.epw, 24, "DF")
    pdf.set_xy(pdf.l_margin, y + 8)
    pdf.set_font("Noto", "", 11)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(pdf.epw, 8, data["labels"]["empty"], align="C")
    pdf.set_y(y + 28)


def _find_pdf_browser() -> str | None:
    """Locate a Chromium-based browser for high-quality Bangla PDF output."""
    env_path = (os.environ.get("FINCORE_PDF_BROWSER") or "").strip()
    candidates: list[str] = [env_path] if env_path else []
    for name in (
        "msedge", "microsoft-edge", "microsoft-edge-stable",
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if os.name == "nt":
        candidates += [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    else:
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
        ]
    for candidate in candidates:
        try:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate))
        except OSError:
            continue
    return None


def _render_pdf_with_browser(
    data: dict[str, Any],
    template_name: str = "web/khata_export_pdf.html",
    prefix: str = "fincore-khata-",
) -> bytes | None:
    """Render the styled HTML transcript with headless Chrome/Edge.

    Chromium performs full OpenType shaping for Bangla, so the PDF looks
    identical to the web page.  Returns None when no browser is available or
    rendering fails, in which case the fpdf2 renderer is used.
    """
    browser = _find_pdf_browser()
    if not browser:
        return None
    try:
        from django.template.loader import render_to_string

        html = render_to_string(template_name, {"data": data})
    except Exception:
        logger.warning("Could not render PDF HTML (%s)", template_name,
                       exc_info=True)
        return None

    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
            tmp_path = Path(tmp)
            html_path = tmp_path / "report.html"
            pdf_path = tmp_path / "report.pdf"
            html_path.write_text(html, encoding="utf-8")
            for source in (FONT_REGULAR, FONT_BOLD):
                if source.is_file():
                    shutil.copy2(source, tmp_path / source.name)
            profile_path = tmp_path / "profile"
            profile_path.mkdir()

            common_args = [
                browser,
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--allow-file-access-from-files",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=2500",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile_path}",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ]
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
                common_args.insert(1, "--no-sandbox")

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            last_error = ""
            for headless in ("--headless=new", "--headless"):
                args = [common_args[0], headless] + common_args[1:]
                try:
                    result = subprocess.run(
                        args,
                        capture_output=True,
                        timeout=45,
                        check=False,
                        creationflags=creationflags,
                    )
                    last_error = (result.stderr or b"").decode("utf-8", "replace")
                except Exception as exc:
                    last_error = str(exc)
                if pdf_path.is_file() and pdf_path.stat().st_size > 1000:
                    return pdf_path.read_bytes()
            logger.warning(
                "Headless browser PDF rendering produced no file (%s): %s",
                browser, last_error[-800:],
            )
            return None
    except Exception:
        logger.warning("Headless browser PDF rendering failed", exc_info=True)
        return None


def export_khata_pdf(data: dict[str, Any]) -> bytes:
    browser_pdf = _render_pdf_with_browser(data)
    if browser_pdf:
        return browser_pdf

    # Portable fallback: fpdf2 with bundled Noto Sans Bengali.
    pdf = _KhataPDF(data)
    pdf.add_page()
    _pdf_meta(pdf, data)
    if not data["rows"]:
        _pdf_empty(pdf, data)
    else:
        widths = [10, 25, 61, 54, 31, 31, 65]
        _pdf_table_header(pdf, data, widths)
        for index, row in enumerate(data["rows"], start=1):
            _pdf_row(pdf, data, row, widths, index)
        _pdf_totals(pdf, data)
    output = pdf.output()
    return bytes(output)


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def export_khata_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    labels = data["labels"]
    wb = Workbook()
    ws = wb.active
    ws.title = labels["sheet"][:31]
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 90
    ws.sheet_properties.tabColor = "C9961B"

    navy = "0B2545"
    navy_dark = "1C1205"
    gold = "C9961B"
    gold_brand = "C9961B"
    ink = "0B1220"
    muted = "64748B"
    line = "E6EDF5"
    soft = "F8FAFC"
    gold_soft = "FDF3D6"
    red = "B91C1C"
    green = "047857"
    white = "FFFFFF"

    thin = Side(style="thin", color=line)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    base_font = Font(name="Nirmala UI", size=10, color=ink)
    header_font = Font(name="Nirmala UI", size=9.5, bold=True, color=white)
    title_font = Font(name="Nirmala UI", size=20, bold=True, color=white)
    brand_font = Font(name="Nirmala UI", size=10, bold=True, color="FDE68A")
    sub_font = Font(name="Nirmala UI", size=11, bold=True, color=gold)
    meta_font = Font(name="Nirmala UI", size=9, color=muted)
    bold_font = Font(name="Nirmala UI", size=10, bold=True, color=ink)
    stat_key_font = Font(name="Nirmala UI", size=8, bold=True, color=muted)
    stat_val_font = Font(name="Nirmala UI", size=13, bold=True, color=ink)
    header_fill = PatternFill("solid", fgColor=navy)
    soft_fill = PatternFill("solid", fgColor=soft)
    gold_fill = PatternFill("solid", fgColor=gold_soft)

    widths = [6, 14, 30, 30, 17, 17, 42]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width

    # ---- Premium banner ----
    ws.merge_cells("A1:G2")
    ws["A1"] = f"{data['title']} — {data['partner']}"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws["A1"].fill = PatternFill("solid", fgColor=navy_dark)
    for row in (1, 2):
        for col in range(1, 8):
            ws.cell(row=row, column=col).fill = PatternFill("solid", fgColor=navy_dark)
    ws.row_dimensions[1].height = 26
    ws.row_dimensions[2].height = 18

    ws.merge_cells("A3:G3")
    ws["A3"] = f"{data['date_label']}   ·   {data['business']}   ·   {labels['partner_book']}"
    ws["A3"].font = sub_font
    ws["A3"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[3].height = 20

    ws.merge_cells("A4:G4")
    ws["A4"] = f"{labels['generated']}: {data['generated_label']}   ·   {labels['amount_note']}"
    ws["A4"].font = meta_font
    ws["A4"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[4].height = 16

    # ---- Stat strip ----
    stat_row = 5
    stat_cells = [
        (1, 2, labels["names"], str(data["count"]), navy),
        (3, 4, labels["total_previous"], data["total_previous_display"], gold),
        (5, 7, labels["total_current"], data["total_current_display"],
         red if data["total_current"] > 0 else green),
    ]
    for col_start, col_end, key, value, value_color in stat_cells:
        ws.merge_cells(start_row=stat_row, start_column=col_start,
                       end_row=stat_row, end_column=col_end)
        key_cell = ws.cell(row=stat_row, column=col_start, value=key.upper())
        key_cell.font = stat_key_font
        key_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.merge_cells(start_row=stat_row + 1, start_column=col_start,
                       end_row=stat_row + 1, end_column=col_end)
        val_cell = ws.cell(row=stat_row + 1, column=col_start, value=value)
        val_cell.font = Font(name="Nirmala UI", size=13, bold=True, color=value_color)
        val_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        for r in (stat_row, stat_row + 1):
            for c in range(col_start, col_end + 1):
                ws.cell(row=r, column=c).fill = PatternFill(
                    "solid", fgColor="F7FAFD")
                ws.cell(row=r, column=c).border = Border(
                    left=thin, right=thin, top=thin, bottom=thin)
    ws.row_dimensions[stat_row].height = 15
    ws.row_dimensions[stat_row + 1].height = 22

    header_row = stat_row + 3
    headers = [
        labels["sl"], labels["id"], labels["name"], labels["contact"],
        f"{labels['previous']} (৳)", f"{labels['current']} (৳)", labels["note"],
    ]
    for col, value in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col, value=value)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(
            horizontal="right" if col in (5, 6) else
            ("center" if col in (1, 2) else "left"),
            vertical="center", wrap_text=True, indent=0 if col in (1, 2, 5, 6) else 1,
        )
    ws.row_dimensions[header_row].height = 30

    first_data_row = header_row + 1
    for offset, row in enumerate(data["rows"]):
        r = first_data_row + offset
        values = [
            row["sl"], row["id"], row["name"], row["contact"],
            float(row["previous"]), float(row["current"]), row["note"],
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=col, value=value)
            cell.font = bold_font if col == 3 else base_font
            cell.border = border
            if col in (1, 2):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif col in (5, 6):
                cell.alignment = Alignment(horizontal="right", vertical="center")
                cell.number_format = "#,##0.00"
                if col == 6:
                    cell.font = Font(
                        name="Nirmala UI", size=10, bold=True,
                        color=red if row["current"] > 0 else green,
                    )
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center",
                                           wrap_text=True, indent=1)
            if offset % 2 == 1:
                cell.fill = soft_fill
        lines = max(
            1,
            len(row["name"]) // 28 + 1,
            len(row["contact"]) // 28 + 1,
            len(row["note"]) // 42 + 1,
        )
        ws.row_dimensions[r].height = max(22, 15 * lines)

    last_row = first_data_row + len(data["rows"]) - 1
    total_row = max(last_row + 1, first_data_row)
    if data["rows"]:
        ws.merge_cells(start_row=total_row, start_column=1,
                       end_row=total_row, end_column=4)
        total_label = ws.cell(row=total_row, column=1, value=labels["total"])
        total_label.font = bold_font
        total_label.alignment = Alignment(horizontal="right", vertical="center")
        for col in range(1, 8):
            cell = ws.cell(row=total_row, column=col)
            cell.fill = gold_fill
            cell.border = Border(
                left=thin, right=thin,
                top=Side(style="double", color=gold_brand), bottom=thin,
            )
        prev_cell = ws.cell(row=total_row, column=5,
                            value=float(data["total_previous"]))
        curr_cell = ws.cell(row=total_row, column=6,
                            value=float(data["total_current"]))
        for cell in (prev_cell, curr_cell):
            cell.font = Font(name="Nirmala UI", size=11, bold=True, color=ink)
            cell.number_format = "#,##0.00"
            cell.alignment = Alignment(horizontal="right", vertical="center")
        curr_cell.font = Font(
            name="Nirmala UI", size=11, bold=True,
            color=red if data["total_current"] > 0 else green,
        )
        ws.row_dimensions[total_row].height = 26
        ws.auto_filter.ref = f"A{header_row}:G{last_row}"
        ws.freeze_panes = f"A{first_data_row}"
        ws.print_title_rows = f"{header_row}:{header_row}"

    ws.print_area = f"A1:G{max(total_row, header_row)}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    from openpyxl.worksheet.properties import PageSetupProperties
    if ws.sheet_properties.pageSetUpPr is None:
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    else:
        ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = 0.3
    ws.page_margins.right = 0.3
    ws.page_margins.top = 0.45
    ws.page_margins.bottom = 0.45
    ws.oddFooter.left.text = "FinCore ERP"
    ws.oddFooter.left.size = 8
    ws.oddFooter.left.color = muted
    ws.oddFooter.center.text = f"{labels['footer']}  ·  {data['generated_label']}"
    ws.oddFooter.center.size = 8
    ws.oddFooter.center.color = muted
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.right.size = 8
    ws.oddFooter.right.color = muted

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------

def _docx_set_cell_shading(cell, color: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), color)
    tc_pr.append(shading)


def _docx_set_repeat_header(row) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


def _docx_style_run(run, size: float, bold: bool = False,
                    color: str | None = None) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    run.font.name = "Nirmala UI"
    run.font.size = Pt(size)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    r_pr = run._element.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.append(r_fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        r_fonts.set(qn(attr), "Nirmala UI")


def _docx_add_page_number(paragraph) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    begin_run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    begin_run._r.append(begin)

    instr_run = paragraph.add_run()
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    instr_run._r.append(instr)

    sep_run = paragraph.add_run()
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    sep_run._r.append(sep)

    number_run = paragraph.add_run("1")
    _docx_style_run(number_run, 7.5, False, "64748B")

    end_run = paragraph.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run._r.append(end)


def _docx_shade_paragraph(paragraph, color: str) -> None:
    """Give a paragraph a solid background (used for the banner)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    p_pr = paragraph._p.get_or_add_pPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), color)
    p_pr.append(shading)


def _docx_add_horizontal_rule(paragraph, color: str, size: int = 18) -> None:
    """Draw a coloured bottom border under a paragraph (accent rule)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    borders.append(bottom)
    p_pr.append(borders)


def export_khata_docx(data: dict[str, Any]) -> bytes:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    labels = data["labels"]
    navy = "0B2545"
    navy_dark = "1C1205"
    gold = "C9961B"
    gold_brand = "C9961B"
    ink = "0B1220"
    muted = "64748B"
    soft = "F7FAFD"
    gold_soft = "FDF3D6"
    red = "B91C1C"
    green = "047857"

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Cm(29.7)
    section.page_height = Cm(21.0)
    section.left_margin = Cm(1.1)
    section.right_margin = Cm(1.1)
    section.top_margin = Cm(0.9)
    section.bottom_margin = Cm(1.0)

    # ---------- Premium banner ----------
    banner = doc.add_paragraph()
    banner.paragraph_format.space_after = Pt(0)
    banner.paragraph_format.space_before = Pt(0)
    banner.paragraph_format.left_indent = Cm(0.2)
    banner.paragraph_format.right_indent = Cm(0.2)
    _docx_shade_paragraph(banner, navy_dark)
    _docx_style_run(banner.add_run(f"  {data['title']}  —  {data['partner']}"),
                    20, True, "FFFFFF")
    _docx_add_horizontal_rule(banner, gold_brand, 24)
    banner.paragraph_format.space_after = Pt(6)

    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_before = Pt(0)
    subtitle.paragraph_format.space_after = Pt(0)
    subtitle.paragraph_format.left_indent = Cm(0.2)
    _docx_style_run(
        subtitle.add_run(
            f"{data['date_label']}   ·   {data['business']}   ·   "
            f"{labels['partner_book']}"),
        11, True, gold,
    )

    meta = doc.add_paragraph()
    meta.paragraph_format.space_before = Pt(3)
    meta.paragraph_format.space_after = Pt(8)
    meta.paragraph_format.left_indent = Cm(0.2)
    _docx_style_run(
        meta.add_run(f"{labels['generated']}: {data['generated_label']}   ·   "
                     f"{labels['amount_note']}"),
        8.5, False, muted,
    )

    # ---------- Stat strip ----------
    stat_table = doc.add_table(rows=1, cols=3)
    stat_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    stat_table.autofit = False
    stat_table.allow_autofit = False
    stat_widths = [Cm(9.0), Cm(9.0), Cm(9.0)]
    stat_items = [
        (labels["names"], str(data["count"]), navy),
        (labels["total_previous"], data["total_previous_display"], gold),
        (labels["total_current"], data["total_current_display"],
         red if data["total_current"] > 0 else green),
    ]
    for cell, (key, value, color), width in zip(
        stat_table.rows[0].cells, stat_items, stat_widths
    ):
        cell.width = width
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        _docx_set_cell_shading(cell, soft)
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.space_before = Pt(2)
        _docx_style_run(paragraph.add_run(key.upper() + "\n"), 8, True, muted)
        _docx_style_run(paragraph.add_run(value), 14, True, color)

    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_before = Pt(0)
    spacer.paragraph_format.space_after = Pt(0)
    _docx_style_run(spacer.add_run(""), 5)

    # ---------- Data table ----------
    table = doc.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.allow_autofit = False

    widths = [1.0, 2.6, 6.0, 5.4, 3.2, 3.2, 5.9]
    headers = [
        labels["sl"], labels["id"], labels["name"], labels["contact"],
        f"{labels['previous']}\n(৳)", f"{labels['current']}\n(৳)", labels["note"],
    ]
    header_row = table.rows[0]
    _docx_set_repeat_header(header_row)
    for index, (cell, value, width) in enumerate(
        zip(header_row.cells, headers, widths)
    ):
        cell.width = Cm(width)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        _docx_set_cell_shading(cell, navy)
        paragraph = cell.paragraphs[0]
        paragraph.alignment = (
            WD_ALIGN_PARAGRAPH.RIGHT if index in (4, 5)
            else (WD_ALIGN_PARAGRAPH.CENTER if index in (0, 1)
                  else WD_ALIGN_PARAGRAPH.LEFT)
        )
        paragraph.paragraph_format.space_after = Pt(0)
        _docx_style_run(paragraph.add_run(value), 8.5, True, "FFFFFF")

    for index, row in enumerate(data["rows"]):
        cells = table.add_row().cells
        values = [
            str(row["sl"]), row["id"], row["name"], row["contact"],
            money(row["previous"]), money(row["current"]), row["note"],
        ]
        for col, (cell, value, width) in enumerate(zip(cells, values, widths)):
            cell.width = Cm(width)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            if index % 2 == 1:
                _docx_set_cell_shading(cell, soft)
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            if col in (0, 1):
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _docx_style_run(paragraph.add_run(value), 8.5, False, muted)
            elif col in (4, 5):
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                color = red if col == 5 and row["current"] > 0 else ink
                if col == 5 and row["current"] <= 0:
                    color = green
                _docx_style_run(paragraph.add_run(value), 8.5,
                                col == 5, color)
            elif col == 2:
                _docx_style_run(paragraph.add_run(value), 8.5, True, ink)
            else:
                _docx_style_run(paragraph.add_run(value), 8.0, False, muted)

    if data["rows"]:
        total_row = table.add_row()
        merged = total_row.cells[0].merge(total_row.cells[3])
        merged.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        paragraph = merged.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        paragraph.paragraph_format.space_after = Pt(0)
        _docx_style_run(paragraph.add_run(labels["total"]), 9.5, True, ink)

        values = [
            (4, money(data["total_previous"]), ink),
            (5, money(data["total_current"]),
             red if data["total_current"] > 0 else green),
        ]
        for col, value, color in values:
            cell = total_row.cells[col]
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            paragraph.paragraph_format.space_after = Pt(0)
            _docx_style_run(paragraph.add_run(value), 10, True, color)
        for cell in total_row.cells:
            _docx_set_cell_shading(cell, gold_soft)

    note = doc.add_paragraph()
    note.paragraph_format.space_before = Pt(8)
    note.paragraph_format.left_indent = Cm(0.2)
    _docx_style_run(
        note.add_run(f"{labels['names']}: {data['count']}   ·   {labels['amount_note']}"),
        8.5, False, muted,
    )

    # Signature strip: business on the left, partner on the right.
    sig_table = doc.add_table(rows=1, cols=2)
    sig_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    sig_table.autofit = False
    sig_table.allow_autofit = False
    for cell, caption in zip(sig_table.rows[0].cells,
                              (labels["business"], labels["partner"])):
        cell.width = Cm(12.0)
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_before = Pt(18)
        paragraph.paragraph_format.space_after = Pt(0)
        _docx_style_run(paragraph.add_run("_" * 42), 9, False, "CBD5E1")
        sub = cell.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub.paragraph_format.space_before = Pt(1)
        sub.paragraph_format.space_after = Pt(0)
        _docx_style_run(sub.add_run(caption), 8, True, muted)

    # Footer: FinCore label left, page number right.
    footer = section.footer
    footer_paragraph = footer.paragraphs[0]
    footer_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _docx_style_run(
        footer_paragraph.add_run(f"FinCore ERP   ·   {labels['footer']}  ·  "
                                 f"{data['generated_label']}  ·  "),
        7.5, False, muted,
    )
    _docx_style_run(footer_paragraph.add_run(f"{labels['page']} "), 7.5,
                    False, muted)
    _docx_add_page_number(footer_paragraph)

    props = doc.core_properties
    props.title = f"{data['title']} — {data['partner']}"
    props.author = "FinCore ERP"
    props.subject = f"{data['title']} {data['date_label']}"
    props.comments = "Generated from FinCore ERP ledger data."

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# সুপার ক্যালকুলেটর — the shop's profit / loss report (PDF / Excel / Word)
# ---------------------------------------------------------------------------

SUPER_LABELS: dict[str, dict[str, str]] = {
    "bn": {
        "title": "সুপার ক্যালকুলেটর",
        "business": "ব্যবসা",
        "date": "তারিখ",
        "period": "সময়কাল",
        "generated": "তৈরি",
        "sell_cash": "নগদে সেল",
        "sell_due": "বাকিতে সেল",
        "sell_total": "মোট সেল",
        "khoroch": "খরচ / ক্রয়",
        "profit": "লাভ / লস",
        "running": "চলতি ফল",
        "collection": "বাকি আদায়",
        "days": "দিন",
        "total": "মোট",
        "formula": "মোট সেল = নগদে সেল + বাকিতে সেল · লাভ/লস = মোট সেল - মোট খরচ",
        "note_collection": "বাকি আদায় পুরনো সেলের ফেরত টাকা — নতুন সেলে যোগ হয় না।",
        "empty": "এই সময়ে কোনো সেল বা খরচ নেই।",
        "footer": "FinCore ERP থেকে স্বয়ংক্রিয়ভাবে তৈরি",
        "amount_note": "সব টাকা বাংলাদেশি টাকায় (৳)",
        "page": "পৃষ্ঠা",
        "sheet": "সুপার ক্যালকুলেটর",
        # ---- step-by-step statement (cash, partner books, expense, summary, final) ----
        "steps": "ধাপে ধাপে হিসাব",
        "step_word": "ধাপ",
        "num_1": "১",
        "num_2": "২",
        "num_3": "৩",
        "num_4": "৪",
        "num_5": "৫",
        "step_cash": "নগদের হিসাব",
        "step_partner": "পার্টনারভিত্তিক বাকি খাতা",
        "step_expense": "খরচের হিসাব",
        "step_summary": "সংক্ষিপ্ত বিবরণ",
        "step_result": "চূড়ান্ত লাভ / লোকসান",
        "step_cash_desc": "নগদে বিক্রি হওয়া প্রতিটি সেল, তারিখ অনুযায়ী।",
        "step_partner_desc": "প্রতিটি পার্টনারের খাতায় গ্রাহকভিত্তিক বর্তমান বাকি।",
        "step_expense_desc": "খরচ ও ক্রয়ের প্রতিটি এন্ট্রি, তারিখ অনুযায়ী।",
        "step_summary_desc": "পার্টনারদের নাম ও তাদের বাকি, ক্রম অনুযায়ী — তারপর নগদ, খরচ ও লাভ/লোকসানের হিসাব।",
        "step_result_desc": "সব হিসাবের শেষ ফল — মোট সেল থেকে মোট খরচ বাদ দিয়ে।",
        "sl": "ক্রম",
        "due_given": "বাকি দিয়েছে",
        "collected": "আদায়",
        "net_due": "নিট বাকি",
        "particular": "বিবরণ",
        "customer": "গ্রাহক",
        "partner": "পার্টনার",
        "method": "মাধ্যম",
        "amount": "টাকা",
        "type": "ধরন",
        "category": "খরচের ধরন",
        "entries": "এন্ট্রি",
        "customers": "গ্রাহক",
        "due_now": "বর্তমান বাকি",
        "period_due": "এই সময়ের বাকি",
        "period_collection": "এই সময়ের আদায়",
        "partner_total": "পার্টনারের মোট বাকি",
        "no_entries": "এই সময়ে কোনো এন্ট্রি নেই।",
        "no_partner_books": "এই সময়ে কোনো পার্টনারের বাকি খাতা নেই।",
        "collection_note": "বাকি আদায় পুরনো সেলের ফেরত টাকা — নতুন সেলে যোগ হয় না।",
        "collection_context": "শুধু তথ্য — লাভ/লোকসানে যোগ হয় না।",
        "result_rule": "মোট সেল = নগদে সেল + বাকিতে সেল · লাভ / লোকসান = মোট সেল − মোট খরচ",
        "profit_word": "লাভ",
        "loss_word": "লোকসান",
        "net_sell": "মোট সেল",
        "net_expense": "মোট খরচ",
        "net_result": "লাভ / লোকসান",
        "cash_total": "মোট নগদে সেল",
        "due_total": "মোট বাকিতে সেল",
        "expense_total": "মোট খরচ",
        "collection_total": "মোট বাকি আদায়",
        "summary_total": "সারসংক্ষেপ",
        "partner_books_total": "সব পার্টনারের মোট বাকি",
        "due_sales": "বাকিতে সেল",
        "collections": "বাকি আদায়",
        "active": "চালু",
        "closed": "বন্ধ",
        "in_period": "এই সময়ে",
        "empty_step": "এই ধাপে কোনো এন্ট্রি নেই।",
    },
    "en": {
        "title": "Super Calculator",
        "business": "Business",
        "date": "Date",
        "period": "Period",
        "generated": "Generated",
        "sell_cash": "Cash sell",
        "sell_due": "Due sell",
        "sell_total": "Total sell",
        "khoroch": "Expenses / Purchases",
        "profit": "Profit / Loss",
        "running": "Running",
        "collection": "Due collected",
        "days": "days",
        "total": "Total",
        "formula": "Total sell = Cash sell + Due sell · Profit / Loss = Total sell - Expenses",
        "note_collection": "Due collected is an older sale paying back — never counted as new sell.",
        "empty": "No sells or expenses in this period.",
        "footer": "Generated automatically by FinCore ERP",
        "amount_note": "All amounts are in Bangladeshi Taka (৳)",
        "page": "Page",
        "sheet": "Super Calculator",
        # ---- step-by-step statement (cash, partner books, expense, summary, final) ----
        "steps": "Step-by-step statement",
        "step_word": "Step",
        "num_1": "1",
        "num_2": "2",
        "num_3": "3",
        "num_4": "4",
        "num_5": "5",
        "step_cash": "Cash account",
        "step_partner": "Partner-wise due books",
        "step_expense": "Expense account",
        "step_summary": "Summary",
        "step_result": "Final profit / loss",
        "step_cash_desc": "Every cash sale, dated.",
        "step_partner_desc": "Each partner's book, customer by customer.",
        "step_expense_desc": "Every expense and purchase, dated.",
        "step_summary_desc": "Partner names and their dues, in serial order — then cash, expenses and the profit / loss calculation.",
        "step_result_desc": "The final result — total sell minus total expenses.",
        "sl": "SL",
        "due_given": "Due given",
        "collected": "Collected",
        "net_due": "Net due",
        "particular": "Particular",
        "customer": "Customer",
        "partner": "Partner",
        "method": "Method",
        "amount": "Amount",
        "type": "Type",
        "category": "Expense type",
        "entries": "entries",
        "customers": "customers",
        "due_now": "Current due",
        "period_due": "Due this period",
        "period_collection": "Collected this period",
        "partner_total": "Partner total due",
        "no_entries": "No entries in this period.",
        "no_partner_books": "No partner due books in this period.",
        "collection_note": "Due collected is an older sale paying back — never counted as new sell.",
        "collection_context": "Context only — not part of profit / loss.",
        "result_rule": "Total sell = Cash sell + Due sell · Profit / Loss = Total sell − Total expenses",
        "profit_word": "Profit",
        "loss_word": "Loss",
        "net_sell": "Total sell",
        "net_expense": "Total expenses",
        "net_result": "Profit / Loss",
        "cash_total": "Total cash sell",
        "due_total": "Total due sell",
        "expense_total": "Total expenses",
        "collection_total": "Total due collected",
        "summary_total": "Summary",
        "partner_books_total": "All partners' total due",
        "due_sales": "Due sales",
        "collections": "Due collected",
        "active": "Active",
        "closed": "Closed",
        "in_period": "this period",
        "empty_step": "No entries in this step.",
    },
}


def signed_money(value: Any) -> str:
    """Format a Decimal with an explicit sign: +৳600.00 / -৳500.00 / ৳0.00."""
    try:
        amount = Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except Exception:
        amount = Decimal("0.00")
    if amount > 0:
        return f"+৳{amount:,.2f}"
    if amount < 0:
        return f"-৳{-amount:,.2f}"
    return "৳0.00"


SUPER_TYPE_LABELS: dict[str, dict[str, str]] = {
    "bn": {
        TransactionType.INCOME: "নগদে সেল",
        TransactionType.CREDIT: "বাকিতে সেল",
        TransactionType.EXPENSE: "খরচ / ক্রয়",
        TransactionType.PAYMENT: "বাকি আদায়",
    },
    "en": {
        TransactionType.INCOME: "Cash sell",
        TransactionType.CREDIT: "Due sell",
        TransactionType.EXPENSE: "Expense / Purchase",
        TransactionType.PAYMENT: "Due collected",
    },
}

SUPER_METHOD_LABELS: dict[str, dict[str, str]] = {
    "bn": {
        PaymentMethod.CASH: "নগদ",
        PaymentMethod.BANK: "ব্যাংক",
        PaymentMethod.BKASH: "বিকাশ",
        PaymentMethod.NAGAD: "নগদ (Nagad)",
        PaymentMethod.ROCKET: "রকেট",
        PaymentMethod.CARD: "কার্ড",
        PaymentMethod.OTHER: "অন্যান্য",
    },
    "en": {
        PaymentMethod.CASH: "Cash",
        PaymentMethod.BANK: "Bank",
        PaymentMethod.BKASH: "bKash",
        PaymentMethod.NAGAD: "Nagad",
        PaymentMethod.ROCKET: "Rocket",
        PaymentMethod.CARD: "Card",
        PaymentMethod.OTHER: "Other",
    },
}

SUPER_EXPENSE_LABELS: dict[str, dict[str, str]] = {
    "bn": {value: label for value, label in ExpenseKind.choices},
    "en": {
        ExpenseKind.GOODS: "Goods / Stock",
        ExpenseKind.RAW_MATERIAL: "Raw material",
        ExpenseKind.SALARY: "Salary / Bonus",
        ExpenseKind.RENT: "Rent",
        ExpenseKind.TRANSPORT: "Transport",
        ExpenseKind.UTILITIES: "Utilities",
        ExpenseKind.OPERATING: "Shop expense",
        ExpenseKind.OTHER: "Other",
    },
}


def _supercalc_detail_rows(
    business,
    dfrom: date,
    dto: date,
    lang: str,
) -> list[dict[str, Any]]:
    """Every cash sale, due sale, collection and expense behind the totals.

    The day table aggregates; this list keeps the actual ledger rows so the
    PDF can show each entry with its own date, name, note and amount.  The
    running profit moves only on a sell or an expense — a collection is an
    old sale paying back, so it stays context and never changes the result.
    """
    labels = SUPER_LABELS[lang]
    type_labels = SUPER_TYPE_LABELS[lang]
    expense_labels = SUPER_EXPENSE_LABELS[lang]
    kind_map = {
        TransactionType.INCOME: "cash",
        TransactionType.CREDIT: "due",
        TransactionType.EXPENSE: "expense",
        TransactionType.PAYMENT: "collection",
    }
    # Map kind string back to TransactionType for label lookup
    kind_to_type = {v: k for k, v in kind_map.items()}

    txns = (Transaction.objects
            .filter(business=business, status=TransactionStatus.COMPLETED,
                    transaction_date__gte=dfrom, transaction_date__lte=dto,
                    transaction_type__in=tuple(kind_map))
            .select_related("customer", "partner")
            .order_by("transaction_date", "created_at", "id"))

    rows: list[dict[str, Any]] = []
    running = Decimal("0.00")
    for txn in txns:
        kind = kind_map[txn.transaction_type]
        amount = Decimal(str(txn.amount or 0)).quantize(Decimal("0.01"))
        if kind == "expense":
            effect = -amount
        elif kind == "collection":
            effect = Decimal("0.00")
        else:
            effect = amount
        running = (running + effect).quantize(Decimal("0.01"))

        customer = (txn.customer.name
                    if txn.customer_id and txn.customer else "")
        partner = (txn.partner.name
                   if txn.partner_id and txn.partner else "")
        if customer and partner:
            party = f"{customer} · {partner}"
        else:
            party = customer or partner or "—"

        description = (txn.description or "").strip()
        if txn.transaction_type == TransactionType.EXPENSE:
            category = expense_labels.get(
                txn.expense_kind, txn.get_expense_kind_display())
        else:
            category = type_labels[kind_to_type[kind]]
        particular = description or category

        rows.append({
            "id": str(txn.id),
            "date": txn.transaction_date,
            "date_label": txn.transaction_date.strftime("%d %b %Y"),
            "kind": kind,
            "type": txn.transaction_type,
            "customer_id": txn.customer_id,
            "partner_id": txn.partner_id,
            "customer": customer or "—",
            "partner": partner or "—",
            "party": party,
            "description": description or "—",
            "particular": particular,
            "category": category,
            "type_label": type_labels[kind_to_type[kind]],
            "method": SUPER_METHOD_LABELS[lang].get(
                txn.payment_method,
                txn.get_payment_method_display() or "—"),
            "amount": amount,
            "amount_display": money(amount),
            "effect": effect,
            "effect_display": ("—" if kind == "collection"
                               else signed_money(effect)),
            "is_context": kind == "collection",
            "running": running,
            "running_display": signed_money(running),
        })
    return rows


def _supercalc_partner_books(
    business,
    detail_rows: list[dict[str, Any]],
    lang: str,
) -> list[dict[str, Any]]:
    """One attractive due book per partner, customer by customer.

    ``total_due`` is the live outstanding due (opening due + every movement),
    while ``period_due`` / ``period_collection`` are this report's activity.
    A closed book with no money and no activity is skipped — it has nothing
    left to show.
    """
    from apps.partners.models import Partner

    period_due: dict[tuple, Decimal] = {}
    period_collection: dict[tuple, Decimal] = {}
    for row in detail_rows:
        if not (row["partner_id"] and row["customer_id"]):
            continue
        key = (row["partner_id"], row["customer_id"])
        if row["kind"] == "due":
            period_due[key] = (period_due.get(key, Decimal("0.00"))
                               + row["amount"])
        elif row["kind"] == "collection":
            period_collection[key] = (
                period_collection.get(key, Decimal("0.00")) + row["amount"])

    books: list[dict[str, Any]] = []
    partners = Partner.objects.filter(business=business).order_by("name")
    for partner in partners:
        assignments = list(
            CustomerAssignment.objects
            .filter(partner=partner, customer__business=business)
            .select_related("customer")
            .prefetch_related("due_entries"))
        book_rows: list[dict[str, Any]] = []
        total_due = Decimal("0.00")
        period_due_total = Decimal("0.00")
        period_collection_total = Decimal("0.00")
        for assignment in assignments:
            due = Decimal(str(assignment.current_due())).quantize(
                Decimal("0.01"))
            key = (partner.id, assignment.customer_id)
            due_period = period_due.get(key, Decimal("0.00"))
            collected_period = period_collection.get(key, Decimal("0.00"))
            total_due += due
            period_due_total += due_period
            period_collection_total += collected_period
            if (not assignment.is_active and due == 0
                    and due_period == 0 and collected_period == 0):
                continue
            book_rows.append({
                "name": assignment.customer.name,
                "due": due,
                "due_display": money(due),
                "period_due": due_period,
                "period_due_display": money(due_period),
                "period_collection": collected_period,
                "period_collection_display": money(collected_period),
                "is_active": assignment.is_active,
            })
        if not book_rows:
            continue
        book_rows.sort(key=lambda item: (-item["due"], item["name"].lower()))
        books.append({
            "name": partner.name,
            "phone": (partner.phone or "").strip(),
            "count": sum(1 for item in book_rows if item["is_active"]),
            "row_count": len(book_rows),
            "total_due": total_due.quantize(Decimal("0.01")),
            "total_due_display": money(total_due),
            "period_due": period_due_total.quantize(Decimal("0.01")),
            "period_due_display": money(period_due_total),
            "period_collection": period_collection_total.quantize(
                Decimal("0.01")),
            "period_collection_display": money(period_collection_total),
            "rows": book_rows,
        })
    books.sort(key=lambda book: (-book["total_due"], book["name"].lower()))
    return books


def build_supercalc_export(
    business,
    dfrom: date,
    dto: date,
    lang: str = "bn",
) -> dict[str, Any]:
    """Build the same day rows the Super Calculator page shows, for export."""
    from apps.web.supercalc import calc_day_rows, calc_totals

    lang = "en" if str(lang).lower().startswith("en") else "bn"
    labels = SUPER_LABELS[lang]

    raw_rows = calc_day_rows(business, dfrom, dto)
    totals = calc_totals(business, dfrom, dto)

    rows: list[dict[str, Any]] = []
    for index, r in enumerate(raw_rows, start=1):
        rows.append({
            "sl": index,
            "date": r["date"],
            "date_label": r["date"].strftime("%d %b %Y"),
            "sell_cash": r["sell_cash"],
            "sell_cash_display": money(r["sell_cash"]),
            "sell_due": r["sell_due"],
            "sell_due_display": money(r["sell_due"]),
            "sell_total": r["sell_total"],
            "sell_total_display": money(r["sell_total"]),
            "khoroch": r["khoroch"],
            "khoroch_display": money(r["khoroch"]),
            "profit": r["profit"],
            "profit_display": signed_money(r["profit"]),
            "running": r["running_profit"],
            "running_display": signed_money(r["running_profit"]),
            "collection": r["collection"],
            "collection_display": money(r["collection"]),
            "has_data": r["has_data"],
        })

    totals_out = {
        "sell_cash": totals["sell_cash"],
        "sell_cash_display": money(totals["sell_cash"]),
        "sell_due": totals["sell_due"],
        "sell_due_display": money(totals["sell_due"]),
        "sell_total": totals["sell_total"],
        "sell_total_display": money(totals["sell_total"]),
        "khoroch": totals["khoroch"],
        "khoroch_display": money(totals["khoroch"]),
        "profit": totals["profit"],
        "profit_display": signed_money(totals["profit"]),
        "collection": totals["collection"],
        "collection_display": money(totals["collection"]),
        # The running total at period end is the period profit itself.
        "running": totals["profit"],
        "running_display": signed_money(totals["profit"]),
    }

    # ---- Step-by-step statement data ----
    detail_rows = _supercalc_detail_rows(business, dfrom, dto, lang)
    partner_books = _supercalc_partner_books(business, detail_rows, lang)

    # Split detail rows by kind for the steps
    cash_rows = [r for r in detail_rows if r["kind"] == "cash"]
    due_rows = [r for r in detail_rows if r["kind"] == "due"]
    expense_rows = [r for r in detail_rows if r["kind"] == "expense"]
    collection_rows = [r for r in detail_rows if r["kind"] == "collection"]

    # Serial list of partners with their dues, used by the summary step.
    summary_partners = [
        {
            "sl": index,
            "name": book["name"],
            "phone": book["phone"],
            "due_given": book["period_due"],
            "due_given_display": book["period_due_display"],
            "collection": book["period_collection"],
            "collection_display": book["period_collection_display"],
            "current_due": book["total_due"],
            "current_due_display": book["total_due_display"],
            "customers": book["row_count"],
        }
        for index, book in enumerate(partner_books, start=1)
    ]

    now = timezone.localtime()
    period_label = f"{dfrom.strftime('%d %b %Y')} → {dto.strftime('%d %b %Y')}"
    return {
        "business": business.name,
        "dfrom": dfrom,
        "dto": dto,
        "period_label": period_label,
        "generated_label": now.strftime("%d %b %Y, %I:%M %p"),
        "title": labels["title"],
        "rows": rows,
        "count": len(rows),
        "days_with_data": sum(1 for r in rows if r["has_data"]),
        "totals": totals_out,
        "labels": labels,
        "lang": lang,
        "filename": f"super-calculator-{dfrom.isoformat()}_{dto.isoformat()}",
        # ---- New step-by-step sections (cash, partner books, expense, summary, final) ----
        "cash_rows": cash_rows,
        "due_rows": due_rows,
        "expense_rows": expense_rows,
        "collection_rows": collection_rows,
        "partner_books": partner_books,
        "summary_partners": summary_partners,
        # Summary counts for the new sections
        "cash_total": sum((r["amount"] for r in cash_rows), Decimal("0.00")).quantize(Decimal("0.01")),
        "cash_total_display": money(sum((r["amount"] for r in cash_rows), Decimal("0.00")).quantize(Decimal("0.01"))),
        "due_total": sum((r["amount"] for r in due_rows), Decimal("0.00")).quantize(Decimal("0.01")),
        "due_total_display": money(sum((r["amount"] for r in due_rows), Decimal("0.00")).quantize(Decimal("0.01"))),
        "expense_total": sum((r["amount"] for r in expense_rows), Decimal("0.00")).quantize(Decimal("0.01")),
        "expense_total_display": money(sum((r["amount"] for r in expense_rows), Decimal("0.00")).quantize(Decimal("0.01"))),
        "collection_total": sum((r["amount"] for r in collection_rows), Decimal("0.00")).quantize(Decimal("0.01")),
        "collection_total_display": money(sum((r["amount"] for r in collection_rows), Decimal("0.00")).quantize(Decimal("0.01"))),
        "partner_books_total": sum((b["total_due"] for b in partner_books), Decimal("0.00")).quantize(Decimal("0.01")),
        "partner_books_total_display": money(sum((b["total_due"] for b in partner_books), Decimal("0.00")).quantize(Decimal("0.01"))),
        "summary_due_given": sum((p["due_given"] for p in summary_partners), Decimal("0.00")).quantize(Decimal("0.01")),
        "summary_due_given_display": money(sum((p["due_given"] for p in summary_partners), Decimal("0.00")).quantize(Decimal("0.01"))),
        "summary_collection": sum((p["collection"] for p in summary_partners), Decimal("0.00")).quantize(Decimal("0.01")),
        "summary_collection_display": money(sum((p["collection"] for p in summary_partners), Decimal("0.00")).quantize(Decimal("0.01"))),
    }


# =====================================================================
# Super Calculator PDF — fpdf2 fallback (step-by-step statement)
# =====================================================================

_SUPER_NAVY = (11, 37, 69)
_SUPER_GOLD = (201, 150, 27)
_SUPER_GOLD_SOFT = (253, 243, 214)
_SUPER_TEAL = (15, 118, 110)
_SUPER_RED = (185, 28, 28)
_SUPER_GREEN = (4, 120, 87)
_SUPER_MUTED = (100, 116, 139)
_SUPER_INK = (15, 23, 42)
_SUPER_SOFT = (248, 250, 252)
_SUPER_LINE = (226, 232, 240)
_SUPER_WARN = (245, 158, 11)
_SUPER_OK_BG = (220, 252, 231)
_SUPER_OK_TEXT = (5, 150, 105)
_SUPER_DANGER_BG = (254, 242, 242)
_SUPER_DANGER_TEXT = (185, 28, 28)
_SUPER_WARN_BG = (255, 251, 235)
_SUPER_WARN_TEXT = (180, 83, 9)
_SUPER_GREEN_BG = (240, 253, 244)
_SUPER_GREEN_TEXT = (22, 101, 52)


class _SuperPDF:
    """Small fpdf2 wrapper with the gold Super Calculator brand."""

    def __new__(cls, data: dict[str, Any]):
        from fpdf import FPDF

        labels = data["labels"]

        class SuperPDF(FPDF):
            def __init__(self):
                super().__init__(orientation="L", unit="mm", format="A4")
                self.set_auto_page_break(auto=True, margin=17)
                self.set_margins(10, 10, 10)
                self.alias_nb_pages()
                self.add_font("Noto", "", str(FONT_REGULAR))
                self.add_font("Noto", "B", str(FONT_BOLD))
                self.set_text_shaping(True)
                self.set_title(f"{data['title']} — {data['business']}")
                self.set_author("FinCore ERP")
                self.set_subject(f"{data['title']} {data['period_label']}")
                self.set_creator("FinCore ERP")

            def header(self):
                # Deep amber-navy banner with a gold accent bar.
                self.set_fill_color(*_SUPER_NAVY)
                self.rect(0, 0, self.w, 26, "F")
                self.set_fill_color(47, 36, 16)
                self.rect(0, 0, self.w, 14, "F")
                self.set_fill_color(*_SUPER_GOLD)
                self.rect(0, 26, self.w, 1.4, "F")

                self.set_fill_color(253, 230, 138)
                self.rect(self.l_margin, 6, 12, 12, "F")
                self.set_xy(self.l_margin, 9.2)
                self.set_font("Noto", "B", 10)
                self.set_text_color(74, 52, 8)
                self.cell(12, 6, "৳", align="C")

                self.set_xy(self.l_margin + 16, 5.4)
                self.set_font("Noto", "B", 17)
                self.set_text_color(255, 255, 255)
                self.cell(130, 8, data["title"])

                self.set_xy(self.l_margin + 16, 13.4)
                self.set_font("Noto", "", 9.5)
                self.set_text_color(216, 189, 138)
                self.cell(130, 6, data["period_label"])

                self.set_xy(self.w - self.r_margin - 110, 8)
                self.set_font("Noto", "", 7.6)
                self.set_text_color(163, 140, 100)
                self.cell(110, 5, labels["business"].upper(), align="R")
                self.set_xy(self.w - self.r_margin - 110, 13)
                self.set_font("Noto", "B", 10)
                self.set_text_color(244, 236, 221)
                self.cell(110, 6, data["business"], align="R")

                self.set_y(32)

            def footer(self):
                self.set_y(-12.5)
                self.set_draw_color(*_SUPER_LINE)
                self.line(self.l_margin, self.get_y(), self.w - self.r_margin,
                          self.get_y())
                self.set_y(-10)
                self.set_font("Noto", "", 7.3)
                self.set_text_color(*_SUPER_MUTED)
                self.cell(0, 5, f"{labels['footer']}  ·  {data['generated_label']}")
                self.set_xy(self.w - self.r_margin - 32, self.get_y())
                self.cell(32, 5, f"{labels['page']} {self.page_no()}", align="R")

        return SuperPDF()


def _super_pdf_step_header(pdf, data: dict[str, Any], step_num: str,
                           step_title: str, step_desc: str, total_label: str,
                           total_value: str) -> None:
    """Header for each step section."""
    y = pdf.get_y()
    # Step number circle
    pdf.set_fill_color(*_SUPER_GOLD)
    pdf.set_draw_color(*_SUPER_GOLD)
    cx = pdf.l_margin + 6
    cy = y + 8
    pdf.ellipse(cx - 5, cy - 5, 10, 10, "DF")
    pdf.set_xy(cx - 5, cy - 2.5)
    pdf.set_font("Noto", "B", 9)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(10, 5, step_num, align="C")

    # Title and description
    pdf.set_xy(pdf.l_margin + 20, y + 2)
    pdf.set_font("Noto", "B", 11)
    pdf.set_text_color(*_SUPER_INK)
    pdf.cell(pdf.epw - 40, 6, step_title)
    pdf.set_xy(pdf.l_margin + 20, y + 8.5)
    pdf.set_font("Noto", "", 7.2)
    pdf.set_text_color(*_SUPER_MUTED)
    pdf.cell(pdf.epw - 40, 4, step_desc)

    # Total on the right
    pdf.set_xy(pdf.w - pdf.r_margin - 55, y + 2)
    pdf.set_font("Noto", "", 6.5)
    pdf.set_text_color(*_SUPER_MUTED)
    pdf.cell(50, 4, total_label.upper(), align="R")
    pdf.set_xy(pdf.w - pdf.r_margin - 55, y + 7)
    pdf.set_font("Noto", "B", 10)
    pdf.set_text_color(*_SUPER_GOLD)
    pdf.cell(50, 5, total_value, align="R")

    # Separator line
    pdf.set_draw_color(*_SUPER_GOLD)
    pdf.line(pdf.l_margin, y + 15, pdf.w - pdf.r_margin, y + 15)
    pdf.set_y(y + 17)


def _super_pdf_simple_table(pdf, data: dict[str, Any], rows: list[dict],
                             widths: list[float], headers: list[str],
                             get_cells, kind: str = "normal") -> None:
    """Generic table renderer for step tables."""
    labels = data["labels"]
    if not rows:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(*_SUPER_LINE)
        pdf.rect(pdf.l_margin, pdf.get_y(), pdf.epw, 12, "DF")
        pdf.set_xy(pdf.l_margin + 2, pdf.get_y() + 3)
        pdf.set_font("Noto", "", 8.5)
        pdf.set_text_color(*_SUPER_MUTED)
        pdf.cell(pdf.epw - 4, 6, labels["empty_step"], align="C")
        pdf.set_y(pdf.get_y() + 14)
        return

    # Header
    y = pdf.get_y()
    x = pdf.l_margin
    pdf.set_fill_color(*_SUPER_NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Noto", "B", 7.5)
    for label, w in zip(headers, widths):
        pdf.set_xy(x, y)
        pdf.cell(w, 8, label, align="C", fill=True)
        x += w
    pdf.set_draw_color(*_SUPER_NAVY)
    pdf.rect(pdf.l_margin, y, sum(widths), 8)
    x = pdf.l_margin
    for w in widths[:-1]:
        x += w
        pdf.line(x, y, x, y + 8)
    pdf.set_y(y + 8)

    # Rows
    for idx, row in enumerate(rows):
        row_h = 7.2
        if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
            pdf.add_page()
            # Re-draw header
            y = pdf.get_y()
            x = pdf.l_margin
            pdf.set_fill_color(*_SUPER_NAVY)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Noto", "B", 7.5)
            for label, w in zip(headers, widths):
                pdf.set_xy(x, y)
                pdf.cell(w, 8, label, align="C", fill=True)
                x += w
            pdf.set_draw_color(*_SUPER_NAVY)
            pdf.rect(pdf.l_margin, y, sum(widths), 8)
            x = pdf.l_margin
            for w in widths[:-1]:
                x += w
                pdf.line(x, y, x, y + 8)
            pdf.set_y(y + 8)

        y = pdf.get_y()
        if idx % 2 == 0:
            pdf.set_fill_color(*_SUPER_SOFT)
            pdf.rect(pdf.l_margin, y, sum(widths), row_h, "F")
        pdf.set_draw_color(*_SUPER_LINE)
        pdf.rect(pdf.l_margin, y, sum(widths), row_h)
        x = pdf.l_margin
        for w in widths[:-1]:
            x += w
            pdf.line(x, y, x, y + row_h)

        cells = get_cells(row, kind)
        x = pdf.l_margin
        for i, (text, align) in enumerate(cells):
            pdf.set_xy(x + 1, y + 1)
            color = _SUPER_INK
            weight = ""
            if kind == "expense" and i == len(cells) - 2:  # amount column
                color = _SUPER_RED
                weight = "B"
            elif kind == "due" and i == len(cells) - 2:
                color = _SUPER_WARN
                weight = "B"
            elif kind == "collection" and i == len(cells) - 2:
                color = _SUPER_MUTED
                weight = "I"
            elif i == len(cells) - 1:  # running column
                color = _SUPER_MUTED
            pdf.set_font("Noto", weight, 7.2)
            pdf.set_text_color(*color)
            pdf.cell(widths[i] - 2, row_h - 1, text, align=align)
            x += widths[i]
        pdf.set_y(y + row_h)

    # Footer totals row (handled by caller)


def _super_pdf_cash_table(pdf, data: dict[str, Any]) -> None:
    labels = data["labels"]
    rows = data.get("cash_rows", [])
    widths = [22, 52, 32, 32, 26, 32, 36]
    headers = [labels["date"], labels["particular"], labels["customer"],
               labels["partner"], labels["method"], labels["amount"],
               labels["running"]]

    def get_cells(row, kind):
        return [
            (row["date_label"], "C"),
            (row["particular"][:28], "L"),
            (row["customer"][:14], "L"),
            (row["partner"][:14], "L"),
            (row["method"][:12], "C"),
            (row["amount_display"], "R"),
            (row["running_display"], "R"),
        ]

    _super_pdf_simple_table(pdf, data, rows, widths, headers, get_cells, "cash")

    # Total row
    y = pdf.get_y()
    x = pdf.l_margin
    pdf.set_fill_color(*_SUPER_GOLD_SOFT)
    pdf.set_draw_color(*_SUPER_GOLD)
    pdf.rect(pdf.l_margin, y, sum(widths), 8, "DF")
    pdf.set_font("Noto", "B", 8)
    pdf.set_text_color(*_SUPER_INK)
    pdf.set_xy(x + 1, y + 1)
    pdf.cell(widths[0] - 2, 6, labels["total"], align="R")
    x += widths[0]
    for i in range(1, 5):
        x += widths[i]
    pdf.set_xy(x + 1, y + 1)
    pdf.cell(widths[5] - 2, 6, money(data["cash_total"]), align="R")
    x += widths[5]
    pdf.set_xy(x + 1, y + 1)
    pdf.set_text_color(*_SUPER_MUTED)
    pdf.cell(widths[6] - 2, 6, data["totals"]["sell_cash_display"], align="R")
    pdf.set_y(y + 10)


def _super_pdf_expense_table(pdf, data: dict[str, Any]) -> None:
    labels = data["labels"]
    rows = data.get("expense_rows", [])
    widths = [22, 52, 38, 26, 32, 36]
    headers = [labels["date"], labels["particular"], labels["category"],
               labels["method"], labels["amount"], labels["running"]]

    def get_cells(row, kind):
        return [
            (row["date_label"], "C"),
            (row["particular"][:30], "L"),
            (row["category"][:16], "L"),
            (row["method"][:12], "C"),
            (row["amount_display"], "R"),
            (row["running_display"], "R"),
        ]

    _super_pdf_simple_table(pdf, data, rows, widths, headers, get_cells, "expense")

    # Total row
    y = pdf.get_y()
    x = pdf.l_margin
    pdf.set_fill_color(*_SUPER_GOLD_SOFT)
    pdf.set_draw_color(*_SUPER_GOLD)
    pdf.rect(pdf.l_margin, y, sum(widths), 8, "DF")
    pdf.set_font("Noto", "B", 8)
    pdf.set_text_color(*_SUPER_INK)
    pdf.set_xy(x + 1, y + 1)
    pdf.cell(widths[0] - 2, 6, labels["total"], align="R")
    x += widths[0]
    for i in range(1, 3):
        x += widths[i]
    pdf.set_xy(x + 1, y + 1)
    pdf.set_text_color(*_SUPER_RED)
    pdf.cell(widths[4] - 2, 6, money(data["expense_total"]), align="R")
    x += widths[4]
    pdf.set_xy(x + 1, y + 1)
    pdf.set_text_color(*_SUPER_MUTED)
    pdf.cell(widths[5] - 2, 6, data["totals"]["khoroch_display"], align="R")
    pdf.set_y(y + 10)


def _super_pdf_summary_section(pdf, data: dict[str, Any]) -> None:
    """Partner-wise serial summary, then cash + expense -> profit/loss."""
    labels = data["labels"]

    # ---------- serial partner table ----------
    partners = data.get("summary_partners", [])
    widths = [16, 74, 24, 36, 36, 40]
    headers = [labels["sl"], labels["partner"], labels["customers"],
               labels["due_given"], labels["collected"], labels["net_due"]]

    def _header(pdf, widths, headers):
        y = pdf.get_y()
        x = pdf.l_margin
        pdf.set_fill_color(*_SUPER_NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Noto", "B", 7.4)
        for label, w in zip(headers, widths):
            pdf.set_xy(x, y)
            pdf.cell(w, 8, label, align="C" if label != labels["partner"] else "L",
                     fill=True)
            x += w
        pdf.set_draw_color(*_SUPER_NAVY)
        pdf.rect(pdf.l_margin, y, sum(widths), 8)
        pdf.set_y(y + 8)

    if partners:
        _header(pdf, widths, headers)
        for idx, p in enumerate(partners):
            row_h = 7.0
            if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
                pdf.add_page()
                _header(pdf, widths, headers)

            y = pdf.get_y()
            if idx % 2 == 0:
                pdf.set_fill_color(*_SUPER_SOFT)
                pdf.rect(pdf.l_margin, y, sum(widths), row_h, "F")
            pdf.set_draw_color(*_SUPER_LINE)
            pdf.rect(pdf.l_margin, y, sum(widths), row_h)
            x = pdf.l_margin

            pdf.set_xy(x + 1, y + 0.8)
            pdf.set_font("Noto", "", 7.4)
            pdf.set_text_color(*_SUPER_MUTED)
            pdf.cell(widths[0] - 2, row_h - 1.6, str(p["sl"]), align="C")
            x += widths[0]

            pdf.set_xy(x + 1, y + 0.8)
            pdf.set_font("Noto", "B", 7.4)
            pdf.set_text_color(*_SUPER_INK)
            pdf.cell(widths[1] - 2, row_h - 1.6, str(p["name"])[:32], align="L")
            x += widths[1]

            pdf.set_xy(x + 1, y + 0.8)
            pdf.set_font("Noto", "", 7.4)
            pdf.set_text_color(*_SUPER_MUTED)
            pdf.cell(widths[2] - 2, row_h - 1.6, str(p["customers"]), align="C")
            x += widths[2]

            pdf.set_xy(x + 1, y + 0.8)
            pdf.set_font("Noto", "", 7.4)
            pdf.set_text_color(*_SUPER_INK)
            pdf.cell(widths[3] - 2, row_h - 1.6, p["due_given_display"], align="R")
            x += widths[3]

            pdf.set_xy(x + 1, y + 0.8)
            pdf.set_text_color(*_SUPER_OK_TEXT)
            pdf.cell(widths[4] - 2, row_h - 1.6, p["collection_display"], align="R")
            x += widths[4]

            pdf.set_xy(x + 1, y + 0.8)
            pdf.set_font("Noto", "B", 7.4)
            pdf.set_text_color(*_SUPER_WARN_TEXT)
            pdf.cell(widths[5] - 2, row_h - 1.6, p["current_due_display"], align="R")
            pdf.set_y(y + row_h)

        # Total row
        y = pdf.get_y()
        x = pdf.l_margin
        pdf.set_fill_color(*_SUPER_GOLD_SOFT)
        pdf.set_draw_color(*_SUPER_GOLD)
        pdf.rect(pdf.l_margin, y, sum(widths), 8, "DF")
        pdf.set_font("Noto", "B", 8)
        pdf.set_text_color(*_SUPER_INK)
        pdf.set_xy(x + 1, y + 1)
        pdf.cell(sum(widths[:3]) - 2, 6, labels["total"], align="R")
        x += widths[0] + widths[1] + widths[2]
        pdf.set_xy(x + 1, y + 1)
        pdf.cell(widths[3] - 2, 6, data.get("summary_due_given_display", money(Decimal("0.00"))), align="R")
        x += widths[3]
        pdf.set_xy(x + 1, y + 1)
        pdf.set_text_color(*_SUPER_OK_TEXT)
        pdf.cell(widths[4] - 2, 6, data.get("summary_collection_display", money(Decimal("0.00"))), align="R")
        x += widths[4]
        pdf.set_xy(x + 1, y + 1)
        pdf.set_text_color(*_SUPER_WARN_TEXT)
        pdf.cell(widths[5] - 2, 6, data["partner_books_total_display"], align="R")
        pdf.set_y(y + 10)
    else:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(*_SUPER_LINE)
        pdf.rect(pdf.l_margin, pdf.get_y(), pdf.epw, 12, "DF")
        pdf.set_xy(pdf.l_margin + 2, pdf.get_y() + 3)
        pdf.set_font("Noto", "", 8.5)
        pdf.set_text_color(*_SUPER_MUTED)
        pdf.cell(pdf.epw - 4, 6, labels["no_partner_books"], align="C")
        pdf.set_y(pdf.get_y() + 14)

    # ---------- cash / expense -> profit calculation ----------
    totals = data["totals"]
    profit_color = _SUPER_RED if totals["profit"] < 0 else _SUPER_GREEN
    calc_rows = [
        (labels["cash_total"], data["cash_total_display"], _SUPER_INK, ""),
        (f'+  {labels["due_total"]}', data["due_total_display"], _SUPER_INK, ""),
        (f'=  {labels["net_sell"]}', totals["sell_total_display"], _SUPER_INK, "STRONG"),
        (f'−  {labels["expense_total"]}', data["expense_total_display"], _SUPER_INK, ""),
        (f'=  {labels["net_result"]}', totals["profit_display"], profit_color, "RESULT"),
    ]

    if pdf.get_y() + 45 > pdf.h - pdf.b_margin:
        pdf.add_page()

    calc_y = pdf.get_y() + 2
    for label, value, color, style in calc_rows:
        row_h = 7.6
        if style == "STRONG":
            pdf.set_fill_color(*_SUPER_GOLD_SOFT)
            pdf.set_draw_color(*_SUPER_GOLD)
        elif style == "RESULT":
            if totals["profit"] < 0:
                pdf.set_fill_color(*_SUPER_DANGER_BG)
                pdf.set_draw_color(*_SUPER_DANGER_TEXT)
            else:
                pdf.set_fill_color(*_SUPER_OK_BG)
                pdf.set_draw_color(*_SUPER_OK_TEXT)
        else:
            pdf.set_fill_color(247, 250, 253)
            pdf.set_draw_color(*_SUPER_LINE)
        pdf.rect(pdf.l_margin, calc_y, pdf.epw, row_h, "DF")
        pdf.set_xy(pdf.l_margin + 4, calc_y + 1)
        is_strong = style in ("STRONG", "RESULT")
        pdf.set_font("Noto", "B" if is_strong else "", 8 if is_strong else 7.6)
        pdf.set_text_color(*(_SUPER_INK if not is_strong else
                             (_SUPER_WARN_TEXT if style == "STRONG" else color)))
        pdf.cell(120, row_h - 2, label, align="L")
        pdf.set_xy(pdf.w - pdf.r_margin - 60, calc_y + 0.6)
        if style == "RESULT":
            pdf.set_font("Noto", "B", 11)
        pdf.set_text_color(*(color if style == "RESULT" else _SUPER_INK))
        pdf.cell(56, row_h - 2, value, align="R")
        calc_y += row_h
    pdf.set_y(calc_y + 2)


def _super_pdf_partner_books(pdf, data: dict[str, Any]) -> None:
    labels = data["labels"]
    books = data.get("partner_books", [])

    if not books:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(*_SUPER_LINE)
        pdf.rect(pdf.l_margin, pdf.get_y(), pdf.epw, 12, "DF")
        pdf.set_xy(pdf.l_margin + 2, pdf.get_y() + 3)
        pdf.set_font("Noto", "", 8.5)
        pdf.set_text_color(*_SUPER_MUTED)
        pdf.cell(pdf.epw - 4, 6, labels["no_partner_books"], align="C")
        pdf.set_y(pdf.get_y() + 14)
        return

    for book in books:
        # Book header
        y = pdf.get_y()
        if y + 30 > pdf.h - pdf.b_margin:
            pdf.add_page()
            y = pdf.get_y()

        pdf.set_fill_color(*_SUPER_OK_BG)
        pdf.set_draw_color(_SUPER_OK_TEXT[0], _SUPER_OK_TEXT[1], _SUPER_OK_TEXT[2])
        pdf.rect(pdf.l_margin, y, pdf.epw, 10, "DF")

        pdf.set_xy(pdf.l_margin + 2, y + 1)
        pdf.set_font("Noto", "", 6.8)
        pdf.set_text_color(*_SUPER_OK_TEXT)
        pdf.cell(30, 4, labels["partner"].upper())
        pdf.set_xy(pdf.l_margin + 2, y + 5)
        pdf.set_font("Noto", "B", 9.5)
        pdf.cell(60, 4, book["name"])

        if book.get("phone"):
            pdf.set_xy(pdf.l_margin + 65, y + 5)
            pdf.set_font("Noto", "", 7)
            pdf.set_text_color(*_SUPER_MUTED)
            pdf.cell(40, 4, f"({book['phone']})")

        # Meta on right
        meta_x = pdf.w - pdf.r_margin - 80
        pdf.set_xy(meta_x, y + 1)
        pdf.set_font("Noto", "B", 7)
        pdf.set_text_color(*_SUPER_INK)
        pdf.cell(75, 4, f"{labels['customers']}: {book['count']}/{book['row_count']}  |  "
                         f"{labels['period_due']}: {book['period_due_display']}  |  "
                         f"{labels['period_collection']}: {book['period_collection_display']}", align="R")

        pdf.set_y(y + 12)

        # Book table
        rows = book["rows"]
        widths = [70, 35, 35, 30, 25]
        headers = [labels["customer"], labels["due_now"], labels["period_due"],
                   labels["period_collection"], labels["active"]]

        # Header
        y = pdf.get_y()
        x = pdf.l_margin
        pdf.set_fill_color(_SUPER_OK_TEXT[0], _SUPER_OK_TEXT[1], _SUPER_OK_TEXT[2])
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Noto", "B", 7.2)
        for label, w in zip(headers, widths):
            pdf.set_xy(x, y)
            pdf.cell(w, 7, label, align="C" if label != labels["customer"] else "L", fill=True)
            x += w
        pdf.set_y(y + 7)

        # Rows
        for idx, item in enumerate(rows):
            row_h = 6.5
            if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
                pdf.add_page()
                y = pdf.get_y()
                x = pdf.l_margin
                pdf.set_fill_color(_SUPER_OK_TEXT[0], _SUPER_OK_TEXT[1], _SUPER_OK_TEXT[2])
                pdf.set_text_color(255, 255, 255)
                pdf.set_font("Noto", "B", 7.2)
                for label, w in zip(headers, widths):
                    pdf.set_xy(x, y)
                    pdf.cell(w, 7, label, align="C" if label != labels["customer"] else "L", fill=True)
                    x += w
                pdf.set_y(y + 7)

            y = pdf.get_y()
            if not item["is_active"]:
                pdf.set_fill_color(*_SUPER_DANGER_BG)
            elif idx % 2 == 0:
                pdf.set_fill_color(*_SUPER_GREEN_BG)
            else:
                pdf.set_fill_color(255, 255, 255)
            pdf.set_draw_color(*_SUPER_LINE)
            pdf.rect(pdf.l_margin, y, sum(widths), row_h, "DF" if idx % 2 == 0 or not item["is_active"] else "D")
            x = pdf.l_margin

            pdf.set_xy(x + 1, y + 0.5)
            pdf.set_font("Noto", "B" if not item["is_active"] else "", 7)
            pdf.set_text_color(*(_SUPER_DANGER_TEXT if not item["is_active"] else _SUPER_INK))
            pdf.cell(widths[0] - 2, row_h - 1, item["name"][:32], align="L")
            x += widths[0]

            pdf.set_xy(x + 1, y + 0.5)
            pdf.set_font("Noto", "B", 7.2)
            pdf.set_text_color(*_SUPER_INK)
            pdf.cell(widths[1] - 2, row_h - 1, item["due_display"], align="R")
            x += widths[1]

            pdf.set_xy(x + 1, y + 0.5)
            pdf.set_font("Noto", "", 7)
            pdf.set_text_color(*_SUPER_INK)
            pdf.cell(widths[2] - 2, row_h - 1, item["period_due_display"], align="R")
            x += widths[2]

            pdf.set_xy(x + 1, y + 0.5)
            pdf.set_font("Noto", "", 7)
            pdf.set_text_color(*_SUPER_OK_TEXT)
            pdf.cell(widths[3] - 2, row_h - 1, item["period_collection_display"], align="R")
            x += widths[3]

            # Status badge
            pdf.set_xy(x + 2, y + 1)
            badge_text = labels["active"] if item["is_active"] else labels["closed"]
            badge_color = _SUPER_OK_TEXT if item["is_active"] else _SUPER_DANGER_TEXT
            badge_bg = _SUPER_OK_BG if item["is_active"] else _SUPER_DANGER_BG
            pdf.set_fill_color(*badge_bg)
            pdf.set_draw_color(*badge_color)
            pdf.set_font("Noto", "B", 5.5)
            pdf.set_text_color(*badge_color)
            bw = pdf.get_string_width(badge_text) + 4
            pdf.cell(bw, 4.5, badge_text, align="C", fill=True, border=1)
            x += widths[4]

            pdf.set_y(y + row_h)

        # Book total row
        y = pdf.get_y()
        x = pdf.l_margin
        pdf.set_fill_color(*_SUPER_GOLD_SOFT)
        pdf.set_draw_color(*_SUPER_GOLD)
        pdf.rect(pdf.l_margin, y, sum(widths), 7, "DF")
        pdf.set_xy(x + 1, y + 0.5)
        pdf.set_font("Noto", "B", 7.5)
        pdf.set_text_color(*_SUPER_INK)
        pdf.cell(widths[0] - 2, 6, labels["partner_total"], align="L")
        x += widths[0]
        pdf.set_xy(x + 1, y + 0.5)
        pdf.cell(widths[1] - 2, 6, book["total_due_display"], align="R")
        x += widths[1]
        pdf.set_xy(x + 1, y + 0.5)
        pdf.cell(widths[2] - 2, 6, book["period_due_display"], align="R")
        x += widths[2]
        pdf.set_xy(x + 1, y + 0.5)
        pdf.set_text_color(*_SUPER_OK_TEXT)
        pdf.cell(widths[3] - 2, 6, book["period_collection_display"], align="R")
        pdf.set_y(y + 9)

        # Gap between books
        pdf.ln(4)


def _super_pdf_final_result(pdf, data: dict[str, Any]) -> None:
    labels = data["labels"]
    t = data["totals"]
    profit_color = _SUPER_RED if t["profit"] < 0 else _SUPER_GREEN

    # Outer box
    y = pdf.get_y()
    if y + 45 > pdf.h - pdf.b_margin:
        pdf.add_page()
        y = pdf.get_y()

    # Header bar
    pdf.set_fill_color(*_SUPER_NAVY)
    pdf.rect(pdf.l_margin, y, pdf.epw, 10, "F")
    pdf.set_xy(pdf.l_margin, y + 1.5)
    pdf.set_font("Noto", "B", 13)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(pdf.epw, 7, labels["step_result"], align="C")

    # Body — three equal-height boxes, vertically centered, aligned on one line.
    box_w = (pdf.epw - 8) / 3
    gap = 4
    box_h = 26
    body_y = y + 12
    if body_y + box_h + 4 > pdf.h - pdf.b_margin:
        pdf.add_page()
        body_y = pdf.get_y() + 1

    boxes = [
        (labels["net_sell"], t["sell_total_display"], _SUPER_TEAL, False),
        (labels["net_expense"], t["khoroch_display"], _SUPER_RED, False),
        (labels["net_result"], t["profit_display"], profit_color, True),
    ]

    x = pdf.l_margin
    for label, value, color, is_result in boxes:
        if is_result:
            # the highlighted answer box — soft gold fill + gold border
            pdf.set_fill_color(255, 253, 240)
            pdf.set_draw_color(*_SUPER_GOLD)
        else:
            pdf.set_fill_color(255, 255, 255)
            pdf.set_draw_color(*_SUPER_LINE)
        pdf.rect(x, body_y, box_w, box_h, "DF")

        # vertically centered: label above value, both centered in the box
        pdf.set_xy(x, body_y + 6)
        pdf.set_font("Noto", "", 7.2)
        pdf.set_text_color(*_SUPER_MUTED)
        pdf.cell(box_w, 4, label, align="C")

        pdf.set_xy(x, body_y + 11)
        pdf.set_font("Noto", "B", 14 if is_result else 11.5)
        pdf.set_text_color(*color)
        pdf.cell(box_w, 8, value, align="C")

        x += box_w + gap

    pdf.set_y(body_y + box_h + 4)


def _super_pdf_meta(pdf, data: dict[str, Any]) -> None:
    """Meta strip + four stat boxes + the formula line."""
    labels = data["labels"]
    t = data["totals"]
    y = pdf.get_y()
    width = pdf.epw
    pdf.set_fill_color(247, 250, 253)
    pdf.set_draw_color(*_SUPER_LINE)
    pdf.rect(pdf.l_margin, y, width, 13, "DF")

    columns = [
        (labels["business"], data["business"], 82),
        (labels["period"], data["period_label"], 92),
        (labels["days"], str(data["count"]), 30),
        (labels["generated"], data["generated_label"], width - 208),
    ]
    x = pdf.l_margin + 3
    for label, value, col_width in columns:
        pdf.set_xy(x, y + 2)
        pdf.set_font("Noto", "", 6.8)
        pdf.set_text_color(*_SUPER_MUTED)
        pdf.cell(col_width - 4, 4, label)
        pdf.set_xy(x, y + 6.2)
        pdf.set_font("Noto", "B", 8.6)
        pdf.set_text_color(*_SUPER_INK)
        pdf.cell(col_width - 4, 5, str(value)[:60])
        x += col_width
    pdf.set_y(y + 16)

    # ---- Stat strip: মোট সেল / খরচ / লাভ-লস / বাকি আদায় ----
    y = pdf.get_y() + 1
    gap = 4.0
    box_w = (width - gap * 3) / 4
    profit_color = _SUPER_RED if t["profit"] < 0 else _SUPER_GREEN
    x = pdf.l_margin
    for key, value, color in (
        (labels["sell_total"], t["sell_total_display"], _SUPER_TEAL),
        (labels["khoroch"], t["khoroch_display"], _SUPER_RED),
        (labels["profit"], t["profit_display"], profit_color),
        (labels["collection"], t["collection_display"], _SUPER_WARN),
    ):
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(*_SUPER_LINE)
        pdf.rect(x, y, box_w, 15, "DF")
        pdf.set_fill_color(*color)
        pdf.rect(x, y, 1.6, 15, "F")
        pdf.set_xy(x + 4, y + 2.4)
        pdf.set_font("Noto", "", 7)
        pdf.set_text_color(*_SUPER_MUTED)
        pdf.cell(box_w - 8, 4, key.upper())
        pdf.set_xy(x + 4, y + 6.6)
        pdf.set_font("Noto", "B", 10.5)
        pdf.set_text_color(*_SUPER_INK)
        pdf.cell(box_w - 8, 6, value, align="R")
        x += box_w + gap
    pdf.set_y(y + 18)

    pdf.set_font("Noto", "", 7.6)
    pdf.set_text_color(*_SUPER_MUTED)
    pdf.cell(pdf.epw, 4.5, labels["formula"])
    pdf.ln(8)


def export_supercalc_pdf(data: dict[str, Any]) -> bytes:
    browser_pdf = _render_pdf_with_browser(
        data, template_name="web/supercalc_export_pdf.html",
        prefix="fincore-supercalc-")
    if browser_pdf:
        return browser_pdf

    # Portable fallback: fpdf2 with bundled Noto Sans Bengali.
    pdf = _SuperPDF(data)
    pdf.add_page()
    _super_pdf_meta(pdf, data)

    if not data["rows"]:
        pdf.set_fill_color(248, 250, 252)
        pdf.set_draw_color(*_SUPER_LINE)
        pdf.rect(pdf.l_margin, pdf.get_y(), pdf.epw, 12, "DF")
        pdf.set_xy(pdf.l_margin + 2, pdf.get_y() + 3)
        pdf.set_font("Noto", "", 9)
        pdf.set_text_color(*_SUPER_MUTED)
        pdf.cell(pdf.epw - 4, 6, data["labels"]["empty"], align="C")
    else:
        # ---- STEP 1: Cash Account ----
        _super_pdf_step_header(pdf, data,
                               data["labels"]["num_1"],
                               data["labels"]["step_cash"],
                               data["labels"]["step_cash_desc"],
                               data["labels"]["cash_total"],
                               money(data["cash_total"]))
        _super_pdf_cash_table(pdf, data)

        # ---- STEP 2: Partner-wise Due Books ----
        _super_pdf_step_header(pdf, data,
                               data["labels"]["num_2"],
                               data["labels"]["step_partner"],
                               data["labels"]["step_partner_desc"],
                               data["labels"]["partner_books_total"],
                               money(data["partner_books_total"]))
        _super_pdf_partner_books(pdf, data)

        # ---- STEP 3: Expense Account ----
        _super_pdf_step_header(pdf, data,
                               data["labels"]["num_3"],
                               data["labels"]["step_expense"],
                               data["labels"]["step_expense_desc"],
                               data["labels"]["expense_total"],
                               money(data["expense_total"]))
        _super_pdf_expense_table(pdf, data)

        # ---- STEP 4: Summary ----
        _super_pdf_step_header(pdf, data,
                               data["labels"]["num_4"],
                               data["labels"]["step_summary"],
                               data["labels"]["step_summary_desc"],
                               data["labels"]["summary_total"],
                               f'{len(data.get("summary_partners", []))} {data["labels"]["entries"]}')
        _super_pdf_summary_section(pdf, data)

        # ---- FINAL RESULT ----
        _super_pdf_final_result(pdf, data)

    output = pdf.output()
    return bytes(output)


def export_supercalc_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    labels = data["labels"]
    t = data["totals"]
    wb = Workbook()
    ws = wb.active
    ws.title = labels["sheet"][:31]
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 90
    ws.sheet_properties.tabColor = "C9961B"

    navy = "0B2545"
    navy_dark = "1C1205"
    gold = "C9961B"
    gold_soft = "FDF3D6"
    teal = "0F766E"
    ink = "0B1220"
    muted = "64748B"
    line = "E6EDF5"
    soft = "F8FAFC"
    red = "B91C1C"
    green = "047857"
    white = "FFFFFF"
    profit_color = red if t["profit"] < 0 else green

    thin = Side(style="thin", color=line)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    base_font = Font(name="Nirmala UI", size=10, color=ink)
    header_font = Font(name="Nirmala UI", size=9.5, bold=True, color=white)
    title_font = Font(name="Nirmala UI", size=20, bold=True, color=white)
    sub_font = Font(name="Nirmala UI", size=11, bold=True, color=gold)
    meta_font = Font(name="Nirmala UI", size=9, color=muted)
    bold_font = Font(name="Nirmala UI", size=10, bold=True, color=ink)
    stat_key_font = Font(name="Nirmala UI", size=8, bold=True, color=muted)
    header_fill = PatternFill("solid", fgColor=navy)
    soft_fill = PatternFill("solid", fgColor=soft)
    gold_fill = PatternFill("solid", fgColor=gold_soft)

    widths = [15, 16, 16, 17, 16, 17, 17]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width

    # ---- Premium banner ----
    ws.merge_cells("A1:G2")
    ws["A1"] = f"{data['title']} — {data['business']}"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws["A1"].fill = PatternFill("solid", fgColor=navy_dark)
    for row in (1, 2):
        for col in range(1, 8):
            ws.cell(row=row, column=col).fill = PatternFill(
                "solid", fgColor=navy_dark)
    ws.row_dimensions[1].height = 26
    ws.row_dimensions[2].height = 18

    ws.merge_cells("A3:G3")
    ws["A3"] = f"{data['period_label']}   ·   {labels['formula']}"
    ws["A3"].font = sub_font
    ws["A3"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[3].height = 20

    ws.merge_cells("A4:G4")
    ws["A4"] = (f"{labels['generated']}: {data['generated_label']}   ·   "
                f"{labels['collection']}: {t['collection_display']}   ·   "
                f"{labels['amount_note']}")
    ws["A4"].font = meta_font
    ws["A4"].alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[4].height = 16

    # ---- Stat strip ----
    stat_row = 5
    stat_cells = [
        (1, 2, labels["sell_total"], t["sell_total_display"], teal),
        (3, 4, labels["khoroch"], t["khoroch_display"], red),
        (5, 7, labels["profit"], t["profit_display"], profit_color),
    ]
    for col_start, col_end, key, value, value_color in stat_cells:
        ws.merge_cells(start_row=stat_row, start_column=col_start,
                       end_row=stat_row, end_column=col_end)
        key_cell = ws.cell(row=stat_row, column=col_start, value=key.upper())
        key_cell.font = stat_key_font
        key_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.merge_cells(start_row=stat_row + 1, start_column=col_start,
                       end_row=stat_row + 1, end_column=col_end)
        val_cell = ws.cell(row=stat_row + 1, column=col_start, value=value)
        val_cell.font = Font(name="Nirmala UI", size=13, bold=True,
                             color=value_color)
        val_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        for r in (stat_row, stat_row + 1):
            for c in range(col_start, col_end + 1):
                ws.cell(row=r, column=c).fill = PatternFill(
                    "solid", fgColor="F7FAFD")
                ws.cell(row=r, column=c).border = Border(
                    left=thin, right=thin, top=thin, bottom=thin)
    ws.row_dimensions[stat_row].height = 15
    ws.row_dimensions[stat_row + 1].height = 22

    header_row = stat_row + 3
    headers = [
        labels["date"], f"{labels['sell_cash']} (৳)", f"{labels['sell_due']} (৳)",
        f"{labels['sell_total']} (৳)", f"{labels['khoroch']} (৳)",
        f"{labels['profit']} (৳)", f"{labels['running']} (৳)",
    ]
    for col, value in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col, value=value)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(
            horizontal="right" if col in (2, 3, 4, 5, 6, 7) else "center",
            vertical="center", wrap_text=True,
        )
    ws.row_dimensions[header_row].height = 30

    first_data_row = header_row + 1
    for offset, row in enumerate(data["rows"]):
        r = first_data_row + offset
        values = [
            row["date_label"], float(row["sell_cash"]), float(row["sell_due"]),
            float(row["sell_total"]), float(row["khoroch"]),
            float(row["profit"]), float(row["running"]),
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=col, value=value)
            cell.font = bold_font if col == 1 else base_font
            cell.border = border
            if col == 1:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(horizontal="right", vertical="center")
                cell.number_format = "#,##0.00"
            if col == 6:
                cell.font = Font(name="Nirmala UI", size=10, bold=True,
                                 color=red if row["profit"] < 0 else green)
            elif col == 7:
                cell.font = Font(name="Nirmala UI", size=10,
                                 color=red if row["running"] < 0 else muted)
            if offset % 2 == 1:
                cell.fill = soft_fill
        ws.row_dimensions[r].height = 22

    if data["rows"]:
        last_row = first_data_row + len(data["rows"]) - 1
        total_row = last_row + 1
        ws.merge_cells(start_row=total_row, start_column=1,
                       end_row=total_row, end_column=3)
        total_label = ws.cell(row=total_row, column=1, value=labels["total"])
        total_label.font = bold_font
        total_label.alignment = Alignment(horizontal="right", vertical="center")
        for col in range(1, 8):
            cell = ws.cell(row=total_row, column=col)
            cell.fill = gold_fill
            cell.border = Border(
                left=thin, right=thin,
                top=Side(style="double", color=gold), bottom=thin,
            )
        for col, key in ((4, "sell_total"), (5, "khoroch"),
                         (6, "profit"), (7, "running")):
            cell = ws.cell(row=total_row, column=col, value=float(t[key]))
            cell.number_format = "#,##0.00"
            cell.alignment = Alignment(horizontal="right", vertical="center")
            if col in (6, 7):
                cell.font = Font(name="Nirmala UI", size=11, bold=True,
                                 color=red if t[key] < 0 else ink)
            else:
                cell.font = Font(name="Nirmala UI", size=11, bold=True, color=ink)
        ws.row_dimensions[total_row].height = 26
        ws.auto_filter.ref = f"A{header_row}:G{last_row}"
        ws.freeze_panes = f"A{first_data_row}"
        ws.print_title_rows = f"{header_row}:{header_row}"
        print_last = total_row
    else:
        print_last = header_row

    ws.print_area = f"A1:G{print_last}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    if ws.sheet_properties.pageSetUpPr is None:
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    else:
        ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = 0.3
    ws.page_margins.right = 0.3
    ws.page_margins.top = 0.45
    ws.page_margins.bottom = 0.45
    ws.oddFooter.left.text = "FinCore ERP"
    ws.oddFooter.left.size = 8
    ws.oddFooter.left.color = muted
    ws.oddFooter.center.text = f"{labels['footer']}  ·  {data['generated_label']}"
    ws.oddFooter.center.size = 8
    ws.oddFooter.center.color = muted
    ws.oddFooter.right.text = "Page &P of &N"
    ws.oddFooter.right.size = 8
    ws.oddFooter.right.color = muted

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def export_supercalc_docx(data: dict[str, Any]) -> bytes:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    labels = data["labels"]
    t = data["totals"]
    navy = "0B2545"
    navy_dark = "1C1205"
    gold = "C9961B"
    gold_soft = "FDF3D6"
    teal = "0F766E"
    ink = "0B1220"
    muted = "64748B"
    soft = "F7FAFD"
    red = "B91C1C"
    green = "047857"
    profit_color = red if t["profit"] < 0 else green

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Cm(29.7)
    section.page_height = Cm(21.0)
    section.left_margin = Cm(1.1)
    section.right_margin = Cm(1.1)
    section.top_margin = Cm(0.9)
    section.bottom_margin = Cm(1.0)

    # ---------- Premium banner ----------
    banner = doc.add_paragraph()
    banner.paragraph_format.space_after = Pt(0)
    banner.paragraph_format.space_before = Pt(0)
    banner.paragraph_format.left_indent = Cm(0.2)
    banner.paragraph_format.right_indent = Cm(0.2)
    _docx_shade_paragraph(banner, navy_dark)
    _docx_style_run(banner.add_run(f"  {data['title']}  —  {data['business']}"),
                    20, True, "FFFFFF")
    _docx_add_horizontal_rule(banner, gold, 24)
    banner.paragraph_format.space_after = Pt(6)

    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_before = Pt(0)
    subtitle.paragraph_format.space_after = Pt(0)
    subtitle.paragraph_format.left_indent = Cm(0.2)
    _docx_style_run(
        subtitle.add_run(f"{data['period_label']}   ·   {labels['formula']}"),
        10.5, True, gold,
    )

    meta = doc.add_paragraph()
    meta.paragraph_format.space_before = Pt(3)
    meta.paragraph_format.space_after = Pt(8)
    meta.paragraph_format.left_indent = Cm(0.2)
    _docx_style_run(
        meta.add_run(
            f"{labels['generated']}: {data['generated_label']}   ·   "
            f"{labels['collection']}: {t['collection_display']}   ·   "
            f"{labels['amount_note']}"),
        8.5, False, muted,
    )

    # ---------- Stat strip ----------
    stat_table = doc.add_table(rows=1, cols=3)
    stat_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    stat_table.autofit = False
    stat_table.allow_autofit = False
    stat_widths = [Cm(9.0), Cm(9.0), Cm(9.0)]
    stat_items = [
        (labels["sell_total"], t["sell_total_display"], teal),
        (labels["khoroch"], t["khoroch_display"], red),
        (labels["profit"], t["profit_display"], profit_color),
    ]
    for cell, (key, value, color), width in zip(
        stat_table.rows[0].cells, stat_items, stat_widths
    ):
        cell.width = width
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        _docx_set_cell_shading(cell, soft)
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.space_before = Pt(2)
        _docx_style_run(paragraph.add_run(key.upper() + "\n"), 8, True, muted)
        _docx_style_run(paragraph.add_run(value), 14, True, color)

    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_before = Pt(0)
    spacer.paragraph_format.space_after = Pt(0)
    _docx_style_run(spacer.add_run(""), 5)

    # ---------- Data table ----------
    table = doc.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.allow_autofit = False

    widths = [3.6, 3.6, 3.6, 3.8, 3.6, 4.0, 3.8]
    headers = [
        labels["date"], f"{labels['sell_cash']}\n(৳)", f"{labels['sell_due']}\n(৳)",
        f"{labels['sell_total']}\n(৳)", f"{labels['khoroch']}\n(৳)",
        f"{labels['profit']}\n(৳)", f"{labels['running']}\n(৳)",
    ]
    header_row = table.rows[0]
    _docx_set_repeat_header(header_row)
    for index, (cell, value, width) in enumerate(
        zip(header_row.cells, headers, widths)
    ):
        cell.width = Cm(width)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        _docx_set_cell_shading(cell, navy)
        paragraph = cell.paragraphs[0]
        paragraph.alignment = (
            WD_ALIGN_PARAGRAPH.CENTER if index == 0 else WD_ALIGN_PARAGRAPH.RIGHT)
        paragraph.paragraph_format.space_after = Pt(0)
        _docx_style_run(paragraph.add_run(value), 8.5, True, "FFFFFF")

    for index, row in enumerate(data["rows"]):
        cells = table.add_row().cells
        values = [
            row["date_label"], row["sell_cash_display"], row["sell_due_display"],
            row["sell_total_display"], row["khoroch_display"],
            row["profit_display"], row["running_display"],
        ]
        for col, (cell, value, width) in enumerate(zip(cells, values, widths)):
            cell.width = Cm(width)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            if index % 2 == 1:
                _docx_set_cell_shading(cell, soft)
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            if col == 0:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _docx_style_run(paragraph.add_run(value), 8.5, False, muted)
            elif col == 3:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                _docx_style_run(paragraph.add_run(value), 8.5, True, ink)
            elif col == 5:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                row_profit_color = red if row["profit"] < 0 else green
                _docx_style_run(paragraph.add_run(value), 8.5, True,
                                row_profit_color)
            elif col == 6:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                row_running_color = red if row["running"] < 0 else muted
                _docx_style_run(paragraph.add_run(value), 8.5, False,
                                row_running_color)
            elif col == 4:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                _docx_style_run(paragraph.add_run(value), 8.5, False, red)
            else:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                _docx_style_run(paragraph.add_run(value), 8.5, False, ink)

    if data["rows"]:
        total_row = table.add_row()
        merged = total_row.cells[0].merge(total_row.cells[2])
        merged.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        paragraph = merged.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        paragraph.paragraph_format.space_after = Pt(0)
        _docx_style_run(paragraph.add_run(labels["total"]), 9.5, True, ink)

        total_values = [
            (3, t["sell_total_display"], ink),
            (4, t["khoroch_display"], red),
            (5, t["profit_display"], profit_color),
            (6, t["running_display"], profit_color),
        ]
        for col, value, color in total_values:
            cell = total_row.cells[col]
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            paragraph.paragraph_format.space_after = Pt(0)
            _docx_style_run(paragraph.add_run(value), 10, True, color)
        for cell in total_row.cells:
            _docx_set_cell_shading(cell, gold_soft)

    note = doc.add_paragraph()
    note.paragraph_format.space_before = Pt(8)
    note.paragraph_format.left_indent = Cm(0.2)
    _docx_style_run(
        note.add_run(
            f"{labels['collection']}: {t['collection_display']}   ·   "
            f"{labels['note_collection']}   ·   {labels['amount_note']}"),
        8.5, False, muted,
    )

    # Signature strip: business on the left, period on the right.
    sig_table = doc.add_table(rows=1, cols=2)
    sig_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    sig_table.autofit = False
    sig_table.allow_autofit = False
    for cell, caption in zip(sig_table.rows[0].cells,
                              (labels["business"], labels["period"])):
        cell.width = Cm(12.0)
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_before = Pt(18)
        paragraph.paragraph_format.space_after = Pt(0)
        _docx_style_run(paragraph.add_run("_" * 42), 9, False, "CBD5E1")
        sub = cell.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub.paragraph_format.space_before = Pt(1)
        sub.paragraph_format.space_after = Pt(0)
        _docx_style_run(sub.add_run(caption), 8, True, muted)

    # Footer: FinCore label left, page number right.
    footer = section.footer
    footer_paragraph = footer.paragraphs[0]
    footer_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _docx_style_run(
        footer_paragraph.add_run(f"FinCore ERP   ·   {labels['footer']}  ·  "
                                 f"{data['generated_label']}  ·  "),
        7.5, False, muted,
    )
    _docx_style_run(footer_paragraph.add_run(f"{labels['page']} "), 7.5,
                    False, muted)
    _docx_add_page_number(footer_paragraph)

    props = doc.core_properties
    props.title = f"{data['title']} — {data['business']}"
    props.author = "FinCore ERP"
    props.subject = f"{data['title']} {data['period_label']}"
    props.comments = "Generated from FinCore ERP Super Calculator data."

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
