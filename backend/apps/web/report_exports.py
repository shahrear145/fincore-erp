"""রিপোর্ট — PDF/Excel exports.

তিন ধরনের রিপোর্ট, প্রতিটি PDF ও Excel-এ:

1. ``pnl_report_pdf/xlsx`` — লাভ-ক্ষতি (Income Statement) for a date range
   (monthly or yearly — the caller supplies from/to dates).

2. ``cashflow_report_pdf/xlsx`` — নগদের আনাগোনা (Cash Flow) — wallet accounting
   opening/inflow/outflow/closing plus day-wise movement.

3. ``partner_statement_pdf/xlsx`` — পার্টনার স্টেটমেন্ট — one partner book's
   opening balance, given (বাকি), collected (আদায়) and closing balance, over a
   date range.

BNGL-ল্যাটিন ফন্ট বান্ডেলড (apps/web/fonts) — বাংলা ঠিকমতো ছাপা হয়।
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import os
from typing import Any

from django.utils import timezone

from apps.transactions.models import (
    CUSTOMER_REQUIRED_TYPES, Transaction, TransactionStatus, TransactionType,
)
from apps.web.exports import PDF_CONTENT_TYPE, _clean_text, money, short_customer_id
from apps.web import receipt_exports as R

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

LABEL = {
    "bn": {
        "business": "ব্যবসা", "period": "সময়কাল", "generated": "তৈরি",
        "sl": "ক্রমিক", "date": "তারিখ", "name": "নাম", "type": "ধরন",
        "amount": "টাকা", "balance": "বাকি", "total": "মোট",
        "page": "পৃষ্ঠা", "amount_note": "সব টাকা বাংলাদেশি টাকায় (৳)",
    },
    "en": {
        "business": "Business", "period": "Period", "generated": "Generated",
        "sl": "SL", "date": "Date", "name": "Name", "type": "Type",
        "amount": "Amount", "balance": "Balance", "total": "Total",
        "page": "Page", "amount_note": "All amounts are in Bangladeshi Taka (৳)",
    },
}


def _lang(language: str) -> str:
    return "en" if str(language).lower().startswith("en") else "bn"


def _d(value) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(CENT)
    except Exception:
        return ZERO


# ---------------------------------------------------------------------------
# data builders
# ---------------------------------------------------------------------------
def build_pnl_report(business, dfrom: date, dto: date, lang: str = "bn") -> dict:
    from apps.accounting.reports import income_statement

    lang = _lang(lang)
    result = income_statement(business, from_date=dfrom, to_date=dto)
    revenues = [{
        "code": row["code"],
        "name": row["name"],
        "amount": _d(row["amount"]),
    } for row in result["revenues"]]
    expenses = [{
        "code": row["code"],
        "name": row["name"],
        "amount": _d(row["amount"]),
    } for row in result["expenses"]]
    total_revenue = _d(result["total_revenue"])
    total_expenses = _d(result["total_expenses"])
    net = total_revenue - total_expenses

    now = timezone.localtime()
    period_label = f"{dfrom.strftime('%d %b %Y')} → {dto.strftime('%d %b %Y')}"
    return {
        "business": business.name,
        "business_phone": (getattr(business, "contact_phone", "") or "").strip(),
        "business_address": (getattr(business, "address", "") or "").strip(),
        "business_logo": R._logo_path(business),
        "dfrom": dfrom, "dto": dto,
        "date_label": period_label,
        "generated_label": now.strftime("%d %b %Y, %I:%M %p"),
        "revenues": revenues,
        "expenses": expenses,
        "total_revenue": total_revenue,
        "total_revenue_display": money(total_revenue),
        "total_expenses": total_expenses,
        "total_expenses_display": money(total_expenses),
        "net": net,
        "net_display": money(net),
        "is_profit": net >= 0,
        "lang": lang,
        "en": lang == "en",
        "filename": f"profit-loss-{dfrom.isoformat()}_{dto.isoformat()}",
        "labels": LABEL[lang],
    }


def build_cashflow_report(business, dfrom: date, dto: date,
                          lang: str = "bn") -> dict:
    from apps.accounting.reports import cash_flow

    lang = _lang(lang)
    result = cash_flow(business, from_date=dfrom, to_date=dto)
    wallets = [{
        "name": row["name"],
        "code": row["code"],
        "opening": _d(row["opening"]),
        "opening_display": money(row["opening"]),
        "inflow": _d(row["inflow"]),
        "inflow_display": money(row["inflow"]),
        "outflow": _d(row["outflow"]),
        "outflow_display": money(row["outflow"]),
        "closing": _d(row["closing"]),
        "closing_display": money(row["closing"]),
    } for row in result["wallets"]]
    daily = [{
        "date": row["date"].strftime("%d %b %Y") if hasattr(row["date"], "strftime") else row["date"],
        "inflow": _d(row["inflow"]),
        "inflow_display": money(row["inflow"]),
        "outflow": _d(row["outflow"]),
        "outflow_display": money(row["outflow"]),
        "net": _d(row["net"]),
        "net_display": money(row["net"]),
        "closing": _d(row["closing_wallet_balance"]),
        "closing_display": money(row["closing_wallet_balance"]),
    } for row in result["daily"]]

    total_in = _d(result["total_money_in"])
    total_out = _d(result["total_money_out"])
    net = _d(result["net_change"])

    now = timezone.localtime()
    period_label = f"{dfrom.strftime('%d %b %Y')} → {dto.strftime('%d %b %Y')}"
    return {
        "business": business.name,
        "business_phone": (getattr(business, "contact_phone", "") or "").strip(),
        "business_address": (getattr(business, "address", "") or "").strip(),
        "business_logo": R._logo_path(business),
        "dfrom": dfrom, "dto": dto,
        "date_label": period_label,
        "generated_label": now.strftime("%d %b %Y, %I:%M %p"),
        "wallets": wallets,
        "daily": daily,
        "total_money_in": total_in,
        "total_money_in_display": money(total_in),
        "total_money_out": total_out,
        "total_money_out_display": money(total_out),
        "net_change": net,
        "net_change_display": money(net),
        "lang": lang,
        "en": lang == "en",
        "filename": f"cash-flow-{dfrom.isoformat()}_{dto.isoformat()}",
        "labels": LABEL[lang],
    }


def build_partner_statement(business, partner, dfrom: date, dto: date,
                            lang: str = "bn") -> dict:
    lang = _lang(lang)
    txs = (Transaction.objects
           .filter(business=business, partner=partner,
                   transaction_type__in=CUSTOMER_REQUIRED_TYPES)
           .exclude(status=TransactionStatus.REVERSED)
           .select_related("customer")
           .order_by("transaction_date", "created_at"))

    opening = ZERO
    tot_given = ZERO
    tot_collected = ZERO
    for t in txs.filter(transaction_date__lt=dfrom):
        opening += R._txn_delta(t)

    rows = []
    running = opening
    index = 0
    for t in txs.filter(transaction_date__gte=dfrom, transaction_date__lte=dto):
        index += 1
        delta = R._txn_delta(t)
        if delta > 0:
            tot_given += delta
        else:
            tot_collected += -delta
        running += delta
        rows.append({
            "sl": index,
            "date": t.transaction_date.strftime("%d %b %Y"),
            "customer": _clean_text(getattr(getattr(t, "customer", None), "name", "") or ""),
            "type": R._signed_type(t, lang),
            "note": _clean_text(t.description),
            "amount": money(t.amount),
            "amount_value": _d(t.amount),
            "running": money(running),
            "delta": money(abs(delta)),
            "delta_signed": delta,
        })

    closing = opening + tot_given - tot_collected
    now = timezone.localtime()
    period_label = f"{dfrom.strftime('%d %b %Y')} → {dto.strftime('%d %b %Y')}"
    return {
        "business": business.name,
        "business_phone": (getattr(business, "contact_phone", "") or "").strip(),
        "business_address": (getattr(business, "address", "") or "").strip(),
        "business_logo": R._logo_path(business),
        "partner": partner.name,
        "dfrom": dfrom, "dto": dto,
        "date_label": period_label,
        "generated_label": now.strftime("%d %b %Y, %I:%M %p"),
        "rows": rows,
        "count": len(rows),
        "opening": opening,
        "opening_display": money(opening),
        "total_given": tot_given,
        "total_given_display": money(tot_given),
        "total_collected": tot_collected,
        "total_collected_display": money(tot_collected),
        "closing": closing,
        "closing_display": money(closing),
        "lang": lang,
        "en": lang == "en",
        "filename": f"partner-statement-{short_customer_id(partner.id)}-{dfrom.isoformat()}_{dto.isoformat()}",
        "labels": LABEL[lang],
    }


# ---------------------------------------------------------------------------
# PDF builders — fpdf2 + bundled Noto Bengali, shared drawing helpers from R
# ---------------------------------------------------------------------------
def _section_title(pdf, text: str, color: tuple[int, int, int] = R._NAVY) -> None:
    y = pdf.get_y() + 2
    pdf.set_xy(pdf.l_margin, y)
    pdf.set_fill_color(248, 250, 252)
    pdf.rect(pdf.l_margin, y, pdf.epw, 7.5, "F")
    pdf.set_xy(pdf.l_margin + 3, y + 1.6)
    pdf.set_font("Noto", "B", 9)
    pdf.set_text_color(*color)
    pdf.cell(0, 5, text)
    pdf.set_y(y + 9.5)


def _total_row(pdf, widths: list[float], cells: list[str],
               aligns: list[str] | None = None) -> None:
    y = pdf.get_y()
    x = pdf.l_margin
    aligns = aligns or ["L"] * len(cells)
    pdf.set_fill_color(240, 253, 250)
    pdf.rect(pdf.l_margin, y, sum(widths), 7.2, "F")
    pdf.set_draw_color(*R._LINE)
    pdf.line(pdf.l_margin, y, pdf.l_margin + sum(widths), y)
    pdf.line(pdf.l_margin, y + 7.2, pdf.l_margin + sum(widths), y + 7.2)
    pdf.set_font("Noto", "B", 8.2)
    pdf.set_text_color(*R._NAVY)
    for text, width, align in zip(cells, widths, aligns):
        pdf.set_xy(x, y + 1.4)
        pdf.cell(width, 4.6, str(text), align=align)
        x += width
    pdf.set_y(y + 8.4)


def pnl_pdf(data: dict[str, Any]) -> bytes:
    from apps.web.pdf_reports import pnl_pdf as premium_pdf
    return premium_pdf(data)


def cashflow_pdf(data: dict[str, Any]) -> bytes:
    from apps.web.pdf_reports import cashflow_pdf as premium_pdf
    return premium_pdf(data)


def partner_statement_pdf(data: dict[str, Any]) -> bytes:
    from apps.web.pdf_reports import partner_statement_pdf as premium_pdf
    return premium_pdf(data)


# ---------------------------------------------------------------------------
# XLSX builders — openpyxl, navy/gold house style
# ---------------------------------------------------------------------------
_NAVY = "0B2545"
_GOLD = "B8860B"
_GREY = "5C6B80"
_LINE = "E2E8F0"


def _xlsx_workbook(title: str):
    from openpyxl import Workbook
    from openpyxl.styles import Border, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    thin = Side(style="thin", color=_LINE)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    return wb, ws, border


def _xlsx_header(ws, data: dict[str, Any], sheet_name: str, widths: list[float]) -> None:
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    ws.title = sheet_name
    ws["A1"] = data["business"]
    ws["A1"].font = Font(bold=True, size=16, color=_NAVY)
    ws["A2"] = sheet_name
    ws["A2"].font = Font(bold=True, size=13, color=_GOLD)
    ws["A3"] = data["date_label"]
    ws["A3"].font = Font(size=10, color=_GREY)
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A5"


def _xlsx_table_header(ws, row: int, headers: list[str], border) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    for col, head in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=head)
        cell.font = Font(bold=True, size=10, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=_NAVY)
        cell.alignment = Alignment(horizontal="center")
        cell.border = border


def _xlsx_row(ws, row: int, values: list[Any], border,
              number_cols: tuple[int, ...] = (),
              fill: bool = False) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    for col, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.border = border
        if isinstance(value, Decimal):
            cell.value = float(value)
        if col in number_cols:
            cell.number_format = "#,##0.00"
            cell.alignment = Alignment(horizontal="right")
        if fill:
            cell.fill = PatternFill("solid", fgColor="F8FAFC")


def _xlsx_total_row(ws, row: int, values: list[Any], border) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    for col, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.border = border
        cell.font = Font(bold=True, size=10, color=_NAVY)
        cell.fill = PatternFill("solid", fgColor="F0FDFA")
        if isinstance(value, Decimal):
            cell.value = float(value)
            cell.number_format = "#,##0.00"
            cell.alignment = Alignment(horizontal="right")


def pnl_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl.styles import Alignment, Font
    from io import BytesIO

    wb, ws, border = _xlsx_workbook("Profit & Loss" if data["en"] else "লাভ-ক্ষতি")
    _xlsx_header(ws, data, ws.title, [52, 26])

    row = 5
    ws.cell(row=row, column=1, value=("Revenue" if data["en"] else "আয়")).font = Font(
        bold=True, size=11, color=_GOLD)
    row += 1
    _xlsx_table_header(ws, row,
                       (["Account", "Amount"] if data["en"] else ["একাউন্ট", "টাকা"]),
                       border)
    row += 1
    for item in data["revenues"]:
        _xlsx_row(ws, row,
                  [f"{item['code']} · {item['name']}", item["amount"]],
                  border, number_cols=(2,), fill=row % 2 == 0)
        row += 1
    _xlsx_total_row(ws, row,
                    [("Total revenue" if data["en"] else "মোট আয়"),
                     data["total_revenue"]], border)
    row += 2
    ws.cell(row=row, column=1, value=("Expenses" if data["en"] else "ব্যয়")).font = Font(
        bold=True, size=11, color=_GOLD)
    row += 1
    _xlsx_table_header(ws, row,
                       (["Account", "Amount"] if data["en"] else ["একাউন্ট", "টাকা"]),
                       border)
    row += 1
    for item in data["expenses"]:
        _xlsx_row(ws, row,
                  [f"{item['code']} · {item['name']}", item["amount"]],
                  border, number_cols=(2,), fill=row % 2 == 0)
        row += 1
    _xlsx_total_row(ws, row,
                    [("Total expenses" if data["en"] else "মোট ব্যয়"),
                     data["total_expenses"]], border)
    row += 2
    ws.cell(row=row, column=1,
            value=(("Net profit" if data["en"] else "নিট লাভ") if data["is_profit"]
                   else ("Net loss" if data["en"] else "নিট ক্ষতি"))).font = Font(
        bold=True, size=13, color=_NAVY)
    net_cell = ws.cell(row=row, column=2, value=float(data["net"]))
    net_cell.font = Font(bold=True, size=14, color="B45309")
    net_cell.number_format = "#,##0.00"
    net_cell.alignment = Alignment(horizontal="right")

    ws.column_dimensions["B"].width = 26
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def cashflow_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    wb, ws, border = _xlsx_workbook("Cash Flow" if data["en"] else "নগদের আনাগোনা")
    _xlsx_header(ws, data, ws.title, [36, 16, 16, 16, 16])

    row = 5
    _xlsx_table_header(ws, row,
                       (["Account", "Opening", "Money In", "Money Out", "Closing"] if data["en"]
                        else ["হিসাব", "শুরু", "জমা", "খরচ", "শেষ"]),
                       border)
    row += 1
    for item in data["wallets"]:
        _xlsx_row(ws, row,
                  [item["name"], item["opening"], item["inflow"],
                   item["outflow"], item["closing"]],
                  border, number_cols=(2, 3, 4, 5), fill=row % 2 == 0)
        row += 1
    _xlsx_total_row(ws, row,
                    [("Total" if data["en"] else "মোট"),
                     sum((w["opening"] for w in data["wallets"]), ZERO),
                     data["total_money_in"], data["total_money_out"],
                     sum((w["closing"] for w in data["wallets"]), ZERO)],
                    border)
    row += 2
    ws.cell(row=row, column=1,
            value=("Net change" if data["en"] else "নিট বৃদ্ধি/হ্রাস")).font = Font(
        bold=True, size=13, color=_NAVY)
    net_cell = ws.cell(row=row, column=2, value=float(data["net_change"]))
    net_cell.font = Font(bold=True, size=13, color="B45309")
    net_cell.number_format = "#,##0.00"
    net_cell.alignment = Alignment(horizontal="right")

    if data["daily"]:
        row += 2
        ws.cell(row=row, column=1,
                value=("Day by day" if data["en"] else "দিন অনুযায়ী")).font = Font(
            bold=True, size=11, color=_GOLD)
        row += 1
        _xlsx_table_header(ws, row,
                           (["Date", "In", "Out", "Net", "Closing"] if data["en"]
                            else ["তারিখ", "জমা", "খরচ", "নিট", "শেষ"]),
                           border)
        row += 1
        for item in data["daily"]:
            _xlsx_row(ws, row,
                      [item["date"], item["inflow"], item["outflow"],
                       item["net"], item["closing"]],
                      border, number_cols=(2, 3, 4, 5), fill=row % 2 == 0)
            row += 1

    for col, width in enumerate([36, 16, 16, 16, 16], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def partner_statement_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter
    from io import BytesIO

    wb, ws, border = _xlsx_workbook(
        "Partner Statement" if data["en"] else "পার্টনার স্টেটমেন্ট")
    _xlsx_header(ws, data, ws.title, [30, 15, 18, 13, 32, 15, 15])

    row = 5
    ws.cell(row=row, column=1, value=("Partner" if data["en"] else "পার্টনার")).font = Font(
        bold=True, size=11, color=_GOLD)
    ws.cell(row=row, column=2, value=data["partner"]).font = Font(
        bold=True, size=11, color=_NAVY)
    row += 1

    stat_rows = [
        (("Opening baki" if data["en"] else "শুরু বাকি"), data["opening"]),
        (("Baki given" if data["en"] else "বাকি দেওয়া"), data["total_given"]),
        (("Collected" if data["en"] else "আদায়"), data["total_collected"]),
        (("Closing baki" if data["en"] else "শেষের বাকি"), data["closing"]),
    ]
    for label, value in stat_rows:
        ws.cell(row=row, column=1, value=label).font = Font(size=10, color=_GREY)
        cell = ws.cell(row=row, column=2, value=float(value))
        cell.font = Font(bold=True, size=10, color=_NAVY)
        cell.number_format = "#,##0.00"
        cell.alignment = Alignment(horizontal="right")
        row += 1
    row += 1

    _xlsx_table_header(ws, row,
                       (["SL", "Date", "Customer", "Type", "Note", "Amount", "Balance"] if data["en"]
                        else ["ক্র.", "তারিখ", "গ্রাহক", "ধরন", "বিবরণ", "টাকা", "বাকি"]),
                       border)
    row += 1
    for item in data["rows"]:
        _xlsx_row(ws, row,
                  [item["sl"], item["date"], item["customer"], item["type"],
                   item["note"], item["amount_value"], _d(item["running"].replace("৳", "").replace(",", ""))],
                  border, number_cols=(6, 7), fill=row % 2 == 0)
        row += 1

    for col, width in enumerate([30, 15, 18, 13, 32, 15, 15], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    output = BytesIO()
    wb.save(output)
    return output.getvalue()
