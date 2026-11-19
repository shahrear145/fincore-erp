"""FinCore ERP — premium PDF renderers.

Every renderer here consumes the *existing* export dictionaries unchanged and
only re-draws them with :mod:`apps.web.pdf_theme`.  No query, calculation,
total, ordering or display string is produced in this module; callers hand in
exactly what the old renderers received.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from apps.web.pdf_theme import (
    FinCorePDF, GOLD, GOLD_2, GOLD_3, GOLD_SOFT, GOLD_PALE, NAVY, NAVY_2,
    MUTED, SLATE, EMERALD, EMERALD_SOFT, EMERALD_LINE, RED, RED_LINE, AMBER,
    LINE, LINE_SOFT, CREAM, WHITE, draw_table, draw_kv_rows, money,
)

ZERO = Decimal("0.00")


# =====================================================================
# সুপার ক্যালকুলেটর
# =====================================================================

def supercalc_pdf(data: dict[str, Any]) -> bytes:
    labels = data["labels"]
    totals = data["totals"]
    lang = data.get("lang", "bn")
    bn = lang != "en"

    pdf = FinCorePDF(
        orientation="P",
        title=labels["title"],
        subtitle=("ধাপে ধাপে সম্পূর্ণ হিসাব, পার্টনার খাতা ও লাভ-লোকসানের বিবরণ"
                  if bn else "Step-by-step statement, partner books and the final result"),
        business=data["business"],
        meta=[
            (labels["period"], data["period_label"]),
            (labels["days"], str(data["count"])),
            (labels["generated"], data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note=labels["footer"],
        lang=lang,
    )
    pdf.add_page()

    profit = totals["profit"]
    profit_tone = "emerald" if profit >= 0 else "red"
    profit_word = labels["profit_word"] if profit >= 0 else labels["loss_word"]

    pdf.kpi_row([
        {"label": labels["sell_total"], "value": totals["sell_total_display"],
         "color": GOLD_2, "soft": GOLD_SOFT,
         "foot": "নগদে সেল + বাকিতে সেল" if bn else "Cash + due sell"},
        {"label": labels["khoroch"], "value": totals["khoroch_display"],
         "color": RED, "soft": (254, 242, 242),
         "foot": "সব খরচ ও ক্রয়" if bn else "All expenses & purchases"},
        {"label": labels["profit"], "value": totals["profit_display"],
         "color": EMERALD if profit >= 0 else RED,
         "soft": (236, 253, 245) if profit >= 0 else (254, 242, 242),
         "value_color": True, "badge": profit_word},
        {"label": labels["collection"], "value": totals["collection_display"],
         "color": AMBER, "soft": (255, 251, 235),
         "foot": "পুরনো সেলের ফেরত" if bn else "Older dues paying back"},
    ])

    if not data["rows"]:
        pdf.empty_box(labels["empty"], height=20)
    else:
        _supercalc_steps(pdf, data)

    # signatures + end mark
    partner_names = [book["name"] for book in data.get("partner_books", [])]
    people: list[tuple[str, str]] = [
        (data["business"],
         "ব্যবসা / ব্যবস্থাপনা" if bn else "Business / Management"),
    ]
    people += [(name, "পার্টনার" if bn else "Partner") for name in partner_names]
    pdf.signatures(people[:5],
                   title="অনুমোদন ও স্বাক্ষর" if bn else "Authorization & signature",
                   subtitle="AUTHORIZATION & SIGNATURE" if bn else "অনুমোদন ও স্বাক্ষর")
    pdf.end_mark("রিপোর্ট সমাপ্ত · END OF REPORT" if bn
                 else "END OF REPORT · রিপোর্ট সমাপ্ত")

    return bytes(pdf.output())


def _supercalc_steps(pdf: FinCorePDF, data: dict[str, Any]) -> None:
    labels = data["labels"]

    # ---- STEP 1: cash account ------------------------------------------------
    pdf.section_head(labels["num_1"], labels["step_cash"],
                     labels["step_cash_desc"],
                     total_label=labels["cash_total"],
                     total_value=data["cash_total_display"])
    cash_rows = data.get("cash_rows", [])
    widths = [26, 44, 26, 26, 19, 24, 21]
    headers = [labels["date"], labels["particular"], labels["customer"],
               labels["partner"], labels["method"], labels["amount"],
               labels["running"]]
    aligns = ["C", "L", "L", "L", "C", "R", "R"]
    if cash_rows:
        rows = [[r["date_label"], r["particular"], r["customer"], r["partner"],
                 r["method"], r["amount_display"], r["running_display"]]
                for r in cash_rows]
        draw_table(pdf, widths, headers, rows, aligns,
                   total=[labels["total"], data["cash_total_display"],
                          data["totals"]["sell_cash_display"]],
                   total_colspan=5,
                   total_aligns=["R", "R", "R", "R", "R", "R", "R"])
    else:
        pdf.empty_box(labels["empty_step"])

    # ---- STEP 2: partner-wise due books -------------------------------------
    pdf.section_head(labels["num_2"], labels["step_partner"],
                     labels["step_partner_desc"],
                     total_label=labels["partner_books_total"],
                     total_value=data["partner_books_total_display"])
    books = data.get("partner_books", [])
    if not books:
        pdf.empty_box(labels["no_partner_books"])
    else:
        book_widths = [78, 30, 26, 27, 25]
        book_headers = [labels["customer"], labels["due_now"],
                        labels["period_due"], labels["period_collection"],
                        labels["active"]]
        book_aligns = ["L", "R", "R", "R", "C"]
        for book in books:
            meta_bits = [f'{labels["customers"]}: {book["count"]}/{book["row_count"]}']
            if book.get("phone"):
                meta_bits.append(book["phone"])
            pdf.partner_head(
                book["name"], "  ·  ".join(meta_bits),
                book["total_due_display"],
                period_due=book["period_due_display"],
                period_collection=book["period_collection_display"],
            )
            pdf.table_header(book_widths, book_headers, book_aligns)
            for index, item in enumerate(book["rows"]):
                if pdf.get_y() + 6.2 > pdf.h - pdf.b_margin:
                    pdf.add_page()
                    pdf.table_header(book_widths, book_headers, book_aligns)
                status = labels["active"] if item["is_active"] else labels["closed"]
                pdf.table_row(
                    book_widths,
                    [item["name"], item["due_display"],
                     item["period_due_display"],
                     item["period_collection_display"], status],
                    book_aligns, fill=bool(index % 2),
                    bold=not item["is_active"],
                    colors=[None, NAVY, None, EMERALD,
                            EMERALD if item["is_active"] else RED],
                )
            pdf.total_row(book_widths,
                          [labels["partner_total"], book["total_due_display"],
                           book["period_due_display"],
                           book["period_collection_display"]],
                          ["L", "R", "R", "R", "C"], colspan=1)
            pdf.ln(3)

    # ---- STEP 3: expense account --------------------------------------------
    pdf.section_head(labels["num_3"], labels["step_expense"],
                     labels["step_expense_desc"],
                     total_label=labels["expense_total"],
                     total_value=data["expense_total_display"])
    expense_rows = data.get("expense_rows", [])
    exp_widths = [26, 46, 36, 20, 30, 28]
    exp_headers = [labels["date"], labels["particular"], labels["category"],
                   labels["method"], labels["amount"], labels["running"]]
    exp_aligns = ["C", "L", "L", "C", "R", "R"]
    if expense_rows:
        rows = [[r["date_label"], r["particular"], r["category"], r["method"],
                 r["amount_display"], r["running_display"]]
                for r in expense_rows]
        draw_table(pdf, exp_widths, exp_headers, rows, exp_aligns,
                   total=[labels["total"], data["expense_total_display"],
                          data["totals"]["khoroch_display"]],
                   total_colspan=4,
                   total_aligns=["R", "R", "R", "R", "R", "R"])
    else:
        pdf.empty_box(labels["empty_step"])

    # ---- STEP 4: summary ----------------------------------------------------
    pdf.section_head(labels["num_4"], labels["step_summary"],
                     labels["step_summary_desc"],
                     total_label=labels["summary_total"],
                     total_value=f'{len(data.get("summary_partners", []))} {labels["entries"]}')
    _supercalc_summary(pdf, data)

    # ---- final result -------------------------------------------------------
    _supercalc_final(pdf, data)


def _supercalc_summary(pdf: FinCorePDF, data: dict[str, Any]) -> None:
    labels = data["labels"]
    partners = data.get("summary_partners", [])
    widths = [12, 52, 18, 35, 34, 35]
    headers = [labels["sl"], labels["partner"], labels["customers"],
               labels["due_given"], labels["collected"], labels["net_due"]]
    aligns = ["C", "L", "C", "R", "R", "R"]
    if partners:
        pdf.table_header(widths, headers, aligns)
        for index, partner in enumerate(partners):
            if pdf.get_y() + 6.2 > pdf.h - pdf.b_margin:
                pdf.add_page()
                pdf.table_header(widths, headers, aligns)
            pdf.table_row(
                widths,
                [partner["sl"], partner["name"], partner["customers"],
                 partner["due_given_display"], partner["collection_display"],
                 partner["current_due_display"]],
                aligns, fill=bool(index % 2),
                colors=[MUTED, NAVY, MUTED, None, EMERALD, AMBER],
            )
        pdf.total_row(
            widths,
            [labels["total"],
             data.get("summary_due_given_display", money(ZERO)),
             data.get("summary_collection_display", money(ZERO)),
             data["partner_books_total_display"]],
            ["L", "R", "C", "R", "R", "R"], colspan=3,
        )
    else:
        pdf.empty_box(labels["no_partner_books"])

    # calculation ladder
    totals = data["totals"]
    profit = totals["profit"]
    ladder = [
        (labels["cash_total"], data["cash_total_display"], NAVY, False),
        (f'+  {labels["due_total"]}', data["due_total_display"], NAVY, False),
        (f'=  {labels["net_sell"]}', totals["sell_total_display"], NAVY, True),
        (f'−  {labels["expense_total"]}', data["expense_total_display"], RED, False),
        (f'=  {labels["net_result"]}', totals["profit_display"],
         EMERALD if profit >= 0 else RED, True),
    ]
    if pdf.get_y() + 12 * len(ladder) + 6 > pdf.h - pdf.b_margin:
        pdf.add_page()
    pdf.ln(2)
    for label, value, color, strong in ladder:
        y = pdf.get_y()
        fill = strong
        if fill:
            pdf.set_fill_color(*GOLD_SOFT)
            pdf.set_draw_color(*GOLD_2)
            pdf.rect(pdf.l_margin, y, pdf.epw, 7.6, "DF")
        pdf.set_xy(pdf.l_margin + 3, y + 1.9)
        pdf.set_font("Noto", "B" if strong else "", 8.4)
        if strong:
            pdf.set_text_color(107, 74, 10)
        else:
            pdf.set_text_color(*SLATE)
        pdf.cell(pdf.epw - 50, 4.2, label)
        pdf.set_xy(pdf.w - pdf.r_margin - 47, y + 1.7)
        pdf.set_font("Noto", "B", 9.2)
        pdf.set_text_color(*color)
        pdf.cell(44, 4.2, value, align="R")
        pdf.set_y(y + 8.2)
    pdf.ln(1)
    pdf.note_line(labels["result_rule"])
    pdf.note_line(labels["collection_note"])


def _supercalc_final(pdf: FinCorePDF, data: dict[str, Any]) -> None:
    labels = data["labels"]
    totals = data["totals"]
    profit = totals["profit"]
    tone = "emerald" if profit >= 0 else "red"
    word = labels["profit_word"] if profit >= 0 else labels["loss_word"]
    pdf.final_panel(
        labels["step_result"],
        word.upper(),
        [
            (labels["net_sell"], totals["sell_total_display"], "navy"),
            (labels["net_expense"], totals["khoroch_display"], "red"),
            (labels["net_result"], totals["profit_display"], tone),
        ],
    )


# =====================================================================
# helpers shared by the remaining documents
# =====================================================================

def _num(index: int, en: bool) -> str:
    if en:
        return str(index)
    return "০১২৩৪৫৬৭৮৯"[index] if 0 <= index <= 9 else str(index)


def _finish(pdf: FinCorePDF, data: dict[str, Any], en: bool,
            extra: Sequence[tuple[str, str]] = (),
            title: str = "", subtitle: str = "") -> None:
    people: list[tuple[str, str]] = [
        (data.get("business", ""),
         "Business / Management" if en else "ব্যবসা / ব্যবস্থাপনা"),
    ]
    if data.get("partner"):
        people.append((data["partner"], "Partner" if en else "পার্টনার"))
    else:
        # No partner on this document — leave a blank line for the
        # accountant / auditor to sign, so the block never looks sparse.
        people.append(("", "Accountant / Auditor" if en
                       else "হিসাবরক্ষক / অডিটর"))
    people.extend(extra)
    pdf.signatures(
        people[:4],
        title=title or ("Authorization & signature" if en
                        else "অনুমোদন ও স্বাক্ষর"),
        subtitle=subtitle or ("অনুমোদন ও স্বাক্ষর" if en
                              else "AUTHORIZATION & SIGNATURE"),
    )
    pdf.end_mark("END OF REPORT · রিপোর্ট সমাপ্ত" if en
                 else "রিপোর্ট সমাপ্ত · END OF REPORT")


def _result_strip(pdf: FinCorePDF, label: str, value: str,
                  tone: str = "navy", note: str = "") -> None:
    if pdf.get_y() + 18 > pdf.h - pdf.b_margin:
        pdf.add_page()
    y = pdf.get_y()
    h = 15.0
    pdf.set_fill_color(*GOLD_PALE)
    pdf.set_draw_color(*GOLD_2)
    pdf.set_line_width(0.4)
    pdf.rect(pdf.l_margin, y, pdf.epw, h, "DF")
    pdf.set_xy(pdf.l_margin + 4, y + 2.2)
    pdf.set_font("Noto", "B", 7.6)
    pdf.set_text_color(107, 74, 10)
    pdf.cell(70, 3.6, label)
    if note:
        pdf.set_xy(pdf.l_margin + 4, y + 6.6)
        pdf.set_font("Noto", "", 6.0)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(pdf.epw - 80, 3.2, note)
    color = EMERALD if tone == "emerald" else (RED if tone == "red" else NAVY)
    size = 12.6 if len(value) <= 18 else 10.6
    pdf.set_xy(pdf.w - pdf.r_margin - 74, y + 3.0)
    pdf.set_font("Noto", "B", size)
    pdf.set_text_color(*color)
    pdf.cell(70, 8.0, value, align="R")
    pdf.set_y(y + h + 3.6)


def _balance_card(pdf: FinCorePDF, x: float, y: float, w: float, h: float,
                  title: str, items: Sequence[dict[str, Any]],
                  total_label: str, total_value: str, tone: str) -> None:
    color = EMERALD if tone == "emerald" else RED
    soft = (236, 253, 245) if tone == "emerald" else (254, 242, 242)
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(*LINE)
    pdf.rect(x, y, w, h, "DF")
    pdf.set_fill_color(*color)
    pdf.rect(x, y, w, 1.2, "F")
    pdf.set_xy(x + 3.5, y + 3.0)
    pdf.set_font("Noto", "B", 8.8)
    pdf.set_text_color(*NAVY)
    pdf.cell(w - 7, 4.0, title)
    row_y = y + 10.0
    for item in items:
        pdf.set_xy(x + 3.5, row_y)
        pdf.set_font("Noto", "", 7.2)
        pdf.set_text_color(*SLATE)
        pdf.cell(w - 40, 4.0, item["label"])
        pdf.set_xy(x + w - 38, row_y - 0.3)
        pdf.set_font("Noto", "B", 8.0)
        pdf.set_text_color(*NAVY)
        pdf.cell(34.5, 4.2, item["display"], align="R")
        row_y += 6.4
    total_y = y + h - 8.2
    pdf.set_fill_color(*soft)
    pdf.set_draw_color(*LINE)
    pdf.rect(x + 2.5, total_y, w - 5, 6.6, "DF")
    pdf.set_xy(x + 4.5, total_y + 1.5)
    pdf.set_font("Noto", "B", 7.0)
    pdf.set_text_color(*color)
    pdf.cell(w - 44, 3.6, total_label)
    pdf.set_xy(x + w - 42, total_y + 1.1)
    pdf.set_font("Noto", "B", 8.8)
    pdf.set_text_color(*NAVY)
    pdf.cell(38, 4.2, total_value, align="R")


def _info_box(pdf: FinCorePDF, title: str, note: str) -> None:
    if pdf.get_y() + 17 > pdf.h - pdf.b_margin:
        pdf.add_page()
    y = pdf.get_y()
    h = 14.0
    pdf.set_fill_color(*GOLD_PALE)
    pdf.set_draw_color(*GOLD_3)
    pdf.rect(pdf.l_margin, y, pdf.epw, h, "DF")
    pdf.set_fill_color(*GOLD_2)
    pdf.rect(pdf.l_margin, y, 1.4, h, "F")
    pdf.set_xy(pdf.l_margin + 4, y + 2.2)
    pdf.set_font("Noto", "B", 8.0)
    pdf.set_text_color(*NAVY)
    pdf.cell(pdf.epw - 8, 4.0, title)
    pdf.set_xy(pdf.l_margin + 4, y + 6.8)
    pdf.set_font("Noto", "", 6.2)
    pdf.set_text_color(*MUTED)
    pdf.cell(pdf.epw - 8, 3.4, note)
    pdf.set_y(y + h + 3.6)


def _health_panel(pdf: FinCorePDF, health: dict[str, Any], en: bool) -> None:
    if pdf.get_y() + 20 > pdf.h - pdf.b_margin:
        pdf.add_page()
    y = pdf.get_y()
    h = 18.0
    pdf.set_fill_color(*CREAM)
    pdf.set_draw_color(*GOLD_3)
    pdf.rect(pdf.l_margin, y, pdf.epw, h, "DF")
    pdf.set_fill_color(*GOLD_2)
    pdf.rect(pdf.l_margin, y, 1.5, h, "F")
    pdf.set_xy(pdf.l_margin + 5, y + 2.4)
    pdf.set_font("Noto", "", 5.8)
    pdf.set_text_color(*MUTED)
    pdf.cell(60, 3.2, "Health score" if en else "স্বাস্থ্য স্কোর")
    pdf.set_xy(pdf.l_margin + 5, y + 6.0)
    pdf.set_font("Noto", "B", 16.0)
    pdf.set_text_color(*NAVY)
    pdf.cell(38, 8.0, f'{health["score"]} / 100')
    pdf.set_xy(pdf.l_margin + 44, y + 7.0)
    grade = str(health.get("grade", ""))
    bw = max(18.0, pdf.get_string_width(grade) + 7)
    pdf.set_fill_color(*EMERALD_SOFT)
    pdf.set_draw_color(*EMERALD_LINE)
    pdf.rect(pdf.l_margin + 44, y + 7.2, bw, 5.2, "DF", round_corners=True,
             corner_radius=2.0)
    pdf.set_xy(pdf.l_margin + 44, y + 8.1)
    pdf.set_font("Noto", "B", 6.4)
    pdf.set_text_color(*EMERALD)
    pdf.cell(bw, 3.2, grade, align="C")
    pdf.set_xy(pdf.l_margin + 5 + 76, y + 4.0)
    pdf.set_font("Noto", "", 6.2)
    pdf.set_text_color(*MUTED)
    pdf.cell(pdf.epw - 90, 3.4,
             "Score built from the position and audit checks." if en
             else "নিট অবস্থান ও অডিট যাচাই থেকে তৈরি স্কোর।")
    pdf.set_y(y + h + 2.6)
    widths = [68, 22, 96]
    headers = ["Part" if en else "অংশ", "Score" if en else "স্কোর",
               "Note" if en else "মন্তব্য"]
    rows = [[part["label"], f'{part["score"]} / {part["max"]}', part["note"]]
            for part in health.get("parts", [])]
    if rows:
        draw_table(pdf, widths, headers, rows, ["L", "C", "L"],
                   row_height=5.6, size=6.4)


# =====================================================================
# বাকির খাতা
# =====================================================================

def khata_pdf(data: dict[str, Any]) -> bytes:
    labels = data["labels"]
    en = data.get("lang") == "en"
    pdf = FinCorePDF(
        orientation="L",
        title=labels["title"],
        subtitle=f'{labels["partner_book"]}  ·  {data["partner"]}',
        business=data["business"],
        meta=[
            (labels["partner"], data["partner"]),
            (labels["date"], data["date_label"]),
            (labels["generated"], data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note=labels["footer"],
        lang=data.get("lang", "bn"),
    )
    pdf.add_page()

    pdf.kpi_row([
        {"label": labels["names"], "value": str(data["count"]),
         "color": GOLD_2, "soft": GOLD_SOFT,
         "foot": "গ্রাহক তালিকা" if not en else "customer rows"},
        {"label": labels["total_previous"],
         "value": data["total_previous_display"],
         "color": AMBER, "soft": (255, 251, 235),
         "foot": labels["previous"]},
        {"label": labels["total_current"],
         "value": data["total_current_display"],
         "color": RED if data["total_current"] > 0 else EMERALD,
         "soft": (254, 242, 242) if data["total_current"] > 0 else (236, 253, 245),
         "value_color": True},
    ])

    widths = [12, 20, 52, 52, 34, 34, 69]
    headers = [labels["sl"], labels["id"], labels["name"], labels["contact"],
               labels["previous"], labels["current"], labels["note"]]
    aligns = ["C", "C", "L", "L", "R", "R", "L"]
    if data["rows"]:
        pdf.table_header(widths, headers, aligns)
        for index, row in enumerate(data["rows"]):
            _khata_row(pdf, data, row, widths, index)
        pdf.total_row(
            widths,
            [labels["total"], data["total_previous_display"],
             data["total_current_display"], ""],
            ["L", "R", "R", "C", "R", "R", "L"], colspan=4,
        )
        pdf.note_line(f'{labels["names"]}: {data["count"]}  ·  '
                      f'{labels["amount_note"]}')
    else:
        pdf.empty_box(labels["empty"], height=24)

    _finish(pdf, data, en)
    return bytes(pdf.output())


def _khata_text_height(pdf: FinCorePDF, width: float, text: str,
                       style: str, size: float, line: float) -> float:
    pdf.set_font("Noto", style, size)
    return float(pdf.multi_cell(width, line, text or "—", dry_run=True,
                                output="HEIGHT"))


def _khata_row(pdf: FinCorePDF, data: dict[str, Any], row: dict[str, Any],
               widths: list[float], index: int) -> None:
    name_h = _khata_text_height(pdf, widths[2] - 4, row["name"], "B", 8.4, 4.1)
    contact_h = _khata_text_height(pdf, widths[3] - 4, row["contact"], "", 7.2, 3.6)
    note_h = _khata_text_height(pdf, widths[6] - 4, row["note"], "", 7.2, 3.6)
    row_h = max(8.8, name_h + 3.2, contact_h + 3.2, note_h + 3.2)
    if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
        pdf.add_page()
        pdf.table_header(
            widths,
            [data["labels"]["sl"], data["labels"]["id"], data["labels"]["name"],
             data["labels"]["contact"], data["labels"]["previous"],
             data["labels"]["current"], data["labels"]["note"]],
            ["C", "C", "L", "L", "R", "R", "L"],
        )
    y = pdf.get_y()
    if index % 2:
        pdf.set_fill_color(248, 250, 252)
        pdf.rect(pdf.l_margin, y, sum(widths), row_h, "F")
    pdf.set_draw_color(*LINE_SOFT)
    pdf.set_line_width(0.2)
    pdf.line(pdf.l_margin, y + row_h, pdf.l_margin + sum(widths), y + row_h)

    x = pdf.l_margin
    pdf.set_xy(x, y + 2.2)
    pdf.set_font("Noto", "", 7.2)
    pdf.set_text_color(*MUTED)
    pdf.cell(widths[0], 4.0, str(row["sl"]), align="C")
    x += widths[0]
    pdf.set_xy(x, y + 2.2)
    pdf.set_font("Noto", "", 6.8)
    pdf.set_text_color(*MUTED)
    pdf.cell(widths[1], 4.0, row["id"], align="C")
    x += widths[1]
    pdf.set_xy(x + 2, y + 1.7)
    pdf.set_font("Noto", "B", 8.4)
    pdf.set_text_color(*NAVY)
    pdf.multi_cell(widths[2] - 4, 4.1, row["name"], align="L")
    x += widths[2]
    pdf.set_xy(x + 2, y + 1.9)
    pdf.set_font("Noto", "", 7.2)
    pdf.set_text_color(*SLATE)
    pdf.multi_cell(widths[3] - 4, 3.6, row["contact"], align="L")
    x += widths[3]
    pdf.set_xy(x, y + 2.2)
    pdf.set_font("Noto", "", 8.0)
    pdf.set_text_color(*NAVY_2)
    pdf.cell(widths[4] - 2, 4.2, row["previous_display"], align="R")
    x += widths[4]
    pdf.set_xy(x, y + 2.2)
    pdf.set_font("Noto", "B", 8.0)
    pdf.set_text_color(*(RED if row["current"] > 0 else EMERALD))
    pdf.cell(widths[5] - 2, 4.2, row["current_display"], align="R")
    x += widths[5]
    pdf.set_xy(x + 2, y + 1.9)
    pdf.set_font("Noto", "", 7.2)
    pdf.set_text_color(*SLATE)
    pdf.multi_cell(widths[6] - 4, 3.6, row["note"], align="L")
    pdf.set_y(y + row_h)


# =====================================================================
# হিসাব সারাংশ
# =====================================================================

def summary_pdf(data: dict[str, Any]) -> bytes:
    labels = data["labels"]
    en = "en" if str(data.get("lang", "bn")).lower().startswith("en") else False
    pdf = FinCorePDF(
        orientation="P",
        title=labels["title"],
        subtitle=("ব্যবসার সম্পদ, দায় ও এই সময়ের নগদের পূর্ণ সারাংশ"
                  if not en else "Assets, liabilities and cash movement at a glance"),
        business=data["business"],
        meta=[
            (labels["period"], data["period_label"]),
            (labels["generated"], data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note=labels["footer"],
        lang="en" if en else "bn",
    )
    pdf.add_page()

    pdf.kpi_row([
        {"label": labels["net"], "value": data["net_display"],
         "color": AMBER, "soft": GOLD_SOFT, "value_color": True,
         "badge": "NET"},
        {"label": labels["in_hand"], "value": data["cash_in_hand_display"],
         "color": EMERALD, "soft": (236, 253, 245)},
        {"label": labels["coming"], "value": data["due_display"],
         "color": GOLD_2, "soft": GOLD_SOFT},
        {"label": labels["owe"], "value": data["owe_total_display"],
         "color": RED, "soft": (254, 242, 242), "value_color": True},
    ])

    # have / owe cards side by side
    gap = 4.0
    card_w = (pdf.epw - gap) / 2
    card_h = 37.0
    if pdf.get_y() + card_h + 4 > pdf.h - pdf.b_margin:
        pdf.add_page()
    y = pdf.get_y()
    _balance_card(pdf, pdf.l_margin, y, card_w, card_h,
                  labels["have"], data["have"], labels["total"],
                  data["have_total_display"], "emerald")
    _balance_card(pdf, pdf.l_margin + card_w + gap, y, card_w, card_h,
                  labels["owe"], data["owe"], labels["total"],
                  data["owe_total_display"], "red")
    pdf.set_y(y + card_h + 5)

    # net strip
    net_tone = "emerald" if data["net"] >= 0 else "red"
    _result_strip(pdf, labels["net"], data["net_display"], net_tone,
                  note=(f'{labels["in_hand"]} {data["cash_in_hand_display"]}  +  '
                        f'{labels["coming"]} {data["due_display"]}  −  '
                        f'{labels["owe"]} {data["owe_total_display"]}'))

    invest = data.get("invest") or {}
    if invest:
        _info_box(
            pdf,
            f'{labels["invest"]}  {invest.get("display", "")}',
            f'{invest.get("received", "")}  ·  {invest.get("returned", "")}  —  '
            f'{invest.get("note", "")}',
        )

    pdf.section_head(_num(1, en), labels["period"])
    pdf.kpi_row([
        {"label": item["label"], "value": item["display"],
         "color": GOLD_2 if index == 0 else (RED if index == 1 else
                                            (EMERALD if index == 2 else NAVY)),
         "soft": GOLD_SOFT, "height": 13.0}
        for index, item in enumerate(data["period"])
    ], height=13.0)

    pdf.section_head(_num(2, en), labels["day_table"])
    widths = [24, 27, 27, 45, 39, 24]
    headers = [labels["date"], labels["in"], labels["out"],
               labels["loan_cap"], labels["baki_book"], labels["drawer"]]
    aligns = ["L", "R", "R", "R", "R", "R"]
    rows = [[r["date_label"], r["cash_in_display"], r["cash_out_display"],
             r["loan_cap"], r["baki"], r["closing_cash_display"]]
            for r in data["rows"] if r["has_data"]]
    if rows:
        totals = data["totals"]
        draw_table(
            pdf, widths, headers, rows, aligns,
            total=[labels["total"], totals["cash_in_display"],
                   totals["cash_out_display"], totals["loan_cap"],
                   totals["baki"], totals["closing_cash_display"]],
            total_colspan=1, row_height=5.8, size=6.5,
            total_aligns=["L", "R", "R", "R", "R", "R"],
        )
    else:
        pdf.empty_box("এই সময়ে কোনো নগদের চলাচল নেই।" if not en
                      else "No cash movement in this period.")

    pdf.note_line(labels["note"])
    _finish(pdf, data, en)
    return bytes(pdf.output())


# =====================================================================
# হিসাব কমান্ড সেন্টার (position)
# =====================================================================

def position_pdf(data: dict[str, Any]) -> bytes:
    labels = data["labels"]
    en = "en" if str(data.get("lang", "bn")).lower().startswith("en") else False
    pdf = FinCorePDF(
        orientation="P",
        title=data["title"],
        subtitle=data["subtitle"],
        business=data["business"],
        meta=[
            (labels["as_of"], data["as_of_label"]),
            (labels["generated"], data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note=labels["footer"],
        lang="en" if en else "bn",
    )
    pdf.add_page()

    pdf.kpi_row([
        {"label": labels["net"], "value": data["net_display"],
         "color": AMBER, "soft": GOLD_SOFT, "value_color": True},
        {"label": labels["health"], "value": f'{data["health"]["score"]} / 100',
         "color": EMERALD, "soft": (236, 253, 245),
         "foot": str(data["health"].get("grade", ""))},
        {"label": labels["advances_short"], "value": data["advances_display"],
         "color": GOLD_2, "soft": GOLD_SOFT},
        {"label": labels["payable_short"], "value": data["payable_display"],
         "color": RED, "soft": (254, 242, 242)},
    ])

    # composition
    pdf.section_head(_num(1, en), labels["composition"])
    rows = [(f'{item["sign"]}  {item["label"]}', item["display"])
            for item in data["composition"]]
    draw_kv_rows(pdf, rows)
    pdf.total_row([pdf.epw - 50, 50], [labels["net"], data["net_display"]],
                  ["L", "R"], colspan=1, height=8.0)

    # health
    pdf.section_head(_num(2, en), labels["health"])
    _health_panel(pdf, data["health"], en)

    # audit
    pdf.section_head(_num(3, en), labels["audit"])
    if data["audit"]["clean"]:
        _info_box(pdf, labels["clean"],
                  "সব যাচাই পরিষ্কার — কোনো অসঙ্গতি পাওয়া যায়নি।" if not en
                  else "Every check passed — no discrepancies found.")
    else:
        for finding in data["audit"]["findings"]:
            _finding_card(pdf, finding)
    if data["audit"].get("counts"):
        counts = data["audit"]["counts"]
        pdf.note_line("  ·  ".join(
            f"{labels.get(key, key)}: {value}"
            for key, value in counts.items()))

    # due by partner
    pdf.section_head(_num(4, en), labels["due"])
    if data["due_groups"]:
        widths = [66, 36, 26, 26, 32]
        headers = [labels["customer"], labels["phone"], labels["opening"],
                   labels["advance"], labels["due_col"]]
        aligns = ["L", "L", "R", "R", "R"]
        for group in data["due_groups"]:
            pdf.partner_head(
                group["partner"],
                f'{labels["customer"]}: {len(group["customers"])}',
                group["total_display"],
            )
            pdf.table_header(widths, headers, aligns)
            for index, customer in enumerate(group["customers"]):
                if pdf.get_y() + 6.0 > pdf.h - pdf.b_margin:
                    pdf.add_page()
                    pdf.table_header(widths, headers, aligns)
                pdf.table_row(
                    widths,
                    [customer["name"], customer["phone"] or "—",
                     money(customer["opening"]), money(customer["advance"]),
                     customer["due_display"]],
                    aligns, fill=bool(index % 2), size=6.8,
                    colors=[NAVY, MUTED, None, None, AMBER],
                )
            pdf.ln(2)
        _result_strip(pdf, labels["total"], data["due_total_display"], "navy")
    else:
        pdf.empty_box("—")

    # loans
    pdf.section_head(_num(5, en), labels["loans"])
    if data["loans"]:
        widths = [84, 32, 34, 36]
        headers = [labels["lender"], labels["received"], labels["returned"],
                   labels["outstanding"]]
        rows = [[loan["name"], money(loan["received"]),
                 money(loan["returned"]), loan["outstanding_display"]]
                for loan in data["loans"]]
        draw_table(pdf, widths, headers, rows, ["L", "R", "R", "R"],
                   total=[labels["total"], data["loan_total_display"]],
                   total_colspan=3,
                   total_aligns=["L", "R", "R", "R"])
    else:
        pdf.empty_box("—")

    # advances / payable
    pdf.section_head(_num(6, en),
                     f'{labels["advances"]}  ·  {labels["payable"]}')
    draw_kv_rows(pdf, [
        (labels["advances"], data["advances_display"]),
        (labels["payable"], data["payable_display"]),
    ])

    if data["investments"]:
        pdf.section_head(_num(7, en), labels["invest"])
        widths = [84, 32, 34, 36]
        headers = [labels["lender"], labels["received"], labels["returned"],
                   labels["outstanding"]]
        rows = [[inv["name"], money(inv["received"]),
                 money(inv["returned"]), inv["outstanding_display"]]
                for inv in data["investments"]]
        draw_table(pdf, widths, headers, rows, ["L", "R", "R", "R"],
                   total=[labels["total"], data["invest_total_display"]],
                   total_colspan=3,
                   total_aligns=["L", "R", "R", "R"])

    pdf.note_line(labels["readonly"])
    _finish(pdf, data, en)
    return bytes(pdf.output())


def _finding_card(pdf: FinCorePDF, finding: dict[str, Any]) -> None:
    if pdf.get_y() + 18 > pdf.h - pdf.b_margin:
        pdf.add_page()
    y = pdf.get_y()
    h = 16.0
    pdf.set_fill_color(254, 242, 242)
    pdf.set_draw_color(*RED_LINE)
    pdf.rect(pdf.l_margin, y, pdf.epw, h, "DF")
    pdf.set_fill_color(*RED)
    pdf.rect(pdf.l_margin, y, 1.3, h, "F")
    pdf.set_xy(pdf.l_margin + 4, y + 2.2)
    pdf.set_font("Noto", "B", 7.6)
    pdf.set_text_color(*RED)
    pdf.cell(24, 3.6, f'[{finding["severity"]}]')
    pdf.set_xy(pdf.l_margin + 28, y + 2.0)
    pdf.set_font("Noto", "B", 7.8)
    pdf.set_text_color(*NAVY)
    pdf.cell(pdf.epw - 36, 3.8, finding["title"])
    pdf.set_xy(pdf.l_margin + 4, y + 7.0)
    pdf.set_font("Noto", "", 6.4)
    pdf.set_text_color(*SLATE)
    pdf.multi_cell(pdf.epw - 60, 3.4, finding["detail"])
    if finding.get("count") or finding.get("amount"):
        pdf.set_xy(pdf.w - pdf.r_margin - 52, y + 2.0)
        pdf.set_font("Noto", "B", 7.0)
        pdf.set_text_color(*RED)
        bits = []
        if finding.get("count"):
            bits.append(str(finding["count"]))
        if finding.get("amount"):
            bits.append(money(finding["amount"]))
        pdf.cell(48, 3.6, "  ·  ".join(bits), align="R")
    pdf.set_y(y + h + 2.2)


# =====================================================================
# লাভ-ক্ষতি (P&L)
# =====================================================================

def pnl_pdf(data: dict[str, Any]) -> bytes:
    en = bool(data.get("en"))
    labels = data["labels"]
    title = "Profit & Loss (P&L)" if en else "লাভ-ক্ষতি (P&L)"
    pdf = FinCorePDF(
        orientation="P",
        title=title,
        subtitle=("আয়, ব্যয় ও নিট ফলাফলের পূর্ণ বিবরণ" if not en
                  else "Revenue, expenses and the net result"),
        business=data["business"],
        meta=[
            (labels["period"], data["date_label"]),
            (labels["generated"], data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note="FinCore ERP — Profit & Loss" if en
        else "FinCore ERP — লাভ-ক্ষতি",
        lang="en" if en else "bn",
    )
    pdf.add_page()

    profit = data["net"]
    tone = "emerald" if profit >= 0 else "red"
    pdf.kpi_row([
        {"label": "Total revenue" if en else "মোট আয়",
         "value": data["total_revenue_display"],
         "color": EMERALD, "soft": (236, 253, 245)},
        {"label": "Total expenses" if en else "মোট ব্যয়",
         "value": data["total_expenses_display"],
         "color": RED, "soft": (254, 242, 242)},
        {"label": "Net profit" if en else "নিট লাভ / ক্ষতি",
         "value": data["net_display"],
         "color": EMERALD if profit >= 0 else RED,
         "soft": (236, 253, 245) if profit >= 0 else (254, 242, 242),
         "value_color": True,
         "badge": ("PROFIT" if en else "লাভ") if profit >= 0
         else ("LOSS" if en else "ক্ষতি")},
    ])

    widths = [146, 40]
    headers = ["Account" if en else "একাউন্ট",
               "Amount (৳)" if en else "টাকা (৳)"]
    pdf.section_head(_num(1, en), "Revenue" if en else "আয় (Revenue)")
    rows = [[f'{row["code"]} · {row["name"]}',
             f'৳{row["amount"]:,.2f}'] for row in data["revenues"]]
    if rows:
        draw_table(pdf, widths, headers, rows, ["L", "R"],
                   total=["Total revenue" if en else "মোট আয়",
                          data["total_revenue_display"]],
                   total_aligns=["L", "R"])
    else:
        pdf.empty_box("No revenue in this range." if en
                      else "এই সময়কালে কোনো আয় নেই।")
        pdf.total_row(widths,
                      ["Total revenue" if en else "মোট আয়",
                       data["total_revenue_display"]],
                      ["L", "R"])

    pdf.section_head(_num(2, en), "Expenses" if en else "ব্যয় (Expenses)")
    rows = [[f'{row["code"]} · {row["name"]}',
             f'৳{row["amount"]:,.2f}'] for row in data["expenses"]]
    if rows:
        draw_table(pdf, widths, headers, rows, ["L", "R"],
                   total=["Total expenses" if en else "মোট ব্যয়",
                          data["total_expenses_display"]],
                   total_aligns=["L", "R"])
    else:
        pdf.empty_box("No expenses in this range." if en
                      else "এই সময়কালে কোনো ব্যয় নেই।")
        pdf.total_row(widths,
                      ["Total expenses" if en else "মোট ব্যয়",
                       data["total_expenses_display"]],
                      ["L", "R"])

    pdf.final_panel(
        "Net profit" if en and profit >= 0 else
        ("Net loss" if en else ("নিট লাভ" if profit >= 0 else "নিট ক্ষতি")),
        ("PROFIT" if en else "লাভ") if profit >= 0 else ("LOSS" if en else "ক্ষতি"),
        [
            ("Total revenue" if en else "মোট আয়",
             data["total_revenue_display"], "navy"),
            ("Total expenses" if en else "মোট ব্যয়",
             data["total_expenses_display"], "red"),
            ("Net result" if en else "নিট ফলাফল",
             data["net_display"], tone),
        ],
    )
    pdf.note_line(data["labels"]["amount_note"])
    _finish(pdf, data, en)
    return bytes(pdf.output())


# =====================================================================
# নগদের আনাগোনা (cash flow)
# =====================================================================

def cashflow_pdf(data: dict[str, Any]) -> bytes:
    en = bool(data.get("en"))
    labels = data["labels"]
    title = "Cash Flow Statement" if en else "নগদের আনাগোনা"
    pdf = FinCorePDF(
        orientation="P",
        title=title,
        subtitle=("ওয়ালেটভিত্তিক নগদের প্রবাহ ও দিনভিত্তিক চলাচল" if not en
                  else "Wallet-wise movement and the day-by-day flow"),
        business=data["business"],
        meta=[
            (labels["period"], data["date_label"]),
            (labels["generated"], data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note="FinCore ERP — Cash Flow" if en
        else "FinCore ERP — নগদের আনাগোনা",
        lang="en" if en else "bn",
    )
    pdf.add_page()

    net = data["net_change"]
    pdf.kpi_row([
        {"label": "Money in" if en else "মোট জমা",
         "value": data["total_money_in_display"],
         "color": EMERALD, "soft": (236, 253, 245)},
        {"label": "Money out" if en else "মোট খরচ",
         "value": data["total_money_out_display"],
         "color": RED, "soft": (254, 242, 242)},
        {"label": "Net change" if en else "নিট পরিবর্তন",
         "value": data["net_change_display"],
         "color": EMERALD if net >= 0 else RED,
         "soft": (236, 253, 245) if net >= 0 else (254, 242, 242),
         "value_color": True,
         "badge": ("UP" if en else "বৃদ্ধি") if net >= 0
         else ("DOWN" if en else "হ্রাস")},
    ])

    pdf.section_head(_num(1, en), "By account" if en else "হিসাব অনুযায়ী")
    widths = [58, 32, 32, 32, 32]
    headers = (["Account", "Opening", "Money in", "Money out", "Closing"]
               if en else ["হিসাব", "শুরু", "জমা", "খরচ", "শেষ"])
    rows = [[wallet["name"], wallet["opening_display"],
             wallet["inflow_display"], wallet["outflow_display"],
             wallet["closing_display"]] for wallet in data["wallets"]]
    if rows:
        draw_table(
            pdf, widths, headers, rows, ["L", "R", "R", "R", "R"],
            total=[("Total" if en else "মোট"),
                   money(sum((w["opening"] for w in data["wallets"]), ZERO)),
                   data["total_money_in_display"],
                   data["total_money_out_display"],
                   money(sum((w["closing"] for w in data["wallets"]), ZERO))],
            total_aligns=["L", "R", "R", "R", "R"],
        )
    else:
        pdf.empty_box("—")

    _result_strip(pdf, "Net change" if en else "নিট বৃদ্ধি / হ্রাস",
                  data["net_change_display"],
                  "emerald" if net >= 0 else "red")

    if data["daily"]:
        pdf.section_head(_num(2, en), "Day by day" if en else "দিন অনুযায়ী")
        daily_widths = [34, 38, 38, 38, 38]
        daily_headers = (["Date", "In", "Out", "Net", "Closing"] if en
                         else ["তারিখ", "জমা", "খরচ", "নিট", "শেষ"])
        rows = [[row["date"], row["inflow_display"], row["outflow_display"],
                 row["net_display"], row["closing_display"]]
                for row in data["daily"]]
        draw_table(pdf, daily_widths, daily_headers, rows,
                   ["L", "R", "R", "R", "R"], row_height=5.8, size=6.6)

    pdf.note_line(labels["amount_note"])
    _finish(pdf, data, en)
    return bytes(pdf.output())


# =====================================================================
# পার্টনার স্টেটমেন্ট
# =====================================================================

def partner_statement_pdf(data: dict[str, Any]) -> bytes:
    en = bool(data.get("en"))
    labels = data["labels"]
    title = "Partner Statement" if en else "পার্টনার স্টেটমেন্ট"
    pdf = FinCorePDF(
        orientation="P",
        title=title,
        subtitle=f'{data["partner"]}  ·  {data["date_label"]}',
        business=data["business"],
        meta=[
            ("PARTNER" if en else "পার্টনার", data["partner"]),
            (labels["period"], data["date_label"]),
            (labels["generated"], data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note="FinCore ERP — Partner Statement" if en
        else "FinCore ERP — পার্টনার স্টেটমেন্ট",
        lang="en" if en else "bn",
    )
    pdf.add_page()

    closing = data["closing"]
    pdf.kpi_row([
        {"label": "Opening baki" if en else "শুরু বাকি",
         "value": data["opening_display"], "color": GOLD_2, "soft": GOLD_SOFT},
        {"label": "Baki given" if en else "বাকি দেওয়া",
         "value": data["total_given_display"], "color": AMBER,
         "soft": (255, 251, 235)},
        {"label": "Collected" if en else "আদায়",
         "value": data["total_collected_display"], "color": EMERALD,
         "soft": (236, 253, 245)},
        {"label": "Closing baki" if en else "শেষের বাকি",
         "value": data["closing_display"],
         "color": RED if closing > 0 else EMERALD,
         "soft": (254, 242, 242) if closing > 0 else (236, 253, 245),
         "value_color": True},
    ])

    pdf.section_head(
        _num(1, en), "Ledger" if en else "খাতার বিবরণ",
        total_label="Closing baki" if en else "শেষের বাকি",
        total_value=data["closing_display"],
    )
    widths = [10, 24, 38, 20, 50, 22, 22]
    headers = (["SL", "Date", "Customer", "Type", "Note", "Amount", "Balance"]
               if en else ["ক্র.", "তারিখ", "গ্রাহক", "ধরন", "বিবরণ", "টাকা", "বাকি"])
    aligns = ["C", "L", "L", "L", "L", "R", "R"]
    if data["rows"]:
        rows = [[str(row["sl"]), row["date"], row["customer"], row["type"],
                 row["note"], row["amount"], row["running"]]
                for row in data["rows"]]
        draw_table(pdf, widths, headers, rows, aligns,
                   row_height=6.0, size=6.8)
    else:
        pdf.empty_box("No entries in the selected range." if en
                      else "এই সময়কালে কোনো এন্ট্রি নেই।")

    pdf.note_line(labels["amount_note"])
    _finish(pdf, data, en)
    return bytes(pdf.output())


# =====================================================================
# বাকি চালান (customer statement)
# =====================================================================

def customer_statement_pdf(data: dict[str, Any]) -> bytes:
    en = bool(data.get("en"))
    title = "Customer Statement" if en else "বাকি চালান"
    pdf = FinCorePDF(
        orientation="P",
        title=title,
        subtitle=f'{data["customer_name"]}  ·  #{data["customer_id"]}',
        business=data["business"],
        meta=[
            ("CUSTOMER" if en else "গ্রাহক", data["customer_name"]),
            ("PHONE" if en else "ফোন", data["customer_phone"] or "—"),
            ("DATE" if en else "তারিখ", data["date_label"]),
            ("GENERATED" if en else "তৈরি", data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note="FinCore ERP — Customer Statement" if en
        else "FinCore ERP — বাকি চালান",
        lang="en" if en else "bn",
    )
    pdf.add_page()

    overdue = data["current_due"] > ZERO
    pdf.kpi_row([
        {"label": "Total due given" if en else "মোট বাকি দেওয়া",
         "value": data["total_due_display"], "color": AMBER,
         "soft": (255, 251, 235)},
        {"label": "Total paid" if en else "মোট পরিশোধ",
         "value": data["total_paid_display"], "color": EMERALD,
         "soft": (236, 253, 245)},
        {"label": "Current due" if en else "চলতি বাকি",
         "value": data["current_due_display"],
         "color": RED if overdue else EMERALD,
         "soft": (254, 242, 242) if overdue else (236, 253, 245),
         "value_color": True},
    ])

    pdf.section_head(
        _num(1, en), "Ledger" if en else "লেনদেনের বিবরণ",
        total_label="Current due" if en else "চলতি বাকি",
        total_value=data["current_due_display"],
    )
    widths = [10, 24, 22, 76, 27, 27]
    headers = (["SL", "Date", "Type", "Particular", "Amount", "Balance"]
               if en else ["ক্র.", "তারিখ", "ধরন", "নামে", "টাকা", "বাকি"])
    aligns = ["C", "L", "L", "L", "R", "R"]
    if data["rows"]:
        rows = []
        for row in data["rows"]:
            running = row["running"]
            balance = money(abs(running)) + ("" if running >= 0 else " (Cr)")
            rows.append([str(row["sl"]), row["date"], row["type"],
                         row["note"], row["amount"], balance])
        draw_table(pdf, widths, headers, rows, aligns,
                   row_height=6.0, size=6.8)
    else:
        pdf.empty_box("No transactions for this customer." if en
                      else "এই গ্রাহকের কোনো লেনদেন নেই।")

    pdf.note_line(
        "All amounts are in Bangladeshi Taka (৳)." if en
        else "সব টাকা বাংলাদেশি টাকায় (৳)।"
    )
    pdf.signatures(
        [
            (data["customer_name"], "Customer" if en else "গ্রাহক"),
            (data["business"], "Business" if en else "ব্যবসায়ী"),
        ],
        title="Authorization & signature" if en else "অনুমোদন ও স্বাক্ষর",
        subtitle="অনুমোদন ও স্বাক্ষর" if en else "AUTHORIZATION & SIGNATURE",
    )
    pdf.end_mark("END OF REPORT · রিপোর্ট সমাপ্ত" if en
                 else "রিপোর্ট সমাপ্ত · END OF REPORT")
    return bytes(pdf.output())


# =====================================================================
# পরিশোধ রশিকা (money receipt)
# =====================================================================

def money_receipt_pdf(data: dict[str, Any]) -> bytes:
    en = bool(data.get("en"))
    title = "Money Receipt" if en else "পরিশোধ রশিকা"
    pdf = FinCorePDF(
        orientation="P",
        title=title,
        subtitle=(f'Receipt No: {data["receipt_no"]}' if en
                  else f'রশিকা নং: {data["receipt_no"]}'),
        business=data["business"],
        meta=[
            ("DATE" if en else "তারিখ", data["date_label"]),
            ("CUSTOMER" if en else "গ্রাহক", data["customer_name"]),
            ("PARTNER" if en else "পার্টনার", data["partner"]),
            ("GENERATED" if en else "তৈরি", data["generated_label"]),
        ],
        generated=data["generated_label"],
        footer_note="FinCore ERP — Money Receipt" if en
        else "FinCore ERP — পরিশোধ রশিকা",
        lang="en" if en else "bn",
    )
    pdf.add_page()

    # amount panel
    y = pdf.get_y()
    h = 42.0
    pdf.set_fill_color(*GOLD_PALE)
    pdf.set_draw_color(*GOLD_2)
    pdf.set_line_width(0.5)
    pdf.rect(pdf.l_margin, y, pdf.epw, h, "DF")
    pdf.set_fill_color(*GOLD_2)
    pdf.rect(pdf.l_margin, y, pdf.epw, 1.0, "F")
    pdf.set_xy(pdf.l_margin, y + 4.0)
    pdf.set_font("Noto", "", 6.4)
    pdf.set_text_color(*MUTED)
    pdf.cell(pdf.epw, 3.4, "Amount in figures" if en else "টাকা (অঙ্কে)",
             align="C")
    pdf.set_xy(pdf.l_margin, y + 9.0)
    pdf.set_font("Noto", "B", 26.0)
    pdf.set_text_color(*GOLD)
    pdf.cell(pdf.epw, 12.0, data["amount_display"], align="C")
    pdf.set_xy(pdf.l_margin + 6, y + 24.0)
    pdf.set_font("Noto", "", 7.4)
    pdf.set_text_color(*NAVY)
    words = (("in words: " + data["amount_words"]) if en
             else ("কথায়: " + data["amount_words"]))
    pdf.multi_cell(pdf.epw - 12, 4.2, words, align="C")
    pdf.set_y(y + h + 6)

    # details
    rows = [
        ("Date" if en else "তারিখ", data["date_label"]),
        ("Received from" if en else "যার থেকে মিলল",
         f'{data["customer_name"]} (#{data["customer_id"]})'),
        ("Particulars" if en else "বাবদ",
         "Payment against baki dues" if en else "বাকি পরিশোধ"),
        ("Note" if en else "বিবরণ", data["note"]),
        ("Receipt No" if en else "রশিকা নং", data["receipt_no"]),
        ("Partner" if en else "পার্টনার", data["partner"]),
    ]
    draw_kv_rows(pdf, rows)

    _info_box(
        pdf,
        "Thank you for your payment." if en else "ধন্যবাদ।",
        ("This receipt is generated from the books — no entry was edited."
         if en else
         "এই রশিকা হিসাবের খাতা থেকেই তৈরি — কোনো এন্ট্রি সম্পাদনা করা হয়নি।"),
    )

    pdf.signatures(
        [
            (data["customer_name"], "Customer" if en else "গ্রাহক"),
            (data["business"],
             "Authorised sign & stamp" if en else "অনুমোদিত স্বাক্ষর ও সিল"),
        ],
        title="Authorization & signature" if en else "অনুমোদন ও স্বাক্ষর",
        subtitle="অনুমোদন ও স্বাক্ষর" if en else "AUTHORIZATION & SIGNATURE",
    )
    pdf.end_mark("END OF REPORT · রিপোর্ট সমাপ্ত" if en
                 else "রিপোর্ট সমাপ্ত · END OF REPORT")
    return bytes(pdf.output())


