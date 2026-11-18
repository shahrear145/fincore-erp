"""হিসাব কমান্ড সেন্টার — PDF / Excel / Word exports.

The export is a *proof document*: the composition, the net position, the
health score, every audit finding and the due/loan breakdown.  It is built
from the same ``build_statement`` data the page shows, so a printed copy can
never disagree with the screen.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from typing import Any

from django.utils import timezone

from apps.web.exports import FONT_BOLD, FONT_REGULAR
from apps.web.position_statement import money

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

LABELS: dict[str, dict[str, str]] = {
    "bn": {
        "title": "হিসাব কমান্ড সেন্টার",
        "subtitle": "নিট অবস্থানের পূর্ণ বিবরণ ও অডিট",
        "business": "ব্যবসা",
        "as_of": "যত তারিখ পর্যন্ত",
        "generated": "তৈরি",
        "composition": "নিট অবস্থান কীভাবে হলো",
        "item": "খাত",
        "amount": "টাকা",
        "net": "নিট অবস্থান",
        "health": "স্বাস্থ্য স্কোর",
        "audit": "অডিট",
        "clean": "সব চেক পরিষ্কার",
        "due": "কাস্টমারের বাকি — পার্টনার অনুযায়ী",
        "loans": "ঋণ — ঋণদাতা অনুযায়ী",
        "invest": "বিনিয়োগকারীর মূল (শুধু তথ্য)",
        "partner": "পার্টনার",
        "customer": "গ্রাহক",
        "phone": "ফোন",
        "opening": "প্রারম্ভিক",
        "advance": "অগ্রিম",
        "due_col": "বর্তমান বাকি",
        "lender": "ঋণদাতা",
        "received": "নেওয়া",
        "returned": "ফেরত",
        "outstanding": "বাকি",
        "advances": "কাস্টমারের অগ্রিম",
        "payable": "খরচ বাকি",
        "severity": "তীব্রতা",
        "finding": "বিষয়",
        "detail": "বিবরণ",
        "score": "স্কোর",
        "part": "ভাগ",
        "note": "নোট",
        "total": "মোট",
        "page": "পৃষ্ঠা",
        "footer": "FinCore ERP — হিসাব কমান্ড সেন্টার",
        "readonly": "এটি শুধু পড়ার ডকুমেন্ট — কোনো লেনদেন পোস্ট বা সম্পাদনা করা হয়নি।",
        "high": "উচ্চ", "medium": "মাঝারি", "low": "নিম্ন",
        "cash": "ড্রয়ারের নগদ", "wallet": "বিকাশ / নগদ / ব্যাংক",
        "customer_due": "কাস্টমারের বাকি", "loans_short": "ঋণ",
        "advances_short": "অগ্রিম", "payable_short": "খরচ বাকি",
    },
    "en": {
        "title": "Accounts Command Center",
        "subtitle": "Full position statement & audit",
        "business": "Business",
        "as_of": "As of",
        "generated": "Generated",
        "composition": "How the net position is built",
        "item": "Item",
        "amount": "Amount",
        "net": "Net position",
        "health": "Health score",
        "audit": "Audit",
        "clean": "All checks are clean",
        "due": "Customer due — by partner",
        "loans": "Loans — by lender",
        "invest": "Investor capital (information only)",
        "partner": "Partner",
        "customer": "Customer",
        "phone": "Phone",
        "opening": "Opening",
        "advance": "Advance",
        "due_col": "Live due",
        "lender": "Lender",
        "received": "Received",
        "returned": "Returned",
        "outstanding": "Outstanding",
        "advances": "Customer advances",
        "payable": "Bills to pay",
        "severity": "Severity",
        "finding": "Finding",
        "detail": "Detail",
        "score": "Score",
        "part": "Part",
        "note": "Note",
        "total": "Total",
        "page": "Page",
        "footer": "FinCore ERP — Accounts Command Center",
        "readonly": "This is a read-only document — nothing was posted or edited.",
        "high": "High", "medium": "Medium", "low": "Low",
        "cash": "Drawer cash", "wallet": "bKash / Nagad / bank",
        "customer_due": "Customer due", "loans_short": "Loans",
        "advances_short": "Advances", "payable_short": "Bills to pay",
    },
}


def _lang(language: str) -> str:
    return "en" if str(language).lower().startswith("en") else "bn"


def build_position_export(business, statement: dict,
                          lang: str = "bn") -> dict[str, Any]:
    lang = _lang(lang)
    labels = LABELS[lang]
    as_of = statement["as_of"]

    def comp_label(key: str) -> str:
        return {
            "cash": labels["cash"],
            "wallet": labels["wallet"],
            "due": labels["customer_due"],
            "loans": labels["loans_short"],
            "advances": labels["advances_short"],
            "payable": labels["payable_short"],
        }.get(key, key)

    composition = [{
        "label": comp_label(item["key"]),
        "kind": item["kind"],
        "amount": item["amount"],
        "display": money(item["amount"]),
        "sign": "−" if item["kind"] == "owe" else "+",
    } for item in statement["composition"]]

    findings = []
    for finding in statement["audit"]["findings"]:
        findings.append({
            "severity": labels.get(finding["severity"], finding["severity"]),
            "title": (finding["title_en"] if lang == "en"
                      else finding["title_bn"]),
            "detail": (finding["detail_en"] if lang == "en"
                       else finding["detail_bn"]),
            "count": finding["count"],
            "amount": finding["amount"],
        })

    due_groups = []
    for group in statement["due"]["groups"]:
        due_groups.append({
            "partner": group["partner_name"],
            "total": group["total"],
            "total_display": money(group["total"]),
            "customers": [{
                "name": c["name"],
                "phone": c["phone"],
                "opening": c["opening"],
                "advance": c["advance"],
                "due": c["due"],
                "due_display": money(c["due"]),
            } for c in group["customers"]],
        })

    loans = [{
        "name": row["name"],
        "received": row["received"],
        "returned": row["returned"],
        "outstanding": row["outstanding"],
        "outstanding_display": money(row["outstanding"]),
    } for row in statement["loans"]["loans"]]

    investments = [{
        "name": row["name"],
        "received": row["received"],
        "returned": row["returned"],
        "outstanding": row["outstanding"],
        "outstanding_display": money(row["outstanding"]),
    } for row in statement["loans"]["investments"]]

    return {
        "business": business.name,
        "title": labels["title"],
        "subtitle": labels["subtitle"],
        "as_of_label": as_of.strftime("%d %b %Y"),
        "generated_label": timezone.localtime().strftime("%d %b %Y, %I:%M %p"),
        "filename": f"position-{as_of.isoformat()}",
        "labels": labels,
        "composition": composition,
        "net": statement["position"]["net"],
        "net_display": money(statement["position"]["net"]),
        "have": statement["position"]["have"],
        "owe": statement["position"]["owe"],
        "proof_ok": statement["proof"]["ok"],
        "health": statement["health"],
        "audit": {
            "findings": findings,
            "clean": statement["audit"]["clean"],
            "counts": statement["audit"]["counts"],
            "total": statement["audit"]["total"],
        },
        "due_groups": due_groups,
        "due_total": statement["due"]["total"],
        "due_total_display": money(statement["due"]["total"]),
        "loans": loans,
        "loan_total": statement["loans"]["loan_total"],
        "loan_total_display": money(statement["loans"]["loan_total"]),
        "investments": investments,
        "invest_total": statement["loans"]["invest_total"],
        "invest_total_display": money(statement["loans"]["invest_total"]),
        "advances_total": statement["position"]["advances"],
        "advances_display": money(statement["position"]["advances"]),
        "payable_total": statement["position"]["payable"],
        "payable_display": money(statement["position"]["payable"]),
    }


# --------------------------------------------------------------------- PDF --

def export_position_pdf(data: dict[str, Any]) -> bytes:
    from fpdf import FPDF

    labels = data["labels"]

    class PositionPDF(FPDF):
        def __init__(self):
            super().__init__(orientation="P", unit="mm", format="A4")
            self.set_auto_page_break(auto=True, margin=18)
            self.set_margins(12, 12, 12)
            self.alias_nb_pages()
            self.add_font("Noto", "", str(FONT_REGULAR))
            self.add_font("Noto", "B", str(FONT_BOLD))
            self.set_text_shaping(True)
            self.set_title(f"{data['title']} — {data['business']}")
            self.set_author("FinCore ERP")

        def header(self):
            self.set_fill_color(11, 37, 69)
            self.rect(0, 0, self.w, 27, "F")
            self.set_fill_color(20, 184, 166)
            self.rect(0, 27, self.w, 1.4, "F")
            self.set_xy(self.l_margin, 6)
            self.set_font("Noto", "B", 16)
            self.set_text_color(255, 255, 255)
            self.cell(120, 8, data["title"])
            self.set_xy(self.l_margin, 14.5)
            self.set_font("Noto", "", 9)
            self.set_text_color(180, 205, 230)
            self.cell(120, 6, data["subtitle"])
            self.set_xy(self.w - self.r_margin - 70, 8)
            self.set_font("Noto", "", 7.5)
            self.set_text_color(150, 180, 210)
            self.cell(70, 5, labels["business"], align="R")
            self.set_xy(self.w - self.r_margin - 70, 13)
            self.set_font("Noto", "B", 10)
            self.set_text_color(255, 255, 255)
            self.cell(70, 6, data["business"], align="R")
            self.set_y(34)

        def footer(self):
            self.set_y(-12)
            self.set_draw_color(226, 232, 240)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin,
                      self.get_y())
            self.set_y(-9.5)
            self.set_font("Noto", "", 7.2)
            self.set_text_color(100, 116, 139)
            self.cell(0, 5, f"{labels['footer']}  ·  {data['generated_label']}")
            self.set_xy(self.w - self.r_margin - 30, self.get_y())
            self.cell(30, 5, f"{labels['page']} {self.page_no()}", align="R")

    pdf = PositionPDF()
    pdf.add_page()

    def section(title: str):
        pdf.ln(2)
        pdf.set_font("Noto", "B", 11)
        pdf.set_text_color(11, 37, 69)
        pdf.cell(0, 7, title)
        pdf.ln(8)

    def row(label: str, value: str, bold: bool = False):
        pdf.set_font("Noto", "B" if bold else "", 9)
        pdf.set_text_color(30, 41, 59)
        pdf.cell(110, 6, label)
        pdf.set_font("Noto", "B", 9.5)
        pdf.set_text_color(11, 37, 69)
        pdf.cell(60, 6, value, align="R")
        pdf.ln(6)

    # composition
    section(labels["composition"])
    for item in data["composition"]:
        row(f"{item['sign']}  {item['label']}", item["display"])
    pdf.set_draw_color(203, 213, 225)
    pdf.line(pdf.l_margin, pdf.get_y() + 1,
             pdf.w - pdf.r_margin, pdf.get_y() + 1)
    pdf.ln(3)
    row(labels["net"], data["net_display"], bold=True)
    pdf.ln(2)

    # health
    section(labels["health"])
    row(f"{labels['score']}: {data['health']['score']} / 100",
        data["health"]["grade"])
    for part in data["health"]["parts"]:
        row(f"{part['label']} ({part['score']} / {part['max']})",
            part["note"])
    pdf.ln(2)

    # audit
    section(labels["audit"])
    if data["audit"]["clean"]:
        pdf.set_font("Noto", "", 9)
        pdf.set_text_color(22, 101, 52)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5.5, labels["clean"])
        pdf.ln(2)
    else:
        for finding in data["audit"]["findings"]:
            pdf.set_font("Noto", "B", 9)
            pdf.set_text_color(153, 27, 27)
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 5.5,
                           f"[{finding['severity']}] {finding['title']}")
            pdf.set_font("Noto", "", 8.5)
            pdf.set_text_color(71, 85, 105)
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 5, finding["detail"])
            pdf.ln(1.5)
    pdf.ln(1)

    # due by partner
    section(labels["due"])
    if data["due_groups"]:
        pdf.set_font("Noto", "B", 8.5)
        pdf.set_fill_color(241, 245, 249)
        pdf.set_text_color(51, 65, 85)
        for label, width in ((labels["customer"], 62),
                             (labels["phone"], 34),
                             (labels["opening"], 28),
                             (labels["advance"], 28),
                             (labels["due_col"], 34)):
            pdf.cell(width, 6, label, border=1, fill=True)
        pdf.ln()
        for group in data["due_groups"]:
            pdf.set_font("Noto", "B", 8.5)
            pdf.set_text_color(11, 37, 69)
            pdf.cell(126, 6, f"{labels['partner']}: {group['partner']}")
            pdf.cell(60, 6, group["total_display"], align="R")
            pdf.ln()
            for customer in group["customers"]:
                pdf.set_font("Noto", "", 8)
                pdf.set_text_color(30, 41, 59)
                pdf.cell(62, 5.5, customer["name"][:36], border=1)
                pdf.cell(34, 5.5, (customer["phone"] or "—")[:18], border=1)
                pdf.cell(28, 5.5, money(customer["opening"]), border=1,
                         align="R")
                pdf.cell(28, 5.5, money(customer["advance"]), border=1,
                         align="R")
                pdf.cell(34, 5.5, customer["due_display"], border=1,
                         align="R")
                pdf.ln()
        row(labels["total"], data["due_total_display"], bold=True)
    else:
        pdf.set_font("Noto", "", 9)
        pdf.multi_cell(0, 5.5, "—")
    pdf.ln(2)

    # loans
    section(labels["loans"])
    if data["loans"]:
        pdf.set_font("Noto", "B", 8.5)
        pdf.set_fill_color(241, 245, 249)
        pdf.set_text_color(51, 65, 85)
        for label, width in ((labels["lender"], 86),
                             (labels["received"], 32),
                             (labels["returned"], 32),
                             (labels["outstanding"], 36)):
            pdf.cell(width, 6, label, border=1, fill=True)
        pdf.ln()
        for loan in data["loans"]:
            pdf.set_font("Noto", "", 8)
            pdf.set_text_color(30, 41, 59)
            pdf.cell(86, 5.5, loan["name"][:48], border=1)
            pdf.cell(32, 5.5, money(loan["received"]), border=1, align="R")
            pdf.cell(32, 5.5, money(loan["returned"]), border=1, align="R")
            pdf.cell(36, 5.5, loan["outstanding_display"], border=1,
                     align="R")
            pdf.ln()
        row(labels["total"], data["loan_total_display"], bold=True)
    else:
        pdf.set_font("Noto", "", 9)
        pdf.multi_cell(0, 5.5, "—")
    pdf.ln(2)

    # advances / payable
    section(labels["advances"] + " · " + labels["payable"])
    row(labels["advances"], data["advances_display"])
    row(labels["payable"], data["payable_display"])

    if data["investments"]:
        pdf.ln(2)
        section(labels["invest"])
        for inv in data["investments"]:
            row(inv["name"], inv["outstanding_display"])

    pdf.ln(4)
    pdf.set_font("Noto", "", 7.5)
    pdf.set_text_color(100, 116, 139)
    pdf.multi_cell(0, 4.5, labels["readonly"])

    return bytes(pdf.output())


# ------------------------------------------------------------------- Excel --

def export_position_xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    labels = data["labels"]
    wb = Workbook()
    thin = Side(style="thin", color="CBD5E1")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="0B2545")
    head_font = Font(bold=True, color="FFFFFF", size=10)
    money_fmt = '#,##0.00'

    def sheet(title: str, headers: list[str], widths: list[int],
              rows: list[list[Any]]):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for col, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col)].width = width
        for cell in ws[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border
        for row in rows:
            ws.append(row)
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.border = border
                if isinstance(cell.value, (int, float)):
                    cell.number_format = money_fmt
                    cell.alignment = Alignment(horizontal="right")
        ws.freeze_panes = "A2"
        return ws

    # summary sheet
    ws = wb.active
    ws.title = "Position"
    ws["A1"] = data["title"]
    ws["A1"].font = Font(bold=True, size=14, color="0B2545")
    ws["A2"] = f"{labels['business']}: {data['business']}"
    ws["A3"] = f"{labels['as_of']}: {data['as_of_label']}"
    ws["A4"] = f"{labels['generated']}: {data['generated_label']}"
    row = 6
    ws.cell(row=row, column=1, value=labels["composition"]).font = Font(
        bold=True, size=11, color="0B2545")
    row += 1
    ws.cell(row=row, column=1, value="Item")
    ws.cell(row=row, column=2, value="Amount")
    for cell in ws[row][:2]:
        cell.fill = head_fill
        cell.font = head_font
        cell.border = border
    row += 1
    for item in data["composition"]:
        ws.cell(row=row, column=1,
                value=f"{item['sign']} {item['label']}").border = border
        cell = ws.cell(row=row, column=2, value=float(item["amount"]))
        cell.number_format = money_fmt
        cell.border = border
        row += 1
    ws.cell(row=row, column=1, value=labels["net"]).font = Font(bold=True)
    cell = ws.cell(row=row, column=2, value=float(data["net"]))
    cell.number_format = money_fmt
    cell.font = Font(bold=True)
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 20
    row += 2
    ws.cell(row=row, column=1, value=labels["health"]).font = Font(
        bold=True, size=11, color="0B2545")
    row += 1
    ws.cell(row=row, column=1,
            value=f"{labels['score']}: {data['health']['score']} / 100")
    ws.cell(row=row, column=2, value=data["health"]["grade"])
    row += 1
    for part in data["health"]["parts"]:
        ws.cell(row=row, column=1,
                value=f"{part['label']} ({part['score']}/{part['max']})")
        ws.cell(row=row, column=2, value=part["note"])
        row += 1

    # audit
    sheet("Audit",
          [labels["severity"], labels["finding"], labels["detail"]],
          [12, 40, 90],
          [[f["severity"], f["title"], f["detail"]]
           for f in data["audit"]["findings"]] or
          [["", labels["clean"], ""]])

    # due
    due_rows = []
    for group in data["due_groups"]:
        for customer in group["customers"]:
            due_rows.append([
                group["partner"], customer["name"], customer["phone"],
                float(customer["opening"]), float(customer["advance"]),
                float(customer["due"]),
            ])
    sheet("Due",
          [labels["partner"], labels["customer"], labels["phone"],
           labels["opening"], labels["advance"], labels["due_col"]],
          [26, 30, 18, 14, 14, 16], due_rows)

    # loans
    sheet("Loans",
          [labels["lender"], labels["received"], labels["returned"],
           labels["outstanding"]],
          [32, 18, 18, 18],
          [[loan["name"], float(loan["received"]), float(loan["returned"]),
            float(loan["outstanding"])] for loan in data["loans"]])

    stream = BytesIO()
    wb.save(stream)
    return stream.getvalue()


# -------------------------------------------------------------------- Word --

def export_position_docx(data: dict[str, Any]) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor

    labels = data["labels"]
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Noto Sans Bengali"
    style.font.size = Pt(10)

    title = doc.add_heading(data["title"], level=0)
    title.runs[0].font.color.rgb = RGBColor(0x0B, 0x25, 0x45)
    doc.add_paragraph(
        f"{labels['business']}: {data['business']}  ·  "
        f"{labels['as_of']}: {data['as_of_label']}")
    doc.add_paragraph(
        f"{labels['generated']}: {data['generated_label']}")

    doc.add_heading(labels["composition"], level=1)
    table = doc.add_table(rows=1, cols=2)
    table.style = "Light Grid Accent 1"
    table.rows[0].cells[0].text = labels["item"]
    table.rows[0].cells[1].text = labels["amount"]
    for item in data["composition"]:
        cells = table.add_row().cells
        cells[0].text = f"{item['sign']} {item['label']}"
        cells[1].text = item["display"]
    cells = table.add_row().cells
    cells[0].text = labels["net"]
    cells[1].text = data["net_display"]

    doc.add_heading(labels["health"], level=1)
    doc.add_paragraph(
        f"{labels['score']}: {data['health']['score']} / 100 — "
        f"{data['health']['grade']}")
    for part in data["health"]["parts"]:
        doc.add_paragraph(
            f"{part['label']}: {part['score']} / {part['max']} — "
            f"{part['note']}", style="List Bullet")

    doc.add_heading(labels["audit"], level=1)
    if data["audit"]["clean"]:
        doc.add_paragraph(labels["clean"])
    else:
        for finding in data["audit"]["findings"]:
            doc.add_heading(
                f"[{finding['severity']}] {finding['title']}", level=2)
            doc.add_paragraph(finding["detail"])

    doc.add_heading(labels["due"], level=1)
    if data["due_groups"]:
        due_table = doc.add_table(rows=1, cols=4)
        due_table.style = "Light Grid Accent 1"
        for index, label in enumerate(
                (labels["partner"], labels["customer"],
                 labels["due_col"], labels["phone"])):
            due_table.rows[0].cells[index].text = label
        for group in data["due_groups"]:
            for customer in group["customers"]:
                cells = due_table.add_row().cells
                cells[0].text = group["partner"]
                cells[1].text = customer["name"]
                cells[2].text = customer["due_display"]
                cells[3].text = customer["phone"] or "—"
        doc.add_paragraph(
            f"{labels['net']}: {data['due_total_display']}")
    else:
        doc.add_paragraph("—")

    doc.add_heading(labels["loans"], level=1)
    if data["loans"]:
        loan_table = doc.add_table(rows=1, cols=4)
        loan_table.style = "Light Grid Accent 1"
        for index, label in enumerate(
                (labels["lender"], labels["received"],
                 labels["returned"], labels["outstanding"])):
            loan_table.rows[0].cells[index].text = label
        for loan in data["loans"]:
            cells = loan_table.add_row().cells
            cells[0].text = loan["name"]
            cells[1].text = money(loan["received"])
            cells[2].text = money(loan["returned"])
            cells[3].text = loan["outstanding_display"]
        doc.add_paragraph(
            f"{labels['net']}: {data['loan_total_display']}")
    else:
        doc.add_paragraph("—")

    doc.add_heading(labels["advances"] + " · " + labels["payable"], level=1)
    doc.add_paragraph(f"{labels['advances']}: {data['advances_display']}")
    doc.add_paragraph(f"{labels['payable']}: {data['payable_display']}")

    if data["investments"]:
        doc.add_heading(labels["invest"], level=1)
        for inv in data["investments"]:
            doc.add_paragraph(
                f"{inv['name']}: {inv['outstanding_display']}",
                style="List Bullet")

    doc.add_paragraph()
    note = doc.add_paragraph(labels["readonly"])
    note.runs[0].font.size = Pt(8)
    note.runs[0].font.color.rgb = RGBColor(0x64, 0x74, 0x8B)

    stream = BytesIO()
    doc.save(stream)
    return stream.getvalue()
