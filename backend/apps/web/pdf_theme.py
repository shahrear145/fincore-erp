"""FinCore ERP — shared premium PDF design system.

Every PDF export in the app uses this module so the printed documents share
one visual language: cream paper, navy ink, gold rules, light table headers,
emerald profit accents and a verified footer.

The module only draws.  It never reads or recalculates business data; callers
pass the exact display strings produced by their existing builders.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

from fpdf import FPDF

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_REGULAR = FONT_DIR / "NotoSansBengaliLatin-Regular.ttf"
FONT_BOLD = FONT_DIR / "NotoSansBengaliLatin-Bold.ttf"

# ---- palette (matches the Super Calculator web report) --------------------
NAVY = (15, 23, 42)
NAVY_2 = (30, 41, 59)
SLATE = (71, 85, 105)
MUTED = (100, 116, 139)
FAINT = (148, 163, 184)
LINE = (226, 232, 240)
LINE_SOFT = (238, 242, 247)
PAPER = (255, 255, 255)
CREAM = (255, 253, 247)
CREAM_2 = (255, 250, 240)
GOLD = (169, 118, 10)
GOLD_2 = (201, 150, 27)
GOLD_3 = (231, 200, 115)
GOLD_SOFT = (253, 243, 215)
GOLD_PALE = (255, 250, 240)
EMERALD = (4, 120, 87)
EMERALD_2 = (5, 150, 105)
EMERALD_SOFT = (236, 253, 245)
EMERALD_LINE = (167, 243, 208)
AMBER = (180, 83, 9)
AMBER_SOFT = (255, 251, 235)
RED = (185, 28, 28)
RED_SOFT = (254, 242, 242)
RED_LINE = (254, 202, 202)
WHITE = (255, 255, 255)

CENT = Decimal("0.01")


def money(value: Any) -> str:
    """Format a value exactly like the rest of the app: ৳1,234.00."""
    try:
        amount = Decimal(str(value or "0")).quantize(CENT)
    except (InvalidOperation, ValueError):
        amount = Decimal("0.00")
    return f"৳{amount:,.2f}"


def signed_money(value: Any) -> str:
    """Format with an explicit sign: +৳600.00 / -৳500.00 / ৳0.00."""
    try:
        amount = Decimal(str(value or "0")).quantize(CENT)
    except (InvalidOperation, ValueError):
        amount = Decimal("0.00")
    if amount > 0:
        return f"+৳{amount:,.2f}"
    if amount < 0:
        return f"-৳{-amount:,.2f}"
    return "৳0.00"


def fit_text(pdf: FPDF, text: Any, width: float, style: str = "",
             size: float = 7.4) -> str:
    """Trim text with an ellipsis so it never spills out of a table cell."""
    value = str(text if text is not None else "—")
    # The bundled Noto Sans Bengali has no U+2192 arrow; use an en dash so
    # period strings render cleanly instead of leaving a missing glyph.
    value = value.replace("→", "–")
    pdf.set_font("Noto", style, size)
    if pdf.get_string_width(value) <= width:
        return value
    ellipsis = "…"
    while value and pdf.get_string_width(value + ellipsis) > width:
        value = value[:-1]
    return (value + ellipsis) if value else ellipsis


def _align(value: str | None) -> str:
    return value if value in ("L", "R", "C") else "L"


class FinCorePDF(FPDF):
    """Base A4 document with a light hero, compact continuation header and
    a verified footer."""

    def __init__(
        self,
        *,
        orientation: str = "P",
        title: str = "",
        subtitle: str = "",
        business: str = "",
        meta: Sequence[tuple[str, str]] | None = None,
        generated: str = "",
        footer_note: str = "",
        lang: str = "bn",
        eyebrow: str = "FINCORE ERP · FINANCIAL INTELLIGENCE",
        badge: str = "SYSTEM VERIFIED",
    ) -> None:
        super().__init__(orientation=orientation, unit="mm", format="A4")
        self.report_title = title
        self.report_subtitle = subtitle
        self.report_business = business
        self.report_meta = list(meta or [])
        self.report_generated = generated
        self.report_footer_note = footer_note
        self.report_lang = "en" if str(lang).lower().startswith("en") else "bn"
        self.report_eyebrow = eyebrow
        self.report_badge = badge
        self._hero_drawn = False

        self.set_auto_page_break(auto=True, margin=17)
        self.set_margins(12, 12, 12)
        self.alias_nb_pages()
        self.add_font("Noto", "", str(FONT_REGULAR))
        self.add_font("Noto", "B", str(FONT_BOLD))
        self.set_text_shaping(True)
        self.set_title(f"{title} — {business}" if business else title)
        self.set_author("FinCore ERP")
        self.set_creator("FinCore ERP")

    # ---------------------------------------------------------------- chrome
    def header(self) -> None:
        if not self._hero_drawn:
            self._draw_hero()
            self._hero_drawn = True
        else:
            self._draw_compact_header()

    def footer(self) -> None:
        self.set_y(-13)
        self.set_draw_color(*GOLD_3)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin,
                  self.get_y())
        self.set_y(-10.2)
        self.set_font("Noto", "", 6.3)
        self.set_text_color(*MUTED)
        left = self.report_footer_note or (
            "FinCore ERP থেকে স্বয়ংক্রিয়ভাবে তৈরি"
            if self.report_lang == "bn" else "Generated automatically by FinCore ERP"
        )
        self.cell(70, 4.5, fit_text(self, left, 68, "", 6.3))
        center = (f"{self.report_generated} · সিস্টেম যাচাইকৃত"
                  if self.report_lang == "bn"
                  else f"{self.report_generated} · system verified")
        self.set_xy(self.l_margin + 66, self.get_y())
        self.set_text_color(138, 106, 31)
        self.cell(80, 4.5, fit_text(self, center, 78, "", 6.1), align="C")
        page_label = "পৃষ্ঠা" if self.report_lang == "bn" else "Page"
        self.set_xy(self.w - self.r_margin - 42, self.get_y())
        self.set_text_color(*SLATE)
        self.cell(42, 4.5, f"{page_label} {self.page_no()} / {{nb}}", align="R")

    def _draw_hero(self) -> None:
        landscape = self.w > self.h
        x = self.l_margin
        y = 8.0
        w = self.epw
        # The card has four stacked bands: brand, eyebrow, title and
        # subtitle, then the business line pinned to the bottom.  Both
        # orientations need enough height so the subtitle never collides
        # with the business line.
        h = 42.0 if landscape else 44.0
        meta_w = 78.0 if landscape else 68.0
        left_w = w - meta_w - 8

        self.set_fill_color(*CREAM)
        self.set_draw_color(*GOLD_3)
        self.set_line_width(0.35)
        self.rect(x, y, w, h, "DF")
        self.set_fill_color(*GOLD_2)
        self.rect(x, y, w, 0.9, "F")

        # brand mark
        self.set_fill_color(*NAVY)
        self.rect(x + 5, y + 5, 9.5, 9.5, "F", round_corners=True,
                  corner_radius=1.8)
        self.set_xy(x + 5, y + 7.4)
        self.set_font("Noto", "B", 11)
        self.set_text_color(*GOLD_3)
        self.cell(9.5, 5, "F", align="C")
        self.set_xy(x + 16.5, y + 5.2)
        self.set_font("Noto", "B", 7.2)
        self.set_text_color(*NAVY)
        self.cell(left_w - 18, 4, "FINCORE ERP")
        self.set_xy(x + 16.5, y + 9.0)
        self.set_font("Noto", "", 5.2)
        self.set_text_color(*MUTED)
        self.cell(left_w - 18, 3.5, "FINANCIAL INTELLIGENCE")

        # eyebrow + title + subtitle
        self.set_xy(x + 5, y + 16.2)
        self.set_font("Noto", "B", 5.4)
        self.set_text_color(*GOLD)
        self.cell(left_w - 6, 3.5,
                  fit_text(self, self.report_eyebrow, left_w - 8, "B", 5.4))
        self.set_xy(x + 5, y + 19.6)
        self.set_font("Noto", "B", 18 if not landscape else 17)
        self.set_text_color(*NAVY)
        self.cell(left_w - 6, 7.5,
                  fit_text(self, self.report_title, left_w - 8, "B",
                           18 if not landscape else 17))
        self.set_xy(x + 5, y + 27.2)
        self.set_font("Noto", "", 7.4)
        self.set_text_color(*SLATE)
        self.cell(left_w - 6, 4,
                  fit_text(self, self.report_subtitle, left_w - 8, "", 7.4))

        # business with gold bar
        self.set_fill_color(*GOLD_2)
        self.rect(x + 5, y + h - 7.6, 1.1, 5.2, "F")
        self.set_xy(x + 7.2, y + h - 8.2)
        self.set_font("Noto", "B", 9.6)
        self.set_text_color(*NAVY)
        self.cell(left_w - 9, 5.6,
                  fit_text(self, self.report_business, left_w - 11, "B", 9.6))

        # meta card
        mx = x + w - meta_w - 4
        my = y + 4.5
        mh = h - 9
        self.set_fill_color(*WHITE)
        self.set_draw_color(*LINE)
        self.rect(mx, my, meta_w, mh, "DF")
        rows = self.report_meta[:4]
        row_h = min(5.0, (mh - 5.5) / max(len(rows), 1))
        for index, (label, value) in enumerate(rows):
            ry = my + 2.2 + index * row_h
            self.set_xy(mx + 3, ry)
            self.set_font("Noto", "", 5.2)
            self.set_text_color(*MUTED)
            self.cell(meta_w - 6, 2.8,
                      fit_text(self, label, meta_w - 8, "", 5.2))
            self.set_xy(mx + 3, ry + 2.7)
            self.set_font("Noto", "B", 6.7)
            self.set_text_color(*NAVY)
            self.cell(meta_w - 6, 3.4,
                      fit_text(self, value, meta_w - 8, "B", 6.7))
        # verified badge
        by = my + mh - 5.0
        self.set_fill_color(*EMERALD_SOFT)
        self.set_draw_color(*EMERALD_LINE)
        self.rect(mx + 3, by, 27, 3.6, "DF", round_corners=True,
                  corner_radius=1.8)
        self.set_fill_color(*EMERALD_2)
        self.ellipse(mx + 4.6, by + 1.25, 1.1, 1.1, "F")
        self.set_xy(mx + 6.4, by + 0.35)
        self.set_font("Noto", "B", 4.6)
        self.set_text_color(*EMERALD)
        self.cell(23, 2.9, fit_text(self, self.report_badge, 22, "B", 4.6))

        self.set_y(y + h + 4.5)

    def _draw_compact_header(self) -> None:
        y = 0
        self.set_fill_color(*CREAM)
        self.rect(0, y, self.w, 13, "F")
        self.set_fill_color(*GOLD_2)
        self.rect(0, y + 13, self.w, 0.5, "F")
        self.set_fill_color(*NAVY)
        self.rect(self.l_margin, y + 3.4, 6.2, 6.2, "F", round_corners=True,
                  corner_radius=1.2)
        self.set_xy(self.l_margin, y + 4.5)
        self.set_font("Noto", "B", 7.2)
        self.set_text_color(*GOLD_3)
        self.cell(6.2, 4, "F", align="C")
        self.set_xy(self.l_margin + 8, y + 2.9)
        self.set_font("Noto", "B", 8.8)
        self.set_text_color(*NAVY)
        self.cell(120, 4.2,
                  fit_text(self, self.report_title, 118, "B", 8.8))
        self.set_xy(self.l_margin + 8, y + 7.3)
        self.set_font("Noto", "", 5.4)
        self.set_text_color(*MUTED)
        self.cell(120, 3.2,
                  fit_text(self, self.report_subtitle, 118, "", 5.4))
        self.set_xy(self.w - self.r_margin - 78, y + 3.0)
        self.set_font("Noto", "B", 6.4)
        self.set_text_color(*NAVY)
        self.cell(78, 3.6,
                  fit_text(self, self.report_business, 76, "B", 6.4),
                  align="R")
        self.set_xy(self.w - self.r_margin - 78, y + 7.0)
        self.set_font("Noto", "", 5.2)
        self.set_text_color(*MUTED)
        self.cell(78, 3.0,
                  fit_text(self, self.report_generated, 76, "", 5.2),
                  align="R")
        self.set_y(17)

    # ------------------------------------------------------------- components
    def kpi_row(self, cards: Sequence[dict[str, Any]], height: float = 17.0,
                gap: float = 3.0) -> None:
        """Four (or three) KPI cards with a coloured top rule."""
        if not cards:
            return
        if self.get_y() + height + 4 > self.h - self.b_margin:
            self.add_page()
        y = self.get_y()
        n = len(cards)
        w = (self.epw - gap * (n - 1)) / n
        for index, card in enumerate(cards):
            x = self.l_margin + index * (w + gap)
            color = card.get("color", GOLD_2)
            soft = card.get("soft", GOLD_SOFT)
            self.set_fill_color(*WHITE)
            self.set_draw_color(*LINE)
            self.rect(x, y, w, height, "DF")
            self.set_fill_color(*color)
            self.rect(x, y, w, 1.1, "F")
            self.set_xy(x + 3, y + 2.8)
            self.set_font("Noto", "B", 6.4)
            self.set_text_color(*MUTED)
            self.cell(w - 6, 3.4,
                      fit_text(self, card.get("label", ""), w - 7, "B", 6.4))
            value = str(card.get("value", ""))
            size = 13.2 if len(value) <= 14 else (11.6 if len(value) <= 18 else 10.2)
            self.set_xy(x + 3, y + 6.4)
            self.set_font("Noto", "B", size)
            self.set_text_color(*color if card.get("value_color") else NAVY)
            self.cell(w - 6, 6.2, fit_text(self, value, w - 7, "B", size))
            foot = str(card.get("foot", "") or "")
            if foot:
                self.set_xy(x + 3, y + height - 4.3)
                self.set_font("Noto", "", 5.5)
                self.set_text_color(*MUTED)
                self.cell(w - 6, 3.2, fit_text(self, foot, w - 7, "", 5.5))
            if card.get("badge"):
                badge = str(card["badge"])
                bw = max(9.0, self.get_string_width(badge) + 4.0)
                self.set_fill_color(*EMERALD_SOFT)
                self.set_draw_color(*EMERALD_LINE)
                self.rect(x + w - bw - 3, y + height - 5.1, bw, 3.4, "DF",
                          round_corners=True, corner_radius=1.6)
                self.set_xy(x + w - bw - 3, y + height - 4.7)
                self.set_font("Noto", "B", 4.9)
                self.set_text_color(*EMERALD)
                self.cell(bw, 2.7, badge, align="C")
        self.set_y(y + height + 4.2)

    def section_head(self, num: str, title: str, desc: str = "",
                     total_label: str = "", total_value: str = "",
                     gap_after: float = 2.2) -> None:
        if self.get_y() + 17 > self.h - self.b_margin:
            self.add_page()
        y = self.get_y()
        self.set_fill_color(*GOLD_SOFT)
        self.set_draw_color(*GOLD_3)
        self.rect(self.l_margin, y + 0.4, 9.4, 9.4, "DF", round_corners=True,
                  corner_radius=1.6)
        self.set_xy(self.l_margin, y + 2.1)
        self.set_font("Noto", "B", 9.4)
        self.set_text_color(*GOLD)
        self.cell(9.4, 5.2, num, align="C")
        text_w = self.epw - 13.5 - (42 if total_value else 0)
        self.set_xy(self.l_margin + 12.4, y + 0.2)
        self.set_font("Noto", "B", 11.2)
        self.set_text_color(*NAVY)
        self.cell(text_w, 4.8, fit_text(self, title, text_w - 2, "B", 11.2))
        if desc:
            self.set_xy(self.l_margin + 12.4, y + 5.2)
            self.set_font("Noto", "", 6.5)
            self.set_text_color(*MUTED)
            self.cell(text_w, 3.4,
                      fit_text(self, desc, text_w - 2, "", 6.5))
        if total_value:
            self.set_xy(self.w - self.r_margin - 42, y + 0.9)
            self.set_font("Noto", "", 5.4)
            self.set_text_color(*MUTED)
            self.cell(42, 3.0,
                      fit_text(self, total_label, 41, "", 5.4), align="R")
            self.set_xy(self.w - self.r_margin - 42, y + 4.2)
            self.set_font("Noto", "B", 9.4)
            self.set_text_color(*NAVY)
            self.cell(42, 4.4,
                      fit_text(self, total_value, 41, "B", 9.4), align="R")
        self.set_draw_color(*GOLD_3)
        self.set_line_width(0.25)
        self.line(self.l_margin, y + 11.4, self.w - self.r_margin, y + 11.4)
        self.set_y(y + 12.8 + gap_after)

    def table_header(self, widths: Sequence[float], labels: Sequence[str],
                     aligns: Sequence[str] | None = None,
                     height: float = 7.2) -> None:
        y = self.get_y()
        self.set_fill_color(248, 250, 252)
        self.set_draw_color(*LINE)
        self.rect(self.l_margin, y, sum(widths), height, "F")
        self.set_draw_color(*GOLD_3)
        self.set_line_width(0.35)
        self.line(self.l_margin, y, self.l_margin + sum(widths), y)
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(self.l_margin, y + height, self.l_margin + sum(widths),
                  y + height)
        aligns = list(aligns or ["L"] * len(labels))
        x = self.l_margin
        for index, (label, width) in enumerate(zip(labels, widths)):
            align = _align(aligns[index] if index < len(aligns) else "L")
            self.set_xy(x + 1.4, y + 1.9)
            self.set_font("Noto", "B", 6.5)
            self.set_text_color(71, 85, 105)
            self.cell(width - 2.8, 3.6,
                      fit_text(self, label, width - 3.2, "B", 6.5),
                      align=align)
            x += width
        self.set_y(y + height)

    def table_row(self, widths: Sequence[float], cells: Sequence[Any],
                  aligns: Sequence[str] | None = None, fill: bool = False,
                  height: float = 6.2, size: float = 7.2,
                  bold: bool = False,
                  colors: Sequence[tuple[int, int, int] | None] | None = None
                  ) -> None:
        y = self.get_y()
        if fill:
            self.set_fill_color(248, 250, 252)
            self.rect(self.l_margin, y, sum(widths), height, "F")
        self.set_draw_color(*LINE_SOFT)
        self.set_line_width(0.2)
        self.line(self.l_margin, y + height, self.l_margin + sum(widths),
                  y + height)
        aligns = list(aligns or ["L"] * len(cells))
        colors = list(colors or [None] * len(cells))
        x = self.l_margin
        for index, (value, width) in enumerate(zip(cells, widths)):
            align = _align(aligns[index] if index < len(aligns) else "L")
            color = colors[index] if index < len(colors) else None
            self.set_xy(x + 1.4, y + 1.55)
            self.set_font("Noto", "B" if bold else "", size)
            self.set_text_color(*(color or NAVY_2))
            self.cell(width - 2.8, height - 2.4,
                      fit_text(self, value, width - 3.2,
                               "B" if bold else "", size), align=align)
            x += width
        self.set_y(y + height)

    def total_row(self, widths: Sequence[float], cells: Sequence[Any],
                  aligns: Sequence[str] | None = None, colspan: int = 1,
                  height: float = 7.2) -> None:
        y = self.get_y()
        if y + height > self.h - self.b_margin:
            self.add_page()
            y = self.get_y()
        self.set_fill_color(253, 246, 227)
        self.set_draw_color(*GOLD_3)
        self.rect(self.l_margin, y, sum(widths), height, "F")
        self.set_line_width(0.35)
        self.line(self.l_margin, y, self.l_margin + sum(widths), y)
        self.line(self.l_margin, y + height, self.l_margin + sum(widths),
                  y + height)
        aligns = list(aligns or ["L"] * len(cells))
        x = self.l_margin
        label_w = sum(widths[:colspan])
        self.set_xy(x + 1.4, y + 1.9)
        self.set_font("Noto", "B", 7.2)
        self.set_text_color(107, 74, 10)
        self.cell(label_w - 2.8, height - 2.6,
                  fit_text(self, cells[0], label_w - 3.2, "B", 7.2),
                  align=_align(aligns[0] if aligns else "L"))
        x += label_w
        for index, value in enumerate(cells[1:], start=colspan):
            width = widths[index] if index < len(widths) else 0
            align = _align(aligns[index] if index < len(aligns) else "R")
            self.set_xy(x + 1.4, y + 1.9)
            self.set_font("Noto", "B", 7.2)
            self.set_text_color(*NAVY)
            self.cell(width - 2.8, height - 2.6,
                      fit_text(self, value, width - 3.2, "B", 7.2),
                      align=align)
            x += width
        self.set_y(y + height)

    def partner_head(self, name: str, meta: str, total_due: str,
                     period_due: str = "", period_collection: str = "",
                     share_pct: float | None = None) -> None:
        if self.get_y() + 20 > self.h - self.b_margin:
            self.add_page()
        y = self.get_y()
        self.set_fill_color(*CREAM)
        self.set_draw_color(*GOLD_3)
        self.rect(self.l_margin, y, self.epw, 15.5, "DF")
        self.set_fill_color(*GOLD_SOFT)
        self.set_draw_color(*GOLD_3)
        self.rect(self.l_margin + 3, y + 3.1, 9.2, 9.2, "DF",
                  round_corners=True, corner_radius=1.6)
        self.set_xy(self.l_margin + 3, y + 4.9)
        self.set_font("Noto", "B", 10.2)
        self.set_text_color(*GOLD)
        self.cell(9.2, 5, (name or "?")[:1].upper(), align="C")
        self.set_xy(self.l_margin + 15, y + 2.5)
        self.set_font("Noto", "B", 10.2)
        self.set_text_color(*NAVY)
        self.cell(70, 4.6, fit_text(self, name, 68, "B", 10.2))
        self.set_xy(self.l_margin + 15, y + 7.4)
        self.set_font("Noto", "", 5.9)
        self.set_text_color(*MUTED)
        self.cell(82, 3.4, fit_text(self, meta, 80, "", 5.9))
        # right totals
        right_x = self.w - self.r_margin - 74
        self.set_xy(right_x, y + 2.0)
        self.set_font("Noto", "", 5.4)
        self.set_text_color(*MUTED)
        self.cell(30, 3.0, "মোট বাকি" if self.report_lang == "bn" else "Total due",
                  align="R")
        self.set_xy(right_x, y + 4.8)
        self.set_font("Noto", "B", 9.6)
        self.set_text_color(*NAVY)
        self.cell(30, 4.4, fit_text(self, total_due, 29, "B", 9.6),
                  align="R")
        if period_due:
            self.set_xy(right_x + 33, y + 2.0)
            self.set_font("Noto", "", 5.2)
            self.set_text_color(*MUTED)
            self.cell(19, 3.0, "এই সময়ে" if self.report_lang == "bn" else "Period",
                      align="R")
            self.set_xy(right_x + 33, y + 4.8)
            self.set_font("Noto", "B", 6.6)
            self.set_text_color(*AMBER)
            self.cell(19, 4.2, fit_text(self, period_due, 18, "B", 6.6),
                      align="R")
        if period_collection:
            self.set_xy(right_x + 54, y + 2.0)
            self.set_font("Noto", "", 5.2)
            self.set_text_color(*MUTED)
            self.cell(20, 3.0, "আদায়" if self.report_lang == "bn" else "Collected",
                      align="R")
            self.set_xy(right_x + 54, y + 4.8)
            self.set_font("Noto", "B", 6.6)
            self.set_text_color(*EMERALD)
            self.cell(20, 4.2, fit_text(self, period_collection, 19, "B", 6.6),
                      align="R")
        if share_pct is not None:
            self.set_xy(right_x, y + 10.0)
            self.set_font("Noto", "", 5.2)
            self.set_text_color(*GOLD)
            self.cell(74, 2.8, f"{share_pct:.1f}% share", align="R")
        self.set_y(y + 15.5 + 1.8)

    def final_panel(self, title: str, badge: str,
                    boxes: Sequence[tuple[str, str, str]]) -> None:
        """Cream/gold final result panel with three boxes.

        ``boxes`` = [(label, value, tone)] where tone is 'navy', 'red' or
        'emerald'.
        """
        if self.get_y() + 36 > self.h - self.b_margin:
            self.add_page()
        y = self.get_y()
        h = 30.0
        self.set_fill_color(*GOLD_PALE)
        self.set_draw_color(*GOLD_2)
        self.set_line_width(0.45)
        self.rect(self.l_margin, y, self.epw, h, "DF")
        self.set_xy(self.l_margin + 4, y + 3.2)
        self.set_font("Noto", "B", 9.8)
        self.set_text_color(*NAVY)
        self.cell(90, 4.4, fit_text(self, title, 88, "B", 9.8))
        # badge
        bw = max(24.0, self.get_string_width(badge) + 6.0)
        self.set_fill_color(*GOLD_SOFT)
        self.set_draw_color(*GOLD_3)
        self.rect(self.w - self.r_margin - bw - 4, y + 2.6, bw, 4.2, "DF",
                  round_corners=True, corner_radius=2.0)
        self.set_xy(self.w - self.r_margin - bw - 4, y + 3.3)
        self.set_font("Noto", "B", 5.3)
        self.set_text_color(*GOLD)
        self.cell(bw, 2.8, fit_text(self, badge, bw - 1, "B", 5.3), align="C")
        # boxes
        gap = 3.0
        box_w = (self.epw - 8.0 - gap * (len(boxes) - 1)) / max(len(boxes), 1)
        box_y = y + 9.0
        box_h = 16.0
        x = self.l_margin + 4
        for label, value, tone in boxes:
            if tone == "emerald":
                self.set_fill_color(*EMERALD_SOFT)
                self.set_draw_color(*EMERALD_LINE)
            elif tone == "red":
                self.set_fill_color(*RED_SOFT)
                self.set_draw_color(*RED_LINE)
            else:
                self.set_fill_color(*WHITE)
                self.set_draw_color(*LINE)
            self.rect(x, box_y, box_w, box_h, "DF")
            self.set_xy(x, box_y + 2.4)
            self.set_font("Noto", "", 5.6)
            self.set_text_color(*MUTED)
            self.cell(box_w, 3.0, fit_text(self, label, box_w - 2, "", 5.6),
                      align="C")
            color = EMERALD if tone == "emerald" else (RED if tone == "red" else NAVY)
            size = 11.4 if len(str(value)) <= 14 else 9.6
            self.set_xy(x, box_y + 6.6)
            self.set_font("Noto", "B", size)
            self.set_text_color(*color)
            self.cell(box_w, 6.0, fit_text(self, value, box_w - 3, "B", size),
                      align="C")
            x += box_w + gap
        self.set_y(y + h + 5)

    def signatures(self, items: Sequence[tuple[str, str]],
                   title: str = "অনুমোদন ও স্বাক্ষর",
                   subtitle: str = "AUTHORIZATION & SIGNATURE") -> None:
        if not items:
            return
        if self.get_y() + 34 > self.h - self.b_margin:
            self.add_page()
        y = self.get_y()
        self.set_fill_color(*WHITE)
        self.set_draw_color(*LINE)
        self.rect(self.l_margin, y, self.epw, 31, "DF")
        self.set_xy(self.l_margin + 4, y + 3.0)
        self.set_font("Noto", "B", 8.6)
        self.set_text_color(*NAVY)
        self.cell(60, 4, title)
        self.set_xy(self.l_margin + 4 + self.get_string_width(title) + 4, y + 3.4)
        self.set_font("Noto", "", 4.9)
        self.set_text_color(*FAINT)
        self.cell(70, 3, subtitle)
        n = len(items)
        gap = 4.0
        col_w = (self.epw - 8 - gap * (n - 1)) / n
        x = self.l_margin + 4
        line_y = y + 15.5
        date_label = ("তারিখ: ____________" if self.report_lang == "bn"
                      else "Date: ____________")
        for name, role in items:
            self.set_draw_color(148, 163, 184)
            self.set_line_width(0.35)
            self.line(x + 2, line_y, x + col_w - 2, line_y)
            self.set_xy(x, line_y + 2.0)
            self.set_font("Noto", "B", 7.2)
            self.set_text_color(*NAVY)
            self.cell(col_w, 3.6, fit_text(self, name, col_w - 2, "B", 7.2),
                      align="C")
            self.set_xy(x, line_y + 5.8)
            self.set_font("Noto", "", 5.4)
            self.set_text_color(*MUTED)
            self.cell(col_w, 3.0, fit_text(self, role, col_w - 2, "", 5.4),
                      align="C")
            self.set_xy(x, line_y + 9.0)
            self.set_font("Noto", "", 4.8)
            self.set_text_color(*FAINT)
            self.cell(col_w, 2.6, date_label, align="C")
            x += col_w + gap
        self.set_y(y + 34)

    def end_mark(self, text: str = "রিপোর্ট সমাপ্ত · END OF REPORT") -> None:
        if self.get_y() + 10 > self.h - self.b_margin:
            return
        y = self.get_y() + 2
        self.set_font("Noto", "B", 5.4)
        self.set_text_color(*FAINT)
        tw = self.get_string_width(text)
        self.set_draw_color(*GOLD_3)
        self.set_line_width(0.2)
        self.line(self.w / 2 - tw / 2 - 20, y + 2, self.w / 2 - tw / 2 - 4, y + 2)
        self.line(self.w / 2 + tw / 2 + 4, y + 2, self.w / 2 + tw / 2 + 20, y + 2)
        self.set_xy(self.w / 2 - tw / 2, y)
        self.cell(tw + 1, 4, text, align="C")

    def empty_box(self, text: str, height: float = 16.0) -> None:
        y = self.get_y()
        self.set_fill_color(248, 250, 252)
        self.set_draw_color(*LINE)
        self.rect(self.l_margin, y, self.epw, height, "DF")
        self.set_xy(self.l_margin + 3, y + height / 2 - 2)
        self.set_font("Noto", "", 7.6)
        self.set_text_color(*MUTED)
        self.cell(self.epw - 6, 4, fit_text(self, text, self.epw - 8, "", 7.6),
                  align="C")
        self.set_y(y + height + 3)

    def note_line(self, text: str) -> None:
        if self.get_y() + 8 > self.h - self.b_margin:
            self.add_page()
        self.set_xy(self.l_margin, self.get_y() + 1)
        self.set_font("Noto", "", 6.2)
        self.set_text_color(*MUTED)
        self.multi_cell(self.epw, 3.6, text)
        self.set_y(self.get_y() + 2)


# ------------------------------------------------------------------ drawing --
def draw_table(pdf: FinCorePDF, widths: Sequence[float],
               headers: Sequence[str], rows: Sequence[Sequence[Any]],
               aligns: Sequence[str], total: Sequence[Any] | None = None,
               total_colspan: int = 1, row_height: float = 6.2,
               size: float = 7.2,
               total_aligns: Sequence[str] | None = None,
               cell_colors: Sequence[Sequence[tuple[int, int, int] | None]] | None = None
               ) -> None:
    """Draw a table with repeated headers and optional gold total row."""
    pdf.table_header(widths, headers, aligns)
    for index, row in enumerate(rows):
        if pdf.get_y() + row_height > pdf.h - pdf.b_margin:
            pdf.add_page()
            pdf.table_header(widths, headers, aligns)
        colors = cell_colors[index] if cell_colors and index < len(cell_colors) else None
        pdf.table_row(widths, row, aligns, fill=bool(index % 2),
                      height=row_height, size=size, colors=colors)
    if total:
        pdf.total_row(widths, total, total_aligns or aligns,
                      colspan=total_colspan)


def draw_kv_rows(pdf: FinCorePDF, rows: Sequence[tuple[str, str]],
                 label_w: float = 100.0) -> None:
    """Two-column label/value rows with dotted leaders."""
    for label, value in rows:
        if pdf.get_y() + 6 > pdf.h - pdf.b_margin:
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_xy(pdf.l_margin, y + 1)
        pdf.set_font("Noto", "", 7.2)
        pdf.set_text_color(*SLATE)
        pdf.cell(label_w, 4.2, fit_text(pdf, label, label_w - 2, "", 7.2))
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.2)
        pdf.line(pdf.l_margin + label_w, y + 3.3, pdf.w - pdf.r_margin - 32,
                 y + 3.3)
        pdf.set_xy(pdf.w - pdf.r_margin - 32, y + 0.6)
        pdf.set_font("Noto", "B", 7.4)
        pdf.set_text_color(*NAVY)
        pdf.cell(32, 4.2, fit_text(pdf, value, 31, "B", 7.4), align="R")
        pdf.set_y(y + 5.4)
