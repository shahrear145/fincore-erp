"""হিসাব সারাংশ export — PDF, Excel and Word.

The export mirrors the new summary page: the two blocks (what the
business has / what it owes), the net position, the period result and
the day-wise cash movement.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.utils import timezone

from apps.web.exports import (
    DOCX_CONTENT_TYPE, FONT_BOLD, FONT_REGULAR, PDF_CONTENT_TYPE,
    XLSX_CONTENT_TYPE, _find_pdf_browser, _render_pdf_with_browser,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

SUMMARY_LABELS: dict[str, dict[str, str]] = {
    "bn": {
        "title": "হিসাব সারাংশ",
        "have": "ব্যবসার কাছে যা আছে",
        "in_hand": "হাতে আছে",
        "coming": "আসবে (বাকি)",
        "owe": "যা ফেরত দিতে হবে",
        "net": "নিট অবস্থান",
        "period": "এই সময়ের হিসাব",
        "cash": "ড্রয়ারে নগদ",
        "wallet": "বিকাশ / নগদ / ব্যাংক",
        "due": "কাস্টমারের পাওনা",
        "invest": "বিনিয়োগকারীর মূল",
        "invest_note": "নিজের টাকা — ফেরত দিতে হবে না, নিট অবস্থানেও যোগ হয়নি",
        "loan": "ঋণদাতাকে ফেরত",
        "advances": "কাস্টমারের অগ্রিম",
        "payable": "খরচ বাকি",
        "operating": "এই সময়ে খরচ",
        "sell": "মোট সেল",
        "goods": "মাংস / মাল ক্রয়",
        "collection": "বাকি আদায়",
        "end_cash": "সময় শেষে হাতে",
        "day_table": "দিনভিত্তিক নগদের চলাচল",
        "date": "তারিখ",
        "in": "নগদ আসল",
        "out": "নগদ গেছে",
        "loan_cap": "ঋণ / মূল",
        "baki_book": "বাকির বই",
        "drawer": "দিনশেষে হাতে",
        "total": "মোট",
        "generated": "তৈরি",
        "page": "পৃষ্ঠা",
        "footer": "FinCore ERP — হিসাব সারাংশ",
        "note": "সব সংখ্যা সরাসরি হিসাবের খাতা থেকে নেওয়া।",
    },
    "en": {
        "title": "Summary",
        "have": "What the business has",
        "in_hand": "In hand",
        "coming": "Coming in (baki)",
        "owe": "What it still owes",
        "net": "Net position",
        "period": "This period",
        "cash": "Cash in the drawer",
        "wallet": "bKash / Nagad / bank",
        "due": "Customers owe me",
        "invest": "Investor's capital",
        "invest_note": "your own money — nothing to return, not counted in the net position",
        "loan": "Loan to repay",
        "advances": "Customer advances",
        "payable": "Bills to pay",
        "operating": "Running costs",
        "sell": "Total sell",
        "goods": "Goods bought",
        "collection": "Baki collected",
        "end_cash": "Cash at period end",
        "day_table": "Day-wise cash movement",
        "date": "Date",
        "in": "Cash in",
        "out": "Cash out",
        "loan_cap": "Loan / capital",
        "baki_book": "Baki book",
        "drawer": "In the drawer",
        "total": "Total",
        "generated": "Generated",
        "page": "Page",
        "footer": "FinCore ERP — Summary",
        "note": "Every figure is read straight from the books.",
    },
}


def _q(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(CENT)
    except Exception:
        return ZERO


def _signed(value) -> str:
    """-৳500.00 / ৳600.00 — the minus sits in front of the sign."""
    amount = Decimal(str(value or 0)).quantize(CENT)
    if amount < 0:
        return f"-৳{-amount:,.2f}"
    return f"৳{amount:,.2f}"


def money(value: Any) -> str:
    """Local money formatter (the minus sits in front of ৳)."""
    return _signed(value)


def build_summary_export(
    business,
    dfrom: date,
    dto: date,
    lang: str = "bn",
) -> dict[str, Any]:
    """The data behind the summary export — the same numbers the page shows."""
    from apps.cashbook import services as cashbook
    from apps.web.summary import day_rows, month_rows, period_totals

    lang = "en" if str(lang).lower().startswith("en") else "bn"
    labels = SUMMARY_LABELS[lang]

    position = cashbook.business_position(business)
    profit = cashbook.profit_breakdown(business, dfrom, dto)
    totals = period_totals(business, dfrom, dto)
    raw_rows = day_rows(business, dfrom, dto)
    raw_months = month_rows(business, dto.year)

    have = [
        {"key": "cash", "label": labels["cash"], "value": position["cash"],
         "display": money(position["cash"]), "note": ""},
        {"key": "wallet", "label": labels["wallet"], "value": position["wallet"],
         "display": money(position["wallet"]), "note": ""},
        {"key": "due", "label": labels["due"], "value": position["due"],
         "display": money(position["due"]), "note": ""},
    ]
    owe = [
        {"key": "loan", "label": labels["loan"],
         "value": position["loan_outstanding"],
         "display": money(position["loan_outstanding"]),
         "note": f"{money(position['loan_received'])} / {money(position['loan_returned'])}"},
        {"key": "advances", "label": labels["advances"],
         "value": position["advances"], "display": money(position["advances"]),
         "note": ""},
        {"key": "payable", "label": labels["payable"],
         "value": position["payable"], "display": money(position["payable"]),
         "note": ""},
    ]

    rows: list[dict[str, Any]] = []
    for index, r in enumerate(raw_rows, start=1):
        rows.append({
            "sl": index,
            "date": r["date"],
            "date_label": r["date"].strftime("%d %b %Y"),
            "cash_in": r["cash_in"], "cash_in_display": money(r["cash_in"]),
            "cash_out": r["cash_out"], "cash_out_display": money(r["cash_out"]),
            "loan_cap": (f"+{money(r['loan_in'] + r['invest_in'])} / "
                         f"−{money(r['loan_out'] + r['invest_out'])}"),
            "baki": (f"+{money(r['due_added'])} / −{money(r['due_collected'])}"),
            "closing_cash": r["closing_cash"],
            "closing_cash_display": money(r["closing_cash"]),
            "has_data": r["has_data"],
        })

    months = [{
        "month": m["month"],
        "label": m["first_day"].strftime("%b %Y"),
        "closing_cash": m["closing_cash"],
        "closing_cash_display": money(m["closing_cash"]),
        "money_in": m["money_in"], "money_in_display": money(m["money_in"]),
    } for m in raw_months]

    period = [
        {"label": labels["sell"], "value": profit["sales"],
         "display": money(profit["sales"])},
        {"label": labels["goods"], "value": profit["goods_bought"],
         "display": money(profit["goods_bought"])},
        {"label": labels["collection"], "value": totals["due_collected"],
         "display": money(totals["due_collected"])},
        {"label": labels["end_cash"],
         "value": raw_rows[-1]["closing_cash"] if raw_rows else ZERO,
         "display": money(raw_rows[-1]["closing_cash"]) if raw_rows else money(0)},
    ]

    now = timezone.localtime()
    period_label = f"{dfrom.strftime('%d %b %Y')} → {dto.strftime('%d %b %Y')}"
    return {
        "business": business.name,
        "title": labels["title"],
        "subtitle": period_label,
        "date_label": period_label,
        "period_label": period_label,
        "generated_label": now.strftime("%d %b %Y, %I:%M %p"),
        "filename": f"summary-{dfrom.isoformat()}_{dto.isoformat()}",
        "labels": labels,
        "have": have,
        "have_total": position["have"],
        "have_total_display": money(position["have"]),
        # split for the reader: money in hand vs money still to come
        "cash_in_hand": position["cash_in_hand"],
        "cash_in_hand_display": money(position["cash_in_hand"]),
        "due_display": money(position["due"]),
        "owe": owe,
        "owe_total": position["owe"],
        "owe_total_display": money(position["owe"]),
        "net": position["net"],
        "net_display": money(position["net"]),
        # the investors' money is the owner's own — information only
        "invest": {
            "value": position["invest_outstanding"],
            "display": money(position["invest_outstanding"]),
            "received": money(position["invest_received"]),
            "returned": money(position["invest_returned"]),
            "note": labels["invest_note"],
        },
        "period": period,
        "rows": rows,
        "months": months,
        "totals": {
            "cash_in": totals["cash_in"], "cash_in_display": money(totals["cash_in"]),
            "cash_out": totals["cash_out"], "cash_out_display": money(totals["cash_out"]),
            "loan_cap": (f"+{money(totals['loan_in'] + totals['invest_in'])} / "
                         f"−{money(totals['loan_out'] + totals['invest_out'])}"),
            "baki": (f"+{money(totals['due_added'])} / −{money(totals['due_collected'])}"),
            "closing_cash": raw_rows[-1]["closing_cash"] if raw_rows else ZERO,
            "closing_cash_display": (money(raw_rows[-1]["closing_cash"])
                                     if raw_rows else money(0)),
        },
    }


# ------------------------------------------------------------------ PDF ---

def export_summary_pdf(data: dict[str, Any]) -> bytes:
    """PDF — the styled HTML transcript via headless Chrome/Edge, with a
    portable fpdf2 fallback."""
    browser_pdf = _render_pdf_with_browser(
        data, template_name="web/summary_export_pdf.html",
        prefix="fincore-summary-")
    if browser_pdf:
        return browser_pdf
    return _summary_pdf_fallback(data)


def _summary_pdf_fallback(data: dict[str, Any]) -> bytes:
    from fpdf import FPDF

    labels = data["labels"]

    class SummaryPDF(FPDF):
        def __init__(self):
            super().__init__(orientation="L", unit="mm", format="A4")
            self.set_auto_page_break(auto=True, margin=18)
            self.set_margins(12, 12, 12)
            self.alias_nb_pages()
            self.add_font("Noto", "", str(FONT_REGULAR))
            self.add_font("Noto", "B", str(FONT_BOLD))
            self.set_text_shaping(True)
            self.set_title(f"{data['title']} — {data['business']}")
            self.set_author("FinCore ERP")

        def header(self):
            self.set_fill_color(28, 18, 5)
            self.rect(0, 0, self.w, 24, "F")
            self.set_fill_color(201, 150, 27)
            self.rect(0, 24, self.w, 1.2, "F")
            self.set_xy(self.l_margin, 6)
            self.set_font("Noto", "B", 16)
            self.set_text_color(255, 255, 255)
            self.cell(150, 8, data["title"])
            self.set_xy(self.l_margin, 14)
            self.set_font("Noto", "", 9)
            self.set_text_color(216, 189, 138)
            self.cell(150, 6, data["subtitle"])
            self.set_xy(self.w - self.r_margin - 90, 8)
            self.set_font("Noto", "", 7.5)
            self.set_text_color(163, 140, 100)
            self.cell(90, 5, "BUSINESS", align="R")
            self.set_xy(self.w - self.r_margin - 90, 13)
            self.set_font("Noto", "B", 10)
            self.set_text_color(244, 236, 221)
            self.cell(90, 6, data["business"], align="R")
            self.set_y(32)

        def footer(self):
            self.set_y(-12)
            self.set_draw_color(226, 232, 240)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin,
                      self.get_y())
            self.set_y(-9.5)
            self.set_font("Noto", "", 7.3)
            self.set_text_color(100, 116, 139)
            self.cell(0, 5, f"{labels['footer']}  ·  {data['generated_label']}")
            self.set_xy(self.w - self.r_margin - 30, self.get_y())
            self.cell(30, 5, f"{labels['page']} {self.page_no()}", align="R")

    pdf = SummaryPDF()
    pdf.add_page()

    def block(title, items, total_display):
        pdf.set_font("Noto", "B", 11)
        pdf.set_text_color(11, 37, 69)
        pdf.cell(0, 7, title)
        pdf.ln(9)
        pdf.set_font("Noto", "", 9)
        for item in items:
            pdf.set_text_color(92, 107, 128)
            pdf.cell(90, 6, item["label"])
            pdf.set_font("Noto", "B", 9)
            pdf.set_text_color(11, 37, 69)
            pdf.cell(50, 6, item["display"], align="R")
            pdf.set_font("Noto", "", 8)
            pdf.set_text_color(140, 150, 170)
            pdf.cell(70, 6, item.get("note") or "", align="R")
            pdf.ln(6)
        pdf.set_font("Noto", "B", 9.5)
        pdf.set_text_color(11, 37, 69)
        pdf.cell(90, 7, labels["total"])
        pdf.cell(50, 7, total_display, align="R")
        pdf.ln(12)

    block(labels["have"], data["have"], data["have_total_display"])
    block(labels["owe"], data["owe"], data["owe_total_display"])

    pdf.set_font("Noto", "B", 14)
    pdf.set_text_color(11, 37, 69)
    pdf.cell(90, 9, labels["net"])
    pdf.set_font("Noto", "B", 14)
    pdf.set_text_color(180, 83, 9)
    pdf.cell(50, 9, data["net_display"], align="R")
    pdf.ln(14)

    # day table
    pdf.set_font("Noto", "B", 11)
    pdf.set_text_color(11, 37, 69)
    pdf.cell(0, 7, labels["day_table"])
    pdf.ln(9)
    widths = [30, 34, 34, 40, 40, 44]
    pdf.set_font("Noto", "B", 8.5)
    pdf.set_fill_color(11, 37, 69)
    pdf.set_text_color(255, 255, 255)
    for w, head in zip(widths, [labels["date"], labels["in"], labels["out"],
                                 labels["loan_cap"], labels["baki_book"],
                                 labels["drawer"]]):
        pdf.cell(w, 8, head, align="C", fill=True)
    pdf.ln(8)
    pdf.set_font("Noto", "", 8.2)
    pdf.set_text_color(30, 41, 59)
    for row in data["rows"]:
        if not row["has_data"]:
            continue
        pdf.cell(widths[0], 6, row["date_label"])
        pdf.cell(widths[1], 6, row["cash_in_display"], align="R")
        pdf.cell(widths[2], 6, row["cash_out_display"], align="R")
        pdf.cell(widths[3], 6, row["loan_cap"], align="R")
        pdf.cell(widths[4], 6, row["baki"], align="R")
        pdf.cell(widths[5], 6, row["closing_cash_display"], align="R")
        pdf.ln(6)

    output = pdf.output()
    return bytes(output)


# ----------------------------------------------------------------- XLSX ---

def export_summary_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    labels = data["labels"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"

    navy = "0B2545"
    gold = "B8860B"
    thin = Side(style="thin", color="E2E8F0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def title_row(text, row, span=6):
        ws.cell(row=row, column=1, value=text).font = Font(
            bold=True, size=14, color=navy)
        ws.merge_cells(start_row=row, start_column=1,
                       end_row=row, end_column=span)
        return row + 1

    row = 1
    ws.cell(row=row, column=1, value=data["business"]).font = Font(
        bold=True, size=16, color=navy)
    row += 1
    ws.cell(row=row, column=1, value=data["title"]).font = Font(
        bold=True, size=13, color=gold)
    row += 1
    ws.cell(row=row, column=1, value=data["subtitle"]).font = Font(
        size=10, color="5C6B80")
    row += 2

    def block(title, items, total, total_display, tag_fill):
        nonlocal row
        row = title_row(title, row)
        ws.cell(row=row, column=1, value=labels["total"]).font = Font(
            bold=True, size=10, color=navy)
        ws.cell(row=row, column=3, value=total_display).font = Font(
            bold=True, size=11, color=navy)
        ws.cell(row=row, column=1).fill = PatternFill("solid", fgColor=tag_fill)
        row += 1
        for item in items:
            ws.cell(row=row, column=1, value=item["label"]).font = Font(
                size=10, color="5C6B80")
            cell = ws.cell(row=row, column=3, value=float(item["value"]))
            cell.font = Font(bold=True, size=10, color=navy)
            cell.number_format = "#,##0.00"
            cell.alignment = Alignment(horizontal="right")
            if item.get("note"):
                ws.cell(row=row, column=4, value=item["note"]).font = Font(
                    size=9, color="94A3B8")
            row += 1
        row += 1

    block(labels["have"], data["have"], data["have_total"],
          data["have_total_display"], "F0FDF4")
    block(labels["owe"], data["owe"], data["owe_total"],
          data["owe_total_display"], "FEF2F2")

    ws.cell(row=row, column=1, value=labels["net"]).font = Font(
        bold=True, size=13, color=navy)
    net_cell = ws.cell(row=row, column=3, value=float(data["net"]))
    net_cell.font = Font(bold=True, size=14, color="B45309")
    net_cell.number_format = "#,##0.00"
    net_cell.alignment = Alignment(horizontal="right")
    row += 2

    row = title_row(labels["period"], row)
    for item in data["period"]:
        ws.cell(row=row, column=1, value=item["label"]).font = Font(
            size=10, color="5C6B80")
        cell = ws.cell(row=row, column=3, value=float(item["value"]))
        cell.font = Font(bold=True, size=10, color=navy)
        cell.number_format = "#,##0.00"
        cell.alignment = Alignment(horizontal="right")
        row += 1
    row += 1

    row = title_row(labels["day_table"], row)
    headers = [labels["date"], labels["in"], labels["out"], labels["loan_cap"],
               labels["baki_book"], labels["drawer"]]
    for col, head in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=head)
        cell.font = Font(bold=True, size=10, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.alignment = Alignment(horizontal="center")
        cell.border = border
    row += 1
    for r in data["rows"]:
        values = [r["date_label"], r["cash_in"], r["cash_out"], r["loan_cap"],
                  r["baki"], r["closing_cash"]]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.border = border
            if isinstance(value, (int, float)):
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")
        row += 1

    totals = data["totals"]
    total_values = [labels["total"], totals["cash_in"], totals["cash_out"],
                    totals["loan_cap"], totals["baki"], totals["closing_cash"]]
    for col, value in enumerate(total_values, start=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font = Font(bold=True, size=10, color=navy)
        cell.fill = PatternFill("solid", fgColor="F5F5F4")
        cell.border = border
        if isinstance(value, (int, float)):
            cell.number_format = "#,##0.00"
            cell.alignment = Alignment(horizontal="right")

    for col, width in enumerate([34, 18, 18, 24, 24, 22], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"

    from io import BytesIO
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


# ----------------------------------------------------------------- DOCX ---

def export_summary_docx(data: dict[str, Any]) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    labels = data["labels"]
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Noto Sans Bengali"
    style.font.size = Pt(9.5)

    def para(text, size=9.5, bold=False, color=(30, 41, 59), space=2):
        p = doc.add_paragraph()
        run = p.add_run(str(text))
        run.font.size = Pt(size)
        run.bold = bold
        run.font.color.rgb = RGBColor(*color)
        p.paragraph_format.space_after = Pt(space)
        return p

    para(data["business"], size=16, bold=True, color=(11, 37, 69))
    para(data["title"], size=13, bold=True, color=(184, 134, 11))
    para(data["subtitle"], size=9, color=(92, 107, 128), space=8)

    def block(title, items, total_display):
        para(title, size=11, bold=True, color=(11, 37, 69), space=3)
        table = doc.add_table(rows=1, cols=3)
        table.style = "Light Grid Accent 1"
        head = table.rows[0].cells
        head[0].text = labels["total"]
        head[2].text = labels["total"]
        for item in items:
            cells = table.add_row().cells
            cells[0].text = item["label"]
            cells[1].text = item.get("note") or ""
            cells[2].text = item["display"]
        cells = table.add_row().cells
        cells[0].text = labels["total"]
        cells[1].text = ""
        cells[2].text = total_display
        for run in cells[2].paragraphs[0].runs:
            run.bold = True
        doc.add_paragraph()

    block(labels["have"], data["have"], data["have_total_display"])
    block(labels["owe"], data["owe"], data["owe_total_display"])

    p = doc.add_paragraph()
    run = p.add_run(f"{labels['net']}: {data['net_display']}")
    run.bold = True
    run.font.size = Pt(13)
    run.font.color.rgb = RGBColor(11, 37, 69)
    doc.add_paragraph()

    para(labels["day_table"], size=11, bold=True, color=(11, 37, 69), space=4)
    table = doc.add_table(rows=1, cols=6)
    table.style = "Light Grid Accent 1"
    headers = [labels["date"], labels["in"], labels["out"], labels["loan_cap"],
               labels["baki_book"], labels["drawer"]]
    for cell, head in zip(table.rows[0].cells, headers):
        cell.text = head
    for r in data["rows"]:
        cells = table.add_row().cells
        values = [r["date_label"], r["cash_in_display"], r["cash_out_display"],
                  r["loan_cap"], r["baki"], r["closing_cash_display"]]
        for cell, value in zip(cells, values):
            cell.text = str(value)

    para(f"{labels['footer']}  ·  {data['generated_label']}", size=8,
         color=(140, 150, 170), space=0)

    from io import BytesIO
    output = BytesIO()
    doc.save(output)
    return output.getvalue()
