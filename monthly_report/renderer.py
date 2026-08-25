"""Render the structured monthly-report model as a self-contained PDF.

The renderer intentionally has no Flask or storage dependencies.  Callers pass
the reviewed runtime data and receive a rewound :class:`io.BytesIO` object.
"""

from __future__ import annotations

import io
import math
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from html import escape
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    Image as RLImage,
    LongTable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from .photos import asset_filename, is_asset_id


__all__ = ["render_monthly_report"]


PAGE_WIDTH, PAGE_HEIGHT = A4
BLACK = colors.HexColor("#000000")
WHITE = colors.HexColor("#FFFFFF")
GREY_HEADER = colors.HexColor("#D9D9D9")
GREY_SUBHEADER = colors.HexColor("#DFDFDF")
LIGHT_GREY = colors.HexColor("#F3F4F6")
MID_GREY = colors.HexColor("#6B7280")
CYAN = colors.HexColor("#00AFEF")
ORANGE = colors.HexColor("#F79646")
ROYAL_BLUE = colors.HexColor("#0000FF")
TOC_BLUE = colors.HexColor("#365F91")
FINAL_GREEN = colors.HexColor("#008751")

OUTER_BORDER = (24.0, 24.0, PAGE_WIDTH - 48.0, PAGE_HEIGHT - 48.0)
BODY_LEFT = 35.4
BODY_RIGHT = 33.0
BODY_BOTTOM = 48.0
BODY_TOP_MARGIN = 109.0
BODY_WIDTH = PAGE_WIDTH - BODY_LEFT - BODY_RIGHT
BODY_HEIGHT = PAGE_HEIGHT - BODY_TOP_MARGIN - BODY_BOTTOM

DEFAULT_APPENDICES = (
    ("6.1", "Summary Progress"),
    ("6.2", "Progress S-Curve"),
    ("6.3", "Overall Schedule"),
    ("6.4", "Document Deliverable List / Drawing Status"),
    ("6.5", "Manning Manpower / Equipment Loading"),
    ("6.6", "Photo Documentation"),
    ("6.7", "Safety Report"),
    ("6.8", "QC Document"),
)


def _value(mapping: Mapping[str, Any] | None, *keys: str, default: Any = "") -> Any:
    if not isinstance(mapping, Mapping):
        return default
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return default


def _plain(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (date, datetime)):
        return value.strftime("%d %b %Y")
    text = str(value).strip()
    return text if text else default


def _xml(value: Any, default: str = "&#8212;") -> str:
    text = _plain(value)
    if not text:
        return default
    return escape(text, quote=False).replace("\n", "<br/>")


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    return [value]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace("%", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _percent(value: Any) -> str:
    number = _number(value)
    if number is not None:
        return f"{number:.1f}%"
    return _plain(value, "\u2014")


def _normalise_report_type(value: Any) -> str:
    """Return the supported report kind, defaulting safely to monthly.

    Historical report JSON does not contain ``report_type``.  Treating every
    missing or unknown value as monthly keeps those saved reports rendering as
    they did before weekly reports were introduced.
    """
    text = _plain(value, "monthly").lower().replace("_", "-")
    if text in {"weekly", "week", "wtd", "week-to-date", "week to date"}:
        return "weekly"
    return "monthly"


def _report_type(report: Mapping[str, Any]) -> str:
    return _normalise_report_type(report.get("report_type"))


def _progress_report_title(report: Mapping[str, Any]) -> str:
    period_name = "Weekly" if _report_type(report) == "weekly" else "Monthly"
    return f"{period_name} Progress Report"


def _normalise_status(value: Any, *, report_type: str = "monthly") -> str:
    text = _plain(value, "draft").lower().replace("_", "-")
    if text in {"final", "issued", "approved"}:
        return "FINAL"
    if text in {"wtd", "week-to-date", "week to date"}:
        return "WTD"
    if text in {"mtd", "month-to-date", "month to date"}:
        return "WTD" if _normalise_report_type(report_type) == "weekly" else "MTD"
    if text in {"partial", "to-date", "to date"}:
        return "WTD" if _normalise_report_type(report_type) == "weekly" else "MTD"
    return "DRAFT"


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _styles() -> dict[str, ParagraphStyle]:
    base = dict(fontName="Helvetica", textColor=BLACK, splitLongWords=1)
    return {
        "body": ParagraphStyle(
            "MonthlyBody", fontSize=10.5, leading=14, spaceAfter=5, **base
        ),
        "body_center": ParagraphStyle(
            "MonthlyBodyCenter", parent=None, fontName="Helvetica", fontSize=10.5,
            leading=14, alignment=TA_CENTER, textColor=BLACK, splitLongWords=1,
        ),
        "small": ParagraphStyle(
            "MonthlySmall", fontSize=8.5, leading=10.5, spaceAfter=3, **base
        ),
        "small_center": ParagraphStyle(
            "MonthlySmallCenter", fontName="Helvetica", fontSize=8.5, leading=10.5,
            alignment=TA_CENTER, textColor=BLACK, splitLongWords=1,
        ),
        "small_right": ParagraphStyle(
            "MonthlySmallRight", fontName="Helvetica", fontSize=8.5, leading=10.5,
            alignment=TA_RIGHT, textColor=BLACK, splitLongWords=1,
        ),
        "table": ParagraphStyle(
            "MonthlyTable", fontName="Helvetica", fontSize=8.5, leading=10.5,
            textColor=BLACK, splitLongWords=1,
        ),
        "table_center": ParagraphStyle(
            "MonthlyTableCenter", fontName="Helvetica", fontSize=8.3, leading=9.8,
            alignment=TA_CENTER, textColor=BLACK, splitLongWords=1,
        ),
        "table_header": ParagraphStyle(
            "MonthlyTableHeader", fontName="Helvetica-Bold", fontSize=8.3,
            leading=9.6, alignment=TA_CENTER, textColor=BLACK, splitLongWords=1,
        ),
        "h1": ParagraphStyle(
            "MonthlyH1", fontName="Helvetica-Bold", fontSize=14, leading=17,
            textColor=BLACK, spaceBefore=0, spaceAfter=9, keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "MonthlyH2", fontName="Helvetica-Bold", fontSize=11.5, leading=14,
            textColor=BLACK, leftIndent=28.3, spaceBefore=5, spaceAfter=6,
            keepWithNext=True,
        ),
        "activity_area": ParagraphStyle(
            "MonthlyActivityArea", fontName="Helvetica-Bold", fontSize=10.5, leading=13,
            textColor=BLACK, leftIndent=14, spaceBefore=5, spaceAfter=2,
            keepWithNext=True,
        ),
        "appendix_item": ParagraphStyle(
            "MonthlyAppendixItem", fontName="Helvetica-Bold", fontSize=11.5,
            leading=15, textColor=BLACK, leftIndent=20, firstLineIndent=-20,
            spaceAfter=4, keepWithNext=False,
        ),
        "toc_title": ParagraphStyle(
            "MonthlyTOCTitle", fontName="Helvetica-Bold", fontSize=14, leading=16,
            alignment=TA_LEFT, textColor=BLACK, spaceAfter=6,
        ),
        "toc_subtitle": ParagraphStyle(
            "MonthlyTOCSubtitle", fontName="Helvetica", fontSize=16, leading=19,
            textColor=TOC_BLUE, spaceAfter=4,
        ),
        "placeholder": ParagraphStyle(
            "MonthlyPlaceholder", fontName="Helvetica-Oblique", fontSize=9.5,
            leading=13, textColor=MID_GREY, leftIndent=28.3, spaceAfter=6,
            splitLongWords=1,
        ),
    }


def _paragraph(value: Any, style: ParagraphStyle, default: str = "&#8212;") -> Paragraph:
    return Paragraph(_xml(value, default), style)


def _heading(text: str, style: ParagraphStyle, level: int) -> Paragraph:
    paragraph = Paragraph(escape(text, quote=False), style)
    paragraph._monthly_toc_level = level  # type: ignore[attr-defined]
    paragraph._monthly_toc_text = text  # type: ignore[attr-defined]
    return paragraph


def _display_heading(text: str, style: ParagraphStyle) -> Paragraph:
    """Render a visible heading without registering a TOC/outline entry.

    Photo-documentation pages can span many dates and continuation pages.  Those
    page-level labels stay visible in the appendix, while the Table of Contents
    carries only the single appendix entry (for example, ``6.2 Photo
    Documentation [Attached]``).
    """

    return Paragraph(escape(text, quote=False), style)


def _draw_outer_border(canvas: pdf_canvas.Canvas) -> None:
    x, y, width, height = OUTER_BORDER
    canvas.setStrokeColor(BLACK)
    canvas.setLineWidth(0.5)
    canvas.rect(x, y, width, height, stroke=1, fill=0)


def _status_color(status: str):
    return {
        "DRAFT": ORANGE,
        "MTD": CYAN,
        "WTD": CYAN,
        "FINAL": FINAL_GREEN,
    }.get(status, ORANGE)


def _draw_status_badge(
    canvas: pdf_canvas.Canvas, status: str, x: float, y: float, width: float = 55
) -> None:
    color = _status_color(status)
    canvas.saveState()
    canvas.setStrokeColor(color)
    canvas.setFillColor(WHITE)
    canvas.setLineWidth(1.2)
    canvas.roundRect(x, y, width, 16, 3, stroke=1, fill=1)
    canvas.setFillColor(color)
    canvas.setFont("Helvetica-Bold", 8.5)
    canvas.drawCentredString(x + width / 2, y + 4.2, status)
    canvas.restoreState()


def _draw_draft_watermark(canvas: pdf_canvas.Canvas) -> None:
    canvas.saveState()
    try:
        canvas.setFillAlpha(0.16)
    except (AttributeError, TypeError):
        pass
    canvas.setFillColor(ORANGE)
    canvas.translate(PAGE_WIDTH / 2, PAGE_HEIGHT / 2)
    canvas.rotate(28)
    canvas.setFont("Helvetica-Bold", 64)
    canvas.drawCentredString(0, 0, "DRAFT")
    canvas.setFont("Helvetica-Bold", 17)
    canvas.drawCentredString(0, -25, "SAMPLE DATA - NOT FOR ISSUE")
    canvas.restoreState()


def _draw_logo_fallback(
    canvas: pdf_canvas.Canvas, x: float, y: float, width: float, height: float
) -> None:
    canvas.saveState()
    canvas.setStrokeColor(ORANGE)
    canvas.setFillColor(WHITE)
    canvas.setLineWidth(2)
    canvas.roundRect(x, y, width, height, 4, stroke=1, fill=1)
    canvas.setFillColor(BLACK)
    canvas.setFont("Helvetica", min(11, max(7, height * 0.23)))
    canvas.drawCentredString(x + width / 2, y + height / 2 - 3, "LOGO VENDOR")
    canvas.restoreState()


def _draw_logo(
    canvas: pdf_canvas.Canvas,
    logo_path: str | os.PathLike[str] | None,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    if logo_path:
        try:
            path = os.fspath(logo_path)
            if os.path.isfile(path):
                image = ImageReader(path)
                image_width, image_height = image.getSize()
                if image_width > 0 and image_height > 0:
                    scale = min(width / image_width, height / image_height)
                    draw_width = image_width * scale
                    draw_height = image_height * scale
                    canvas.drawImage(
                        image,
                        x + (width - draw_width) / 2,
                        y + (height - draw_height) / 2,
                        draw_width,
                        draw_height,
                        preserveAspectRatio=True,
                        mask="auto",
                    )
                    return
        except (OSError, ValueError, TypeError):
            pass
    _draw_logo_fallback(canvas, x, y, width, height)


def _draw_box_paragraph(
    canvas: pdf_canvas.Canvas,
    text: Any,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    font_name: str = "Helvetica",
    font_size: float = 11,
    min_font_size: float = 7,
    alignment: int = TA_CENTER,
    text_color=BLACK,
) -> None:
    escaped = _xml(text)
    size = font_size
    paragraph = None
    paragraph_height = 0.0
    while size >= min_font_size:
        style = ParagraphStyle(
            "CanvasBox",
            fontName=font_name,
            fontSize=size,
            leading=size * 1.15,
            alignment=alignment,
            textColor=text_color,
            splitLongWords=1,
        )
        paragraph = Paragraph(escaped, style)
        _, paragraph_height = paragraph.wrap(width, height)
        if paragraph_height <= height:
            break
        size -= 0.5
    if paragraph is not None:
        paragraph.drawOn(canvas, x, y + max(0, (height - paragraph_height) / 2))


def _period_label(report: Mapping[str, Any]) -> str:
    direct = _plain(_value(report, "reporting_period", "report_period"))
    if direct:
        return direct
    period = report.get("period")
    if isinstance(period, Mapping):
        start = _plain(_value(period, "start", "date_from"))
        end = _plain(_value(period, "end", "date_to"))
        if start and end:
            return f"{start} to {end}"
        return start or end
    return ""


def _normalise_revision_rows(report: Mapping[str, Any]) -> list[dict[str, str]]:
    source = report.get("revision_rows", report.get("revisions", []))
    rows: list[dict[str, str]] = []
    for raw in _as_list(source):
        if not isinstance(raw, Mapping):
            continue
        rows.append({
            "rev": _plain(_value(raw, "rev", "revision")),
            "description": _plain(_value(raw, "description", "status")),
            "date": _plain(_value(raw, "date", "issued_date")),
            "prepared": _plain(_value(raw, "prepared", "prepared_by")),
            "checked": _plain(_value(raw, "checked", "checked_by")),
            "vendor_approved": _plain(
                _value(raw, "vendor_approved", "approved", "approved_by")
            ),
            "kn_approved": _plain(_value(raw, "kn_approved", "client_approved")),
        })
    if not rows and any(
        _plain(report.get(key))
        for key in ("prepared_by", "checked_by", "approved_by", "kn_approved_by")
    ):
        rows.append({
            "rev": _plain(_value(report, "revision", "rev")),
            "description": _plain(report.get("revision_description")),
            "date": _plain(_value(report, "issued_date", "issue_date")),
            "prepared": _plain(report.get("prepared_by")),
            "checked": _plain(report.get("checked_by")),
            "vendor_approved": _plain(report.get("approved_by")),
            "kn_approved": _plain(report.get("kn_approved_by")),
        })
    return rows[-3:]


def _draw_revision_table(canvas: pdf_canvas.Canvas, report: Mapping[str, Any]) -> None:
    styles = _styles()
    rows = _normalise_revision_rows(report)
    while len(rows) < 3:
        rows.append({key: "" for key in (
            "rev", "description", "date", "prepared", "checked",
            "vendor_approved", "kn_approved",
        )})

    def cell(value: Any, *, header: bool = False) -> Paragraph:
        style = ParagraphStyle(
            "RevisionHeader" if header else "RevisionCell",
            parent=styles["table_center"],
            fontName="Helvetica-Bold",
            fontSize=7.4 if header else 6.8,
            leading=8.2 if header else 7.7,
            textColor=ROYAL_BLUE,
        )
        return _paragraph(value, style, default="")

    table_data = [
        [cell("REV", header=True), cell("DESCRIPTION", header=True),
         cell("DATE", header=True), cell("VENDOR", header=True), "", "",
         cell("KN", header=True)],
        ["", "", "", cell("PREPARED", header=True), cell("CHECKED", header=True),
         cell("APVD", header=True), cell("APVD", header=True)],
    ]
    for row in rows:
        table_data.append([
            cell(row["rev"]), cell(row["description"]), cell(row["date"]),
            cell(row["prepared"]), cell(row["checked"]),
            cell(row["vendor_approved"]), cell(row["kn_approved"]),
        ])
    table_data.append(["", "", "", cell("CHECKED & REVIEWED BY KN", header=True), "", "", ""])

    table = Table(
        table_data,
        colWidths=[42.3, 185.8, 62.3, 61.0, 57.5, 62.3, 58.2],
        rowHeights=[15, 18, 26, 26, 26, 26.5],
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.72, ROYAL_BLUE),
        ("SPAN", (0, 0), (0, 1)),
        ("SPAN", (1, 0), (1, 1)),
        ("SPAN", (2, 0), (2, 1)),
        ("SPAN", (3, 0), (5, 0)),
        ("SPAN", (3, 5), (6, 5)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    table.wrapOn(canvas, 529.4, 137.5)
    table.drawOn(canvas, 35.4, 41.3)


def _draw_cover(
    canvas: pdf_canvas.Canvas,
    _doc: BaseDocTemplate,
    report: Mapping[str, Any],
    status: str,
    logo_path: str | os.PathLike[str] | None,
) -> None:
    canvas.saveState()
    report_title = _progress_report_title(report)
    canvas.setTitle(
        f"{report_title} - {_plain(_value(report, 'project_name', 'project_title'), 'Project')}"
    )
    _draw_outer_border(canvas)
    if status == "DRAFT":
        _draw_draft_watermark(canvas)
    _draw_status_badge(canvas, status, 35.4, 786.0)
    _draw_logo(canvas, logo_path, 415.5, 753.5, 128.25, 47.25)

    customer = _value(report, "customer", "client_name", default="Kertas Nusantara")
    _draw_box_paragraph(
        canvas, customer, 70, 661, PAGE_WIDTH - 140, 48,
        font_name="Helvetica-Bold", font_size=36, min_font_size=20,
    )
    _draw_box_paragraph(
        canvas, report_title.upper(), 70, 586, PAGE_WIDTH - 140, 35,
        font_name="Helvetica-Bold", font_size=20, min_font_size=14,
    )
    period = _period_label(report)
    period_text = f"({period})" if period and not period.startswith("(") else period
    _draw_box_paragraph(
        canvas, period_text, 110, 553, PAGE_WIDTH - 220, 28,
        font_name="Helvetica-Bold", font_size=18, min_font_size=11,
    )
    issued = _plain(_value(report, "issued_date", "issue_date"))
    _draw_box_paragraph(
        canvas, f"Issued on {issued}" if issued else "Issued on \u2014",
        100, 526, PAGE_WIDTH - 200, 27,
        font_name="Helvetica-Bold", font_size=18, min_font_size=11,
    )
    vendor_doc = _value(report, "vendor_doc_no", "document_no", "doc_no")
    _draw_box_paragraph(
        canvas, f"Vendor Doc No: {_plain(vendor_doc, '\u2014')}",
        150, 485, PAGE_WIDTH - 300, 27,
        font_name="Helvetica-Bold", font_size=14, min_font_size=9,
    )
    project_no = _value(report, "vendor_project_no", "project_no")
    _draw_box_paragraph(
        canvas, f"Vendor Project No.: {_plain(project_no, '\u2014')}",
        95, 373, PAGE_WIDTH - 190, 35,
        font_name="Helvetica-Bold", font_size=19, min_font_size=10,
    )
    project_name = _value(report, "project_name", "project_title", default="Project Name")
    _draw_box_paragraph(
        canvas, project_name, 70, 315, PAGE_WIDTH - 140, 64,
        font_name="Helvetica-Bold", font_size=23, min_font_size=10,
    )
    _draw_revision_table(canvas, report)
    canvas.restoreState()


def _draw_body_page(
    canvas: pdf_canvas.Canvas,
    _doc: BaseDocTemplate,
    report: Mapping[str, Any],
    status: str,
    logo_path: str | os.PathLike[str] | None,
) -> None:
    canvas.saveState()
    _draw_outer_border(canvas)
    if status == "DRAFT":
        _draw_draft_watermark(canvas)
    _draw_status_badge(canvas, status, 35.4, 790.0)
    canvas.setFillColor(BLACK)
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawCentredString(
        PAGE_WIDTH / 2 + 6, 792.8, _progress_report_title(report)
    )

    project_name = _value(report, "project_name", "project_title", default="Project Name")
    _draw_box_paragraph(
        canvas, project_name, 45.1, 752.0, 174, 29,
        font_name="Helvetica-Bold", font_size=11.5, min_font_size=7,
        alignment=TA_LEFT,
    )
    cod = _value(report, "cod", "project_code", "project_no")
    _draw_box_paragraph(
        canvas, f"({_plain(cod, 'COD')})", 224, 752.0, 95, 29,
        font_name="Helvetica-Bold", font_size=12.5, min_font_size=7,
    )
    vendor_doc = _value(report, "vendor_doc_no", "document_no", "doc_no")
    _draw_box_paragraph(
        canvas, f"Doc No (Vendor): {_plain(vendor_doc, '\u2014')}",
        337, 752.0, 128, 29,
        font_name="Helvetica-Bold", font_size=10.5, min_font_size=6.5,
        alignment=TA_LEFT,
    )
    _draw_logo(canvas, logo_path, 468.75, 776.75, 95.25, 32.25)
    canvas.setStrokeColor(BLACK)
    canvas.setLineWidth(0.5)
    canvas.line(43.7, 746.8, 537.3, 746.8)
    canvas.setLineWidth(0.75)
    canvas.line(33.75, 742.3, 562.5, 742.3)
    canvas.restoreState()


class _NumberedCanvas(pdf_canvas.Canvas):
    """Delay page output so the footer can contain the final page count."""

    def __init__(self, *args, status: str = "DRAFT", **kwargs):
        super().__init__(*args, **kwargs)
        self._monthly_status = status
        self._monthly_page_states: list[dict[str, Any]] = []

    def showPage(self):  # noqa: N802 - ReportLab API
        state = {
            key: value for key, value in self.__dict__.items()
            if key != "_monthly_page_states"
        }
        self._monthly_page_states.append(state)
        self._startPage()

    def save(self):
        total_pages = len(self._monthly_page_states)
        for state in self._monthly_page_states:
            self.__dict__.update(state)
            if self._pageNumber > 1:
                self._draw_monthly_footer(total_pages)
            pdf_canvas.Canvas.showPage(self)
        pdf_canvas.Canvas.save(self)

    def _draw_monthly_footer(self, total_pages: int) -> None:
        status = self._monthly_status
        self.saveState()
        self.setFillColor(_status_color(status))
        self.setFont("Helvetica-Bold", 8.5)
        text = f"{status}  |  Page {self._pageNumber} of {total_pages}"
        self.drawCentredString(PAGE_WIDTH / 2, 31.0, text)
        self.restoreState()


class _MonthlyDocTemplate(BaseDocTemplate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._monthly_heading_counter = 0

    def beforeDocument(self) -> None:  # noqa: N802 - ReportLab API
        # ``multiBuild`` makes several complete passes while resolving the
        # table of contents.  Stable bookmark keys are required for the TOC to
        # consider two consecutive passes identical.
        self._monthly_heading_counter = 0

    def afterFlowable(self, flowable: Flowable) -> None:  # noqa: N802 - ReportLab API
        level = getattr(flowable, "_monthly_toc_level", None)
        if level is None:
            return
        self._monthly_heading_counter += 1
        text = getattr(flowable, "_monthly_toc_text", "")
        key = f"monthly-heading-{self._monthly_heading_counter}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(text, key, level=level, closed=False)
        self.notify("TOCEntry", (level, text, self.page, key))


def _normalise_progress(value: Any) -> list[dict[str, Any]]:
    source = value.get("rows", []) if isinstance(value, Mapping) else value
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(_as_list(source), start=1):
        if not isinstance(raw, Mapping):
            continue
        previous = _number(_value(
            raw, "previous", "previous_actual", "cumulative_previous_actual", default=None
        ))
        this_month = _number(_value(
            raw,
            "this_month",
            "this_week",
            "this_period",
            "current_period",
            "this",
            "period",
            "this_week_actual",
            "this_period_actual",
            "current_period_actual",
            default=None,
        ))
        to_date = _number(_value(
            raw, "to_date", "cumulative", "cumulative_to_date_actual", default=None
        ))
        plan = _number(_value(
            raw, "plan", "to_date_plan", "cumulative_to_date_plan", default=None
        ))
        variance = _number(_value(raw, "variance", "deviation", default=None))
        rows.append({
            "description": _plain(raw.get("description"), f"Progress item {index}"),
            "previous": previous,
            "this_month": this_month,
            "to_date": to_date,
            "plan": plan,
            "variance": variance,
            "weight": _number(_value(raw, "weight", "weight_factor", default=None)),
            "is_total": bool(raw.get("is_total")),
        })
    return rows


def _progress_percent(value: Any) -> str:
    number = _number(value)
    return f"{number:.2f}%" if number is not None else "—"


def _progress_table(
    rows: list[dict[str, Any]],
    styles: Mapping[str, ParagraphStyle],
    *,
    report_type: str = "monthly",
    period_label: str = "",
) -> LongTable:
    display_rows = rows or [
        {"description": description, "previous": None, "this_month": None,
         "to_date": None, "plan": None, "variance": None}
        for description in ("Engineering", "Purchasing", "Manufacturing", "Delivery", "Total Overall")
    ]
    header = styles["table_header"]
    body = styles["table"]
    centered = styles["table_center"]
    source_period_label = _plain(period_label)
    current_period_label = (
        f"{source_period_label}\n(b)"
        if source_period_label
        else (
            "This Week\n(b)"
            if _normalise_report_type(report_type) == "weekly"
            else "This Month\n(b)"
        )
    )
    data: list[list[Any]] = [
        [_paragraph("Description", header), _paragraph("Progress", header), "", "",
         _paragraph("To-date Plan\n(d)", header),
         _paragraph("Reported Variance\n(e)", header)],
        ["", _paragraph("Previous\n(a)", header),
         _paragraph(current_period_label, header),
         _paragraph("To-date\n(c)", header), "", ""],
    ]
    for row in display_rows:
        data.append([
            _paragraph(row.get("description"), body),
            _paragraph(_progress_percent(row.get("previous")), centered),
            _paragraph(_progress_percent(row.get("this_month")), centered),
            _paragraph(_progress_percent(row.get("to_date")), centered),
            _paragraph(_progress_percent(row.get("plan")), centered),
            _paragraph(_progress_percent(row.get("variance")), centered),
        ])
    table = LongTable(
        data,
        colWidths=[108.0, 76.6, 81.0, 76.6, 72.0, 76.6],
        repeatRows=2,
        splitByRow=1,
        splitInRow=1,
        hAlign="CENTER",
    )
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.75, BLACK),
        ("BACKGROUND", (0, 0), (-1, 0), GREY_HEADER),
        ("BACKGROUND", (1, 1), (3, 1), GREY_SUBHEADER),
        ("SPAN", (0, 0), (0, 1)),
        ("SPAN", (1, 0), (3, 0)),
        ("SPAN", (4, 0), (4, 1)),
        ("SPAN", (5, 0), (5, 1)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for row_index, row in enumerate(display_rows, start=2):
        if row.get("is_total") or "total" in _plain(row.get("description")).lower():
            commands.extend([
                ("FONTNAME", (0, row_index), (-1, row_index), "Helvetica-Bold"),
                ("BACKGROUND", (0, row_index), (-1, row_index), LIGHT_GREY),
            ])
    table.setStyle(TableStyle(commands))
    return table


def _executive_summary(report: Mapping[str, Any], progress: list[dict[str, Any]]) -> str:
    supplied = _plain(report.get("executive_summary"))
    if supplied:
        return supplied
    total = next(
        (row for row in reversed(progress)
         if row.get("is_total") or "total" in row["description"].lower()),
        None,
    )
    if total and total.get("to_date") is not None and total.get("plan") is not None:
        variance = total.get("variance")
        summary = (
            "For this reporting period, overall project progress is "
            f"{_percent(total['to_date'])} compared with the plan of "
            f"{_percent(total['plan'])}. "
        )
        if variance is not None:
            summary = summary[:-2] + f", for a reported variance of {_percent(variance)}. "
        return summary + "The summary of progress and S-Curve are shown in the appendices."
    return (
        "Overall progress values have not been supplied for this reporting period. "
        "Complete and review the progress table before issue."
    )


def _safety_table(value: Any, styles: Mapping[str, ParagraphStyle]) -> Table:
    safety = value if isinstance(value, Mapping) else {}
    peak_headcount = _value(safety, "peak_daily_headcount")
    if _plain(peak_headcount):
        manpower_label = "Peak Daily Headcount"
        manpower_value = peak_headcount
    else:
        manpower_label = "Total Manpower"
        manpower_value = _value(safety, "total_manpower", "manpower")
    rows = [
        (manpower_label, manpower_value),
        ("Total Man hours", _value(safety, "total_man_hours", "man_hours")),
        ("Total Recordable Cases", _value(
            safety, "total_recordable_cases", "recordable_cases"
        )),
        ("Lost Workdays", safety.get("lost_workdays")),
        ("Lost Time Injuries", safety.get("lost_time_injuries")),
        ("Severity Rate", safety.get("severity_rate")),
        ("Average Day Away", safety.get("average_day_away")),
    ]
    table_data = []
    for label, raw in rows:
        rendered = _plain(raw, "Not supplied")
        if label == "Total Man hours" and rendered != "Not supplied" and not rendered.upper().endswith("MH"):
            rendered += " MH"
        table_data.append([
            _paragraph("\u2022", styles["body_center"], default=""),
            _paragraph(label, styles["body"]),
            _paragraph(":", styles["body_center"]),
            _paragraph(rendered, styles["body"]),
        ])
    table = Table(table_data, colWidths=[18, 145, 24, 150], hAlign="CENTER")
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _mapping_summary(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _value(value, "summary", "text", "description", "remarks")
    return value


def _describe_mapping(value: Mapping[str, Any]) -> str:
    preferred = _activity_display_text(value)
    if preferred:
        area = _plain(_value(value, "area", "location"))
        return f"{area}: {preferred}" if area else preferred
    pairs = []
    for key, item in value.items():
        if item in (None, "", [], {}):
            continue
        label = str(key).replace("_", " ").strip().title()
        pairs.append(f"{label}: {_plain(item)}")
    return "; ".join(pairs)


def _activity_display_text(value: Mapping[str, Any]) -> str:
    text = _plain(_value(value, "text", "description", "activity", "title", "name"))
    if not text:
        return ""
    status = _plain(value.get("status"))
    if status and status.casefold() not in text.casefold():
        return f"{text} — {status}"
    return text




def _coverage_flowables(report: Mapping[str, Any], styles: Mapping[str, ParagraphStyle]) -> list[Flowable]:
    coverage = report.get("coverage") if isinstance(report.get("coverage"), Mapping) else {}
    expected = [str(x) for x in _as_list(coverage.get("expected_dates")) if str(x).strip()]
    covered = [str(x) for x in _as_list(coverage.get("covered_dates", coverage.get("found_dates"))) if str(x).strip()]
    missing = [str(x) for x in _as_list(coverage.get("missing_dates")) if str(x).strip()]
    if not expected and not covered and not missing:
        return []
    total = len(expected) if expected else len(set(covered + missing))
    text = f"Daily Report Coverage: {len(covered)} / {total or len(covered)} day(s)"
    if covered:
        text += f" | Available: {', '.join(covered)}"
    if missing:
        text += f" | Missing: {', '.join(missing)}"
    else:
        text += " | Missing: None"
    return [
        Paragraph("<b>Source Coverage</b>", styles["body"]),
        _paragraph(text, styles["placeholder"]),
        Spacer(1, 6),
    ]

def _client_facing_missing_summary(value: Any, *, section: str) -> Any:
    """Replace internal manual-input placeholders with client-facing wording."""

    section_key = str(section or "").strip().casefold()
    message = (
        "No engineering status data was supplied in the available Daily Reports."
        if section_key == "engineering"
        else "No procurement status data was supplied in the available Daily Reports."
    )
    placeholders = {
        "",
        "not supplied",
        "manual input required",
        "manual input required.",
        "manual weekly input required",
        "manual weekly input required.",
        "manual monthly input required",
        "manual monthly input required.",
    }

    if isinstance(value, Mapping):
        result = dict(value)
        summary = _plain(result.get("summary")).strip()
        if summary.casefold() in placeholders:
            result["summary"] = message
        return result

    plain = _plain(value).strip()
    if plain.casefold() in placeholders:
        return message
    return value


def _content_flowables(
    value: Any,
    styles: Mapping[str, ParagraphStyle],
    *,
    empty_message: str,
    bullets: bool = False,
) -> list[Flowable]:
    if isinstance(value, Mapping) and any(key in value for key in ("summary", "items", "rows")):
        values: list[Any] = []
        summary = _plain(value.get("summary"))
        if summary:
            values.append(summary)
        values.extend(_as_list(_value(value, "items", "rows", default=[])))
    else:
        values = _as_list(value)

    rendered: list[Flowable] = []
    for item in values:
        if isinstance(item, Mapping):
            text = _describe_mapping(item)
        else:
            text = _plain(item)
        if not text:
            continue
        if bullets:
            rendered.append(Paragraph(
                f"&#8226;&nbsp;&nbsp;{_xml(text)}", styles["body"]
            ))
        else:
            rendered.append(_paragraph(text, styles["body"]))
    if not rendered:
        rendered.append(_paragraph(empty_message, styles["placeholder"]))
    return rendered


_ACTIVITY_WORKSTREAM_PREFIXES = (
    "Oil System & Flushing",
    "Mechanical Maintenance",
    "Instrumentation & Electrical",
    "Actuator & Pneumatic",
    "Valve Mechanical",
    "Testing & Commissioning",
    "Standby / Coordination",
    "Other Site Work",
)


def _strip_generated_activity_prefixes(value: Any, *, area: str = "", workstream: str = "") -> str:
    """Last-resort cleanup for old/reviewed drafts with nested display labels."""

    text = _plain(value).strip()
    if not text:
        return ""
    if area:
        for dash in (" – ", " - ", ": "):
            prefix = area + dash
            if text.casefold().startswith(prefix.casefold()):
                text = text[len(prefix):].lstrip()
                break
    ordered = [workstream] if workstream else []
    ordered.extend(label for label in _ACTIVITY_WORKSTREAM_PREFIXES if label and label not in ordered)
    for _ in range(4):
        changed = False
        for label in ordered:
            prefix = label + ":"
            if text.casefold().startswith(prefix.casefold()):
                text = text[len(prefix):].lstrip(" -–:;")
                changed = True
                break
        if not changed:
            break
    return text.strip()


def _activity_flowables(
    value: Any,
    styles: Mapping[str, ParagraphStyle],
    *,
    empty_message: str,
) -> list[Flowable]:
    """Render period activities as concise ``Area – Workstream`` bullets.

    Area/workstream metadata comes from the deterministic compiler.  Claude may
    polish only the narrative body.  Generic placeholders such as ``Site`` are
    never rendered as stand-alone headings.
    """

    rows = _as_list(value)
    structured = [
        row for row in rows
        if isinstance(row, Mapping) and _activity_display_text(row)
    ]
    if not structured:
        return _content_flowables(value, styles, empty_message=empty_message, bullets=True)

    rendered: list[Flowable] = []
    seen: set[str] = set()
    generic_areas = {"", "site", "general", "all areas", "all area"}
    for row in rows:
        if not isinstance(row, Mapping):
            text = _plain(row)
            if text and text.casefold() not in seen:
                rendered.append(Paragraph(f"&#8226;&nbsp;&nbsp;{_xml(text)}", styles["body"]))
                seen.add(text.casefold())
            continue

        text = _activity_display_text(row)
        if not text:
            continue
        area = _plain(_value(row, "area", "location")).strip()
        workstream = _plain(row.get("workstream")).strip()
        if area.casefold() in generic_areas:
            area = ""

        detail = _strip_generated_activity_prefixes(text, area=area, workstream=workstream)
        if not detail:
            continue

        if area and workstream:
            display = f"{area} – {workstream}: {detail}"
        elif area:
            display = f"{area} – {detail}"
        elif workstream:
            display = f"{workstream}: {detail}" if not detail.casefold().startswith((workstream + ":").casefold()) else detail
        else:
            display = detail

        key = display.casefold()
        if key in seen:
            continue
        seen.add(key)
        rendered.append(Paragraph(f"&#8226;&nbsp;&nbsp;{_xml(display)}", styles["body"]))

    if not rendered:
        rendered.append(_paragraph(empty_message, styles["placeholder"]))
    return rendered


def _po_rows(value: Any) -> tuple[Any, list[Mapping[str, Any]], Any, Any]:
    if not isinstance(value, Mapping):
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return None, [row for row in value if isinstance(row, Mapping)], None, None
        return value, [], None, None
    rows = _value(value, "rows", "purchase_orders", "pos", default=[])
    return (
        value.get("summary"),
        [row for row in _as_list(rows) if isinstance(row, Mapping)],
        value.get("equipment_delivery"),
        value.get("shipments"),
    )


def _procurement_table(rows: list[Mapping[str, Any]], styles: Mapping[str, ParagraphStyle]) -> LongTable:
    header = styles["table_header"]
    body = styles["table"]
    data: list[list[Any]] = [[
        _paragraph("PO #", header),
        _paragraph("PO Name", header),
        _paragraph("Supplier/Vendor/\nContractor", header),
        _paragraph("PO Status", header),
    ]]
    if rows:
        for row in rows:
            data.append([
                _paragraph(_value(row, "po_number", "po_no", "po", "number"), body),
                _paragraph(_value(row, "po_name", "name", "description"), body),
                _paragraph(_value(row, "supplier", "vendor", "contractor"), body),
                _paragraph(_value(row, "status", "po_status"), body),
            ])
    else:
        data.append([_paragraph("No purchase-order data supplied.", styles["placeholder"]), "", "", ""])
    table = LongTable(
        data, colWidths=[72.5, 103.3, 98.9, 234.7], repeatRows=1,
        splitByRow=1, splitInRow=1, hAlign="CENTER",
    )
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("BACKGROUND", (0, 0), (-1, 0), CYAN),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if not rows:
        commands.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(commands))
    return table


def _equipment_table(rows: list[Mapping[str, Any]], styles: Mapping[str, ParagraphStyle]) -> LongTable:
    header = styles["table_header"]
    body = styles["table"]
    data: list[list[Any]] = [[
        _paragraph("Equipment", header), _paragraph("Supplier", header),
        _paragraph("Status", header), _paragraph("Expected Delivery", header),
        _paragraph("Actual Delivery", header),
    ]]
    for row in rows:
        data.append([
            _paragraph(_value(row, "equipment", "name", "description"), body),
            _paragraph(_value(row, "supplier", "vendor"), body),
            _paragraph(row.get("status"), body),
            _paragraph(_value(row, "expected_delivery", "eta", "expected"), body),
            _paragraph(_value(row, "actual_delivery", "actual"), body),
        ])
    table = LongTable(
        data, colWidths=[135, 95, 90, 95, 95], repeatRows=1,
        splitByRow=1, splitInRow=1, hAlign="CENTER",
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("BACKGROUND", (0, 0), (-1, 0), GREY_HEADER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _shipment_table(rows: list[Mapping[str, Any]], styles: Mapping[str, ParagraphStyle]) -> LongTable:
    header = styles["table_header"]
    body = styles["table"]
    centered = styles["table_center"]
    data: list[list[Any]] = [[
        _paragraph("Shipment No.", header), _paragraph("Description", header),
        _paragraph("PO Number", header), _paragraph("Expected Loading Port", header),
        _paragraph("Arriving Port", header), _paragraph("ETD", header),
        _paragraph("Actual departure date", header),
    ]]
    if rows:
        for index, row in enumerate(rows, start=1):
            data.append([
                _paragraph(_value(row, "shipment_no", "number", default=index), centered),
                _paragraph(row.get("description"), body),
                _paragraph(_value(row, "po_number", "po_no", "po"), body),
                _paragraph(_value(row, "expected_loading_port", "loading_port"), body),
                _paragraph(_value(row, "arriving_port", "arrival_port"), body),
                _paragraph(_value(row, "etd", "expected_departure"), centered),
                _paragraph(_value(row, "actual_departure_date", "actual_departure"), centered),
            ])
    else:
        data.append([_paragraph("No shipment data supplied.", styles["placeholder"]), "", "", "", "", "", ""])
    table = LongTable(
        data, colWidths=[58.8, 98.9, 55.9, 65.6, 67.7, 76.5, 78.1],
        repeatRows=1, splitByRow=1, splitInRow=1, hAlign="CENTER",
    )
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("BACKGROUND", (0, 0), (-1, 0), GREY_HEADER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if not rows:
        commands.append(("SPAN", (0, 1), (-1, 1)))
    table.setStyle(TableStyle(commands))
    return table


def _constraint_reporting_message(report: Mapping[str, Any], site: Mapping[str, Any]) -> str:
    reporting = site.get("constraint_reporting")
    if not isinstance(reporting, Mapping):
        reporting = report.get("constraint_reporting")
    if not isinstance(reporting, Mapping):
        return "Concern and closeout information was not supplied."
    none_dates = _as_list(reporting.get("none_reported_dates"))
    reported_dates = _as_list(reporting.get("reported_dates"))
    missing_dates = _as_list(reporting.get("not_supplied_dates"))
    if none_dates and not reported_dates and not missing_dates:
        findings = _as_list(site.get("key_findings"))
        if findings:
            return "No formal constraints were reported in the available Daily Reports. Key remarks/findings are shown below."
        return "No formal constraints were reported in the available Daily Reports."
    return "Concern and closeout information was not supplied."



def _key_findings_table(rows: list[Any], styles: Mapping[str, ParagraphStyle]) -> LongTable | None:
    data: list[list[Any]] = [[
        _paragraph("Date / Area", styles["table_header"]),
        _paragraph("Key Remark / Finding", styles["table_header"]),
    ]]
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        text = _plain(_value(raw, "text", "remark", "description"))
        if not text:
            continue
        date = _plain(_value(raw, "date", "source_date"))
        area = _plain(raw.get("area"))
        label = " / ".join(value for value in (date, area) if value) or "Source remark"
        data.append([
            _paragraph(label, styles["table"]),
            _paragraph(text, styles["table"]),
        ])
    if len(data) == 1:
        return None
    table = LongTable(data, colWidths=[110, 360], repeatRows=1, splitByRow=1, splitInRow=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("BACKGROUND", (0, 0), (-1, 0), GREY_HEADER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _concerns_table(rows: list[Any], styles: Mapping[str, ParagraphStyle]) -> LongTable | None:
    data: list[list[Any]] = [[
        _paragraph("Area of Concern", styles["table_header"]),
        _paragraph("Source-Recorded Action / Follow-up", styles["table_header"]),
    ]]
    for raw in rows:
        if isinstance(raw, Mapping):
            concern = _value(raw, "concern", "text", "description")
            action = _value(raw, "corrective_action", "action", "suggested_action")
        else:
            concern, action = raw, ""
        if _plain(concern) or _plain(action):
            data.append([
                _paragraph(concern, styles["table"]),
                _paragraph(action, styles["table"]),
            ])
    if len(data) == 1:
        return None
    table = LongTable(
        data, colWidths=[BODY_WIDTH * 0.47, BODY_WIDTH * 0.53],
        repeatRows=1, splitByRow=1, splitInRow=1,
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("BACKGROUND", (0, 0), (-1, 0), GREY_HEADER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _normalise_s_curve(
    value: Any, progress: list[dict[str, Any]]
) -> tuple[list[str], list[float], list[float], bool] | None:
    if isinstance(value, Mapping):
        labels = [_plain(item) for item in _as_list(value.get("labels"))]
        plan_raw = _as_list(_value(value, "plan", "planned", default=[]))
        actual_raw = _as_list(value.get("actual"))
        count = min(len(labels), len(plan_raw), len(actual_raw))
        if count >= 2:
            plan = [_number(item) for item in plan_raw[:count]]
            actual = [_number(item) for item in actual_raw[:count]]
            if all(item is not None for item in plan + actual):
                approved = _coerce_bool(value.get("approved"), False)
                illustrative = _coerce_bool(value.get("illustrative"), False) or not approved
                return labels[:count], [float(item) for item in plan], [float(item) for item in actual], illustrative

    total = next(
        (row for row in reversed(progress)
         if row.get("is_total") or "total" in row["description"].lower()),
        progress[-1] if progress else None,
    )
    if not total:
        return None
    previous = total.get("previous")
    actual_now = total.get("to_date")
    plan_now = total.get("plan")
    if actual_now is None and previous is None:
        return None
    previous = float(previous or 0)
    actual_now = float(actual_now if actual_now is not None else previous)
    plan_now = float(plan_now if plan_now is not None else actual_now)
    return (
        ["Start", "Previous", "Current"],
        [0.0, min(previous, plan_now), plan_now],
        [0.0, previous, actual_now],
        True,
    )


class _SCurveFlowable(Flowable):
    def __init__(
        self,
        labels: list[str],
        planned: list[float],
        actual: list[float],
        *,
        illustrative: bool,
        width: float = 490,
        height: float = 285,
    ):
        super().__init__()
        self.width = width
        self.height = height
        self.labels = labels
        self.planned = planned
        self.actual = actual
        self.illustrative = illustrative

    def draw(self):
        canvas = self.canv
        left, bottom = 42.0, 43.0
        chart_width = self.width - left - 18
        chart_height = self.height - bottom - 42
        maximum = max([100.0, *self.planned, *self.actual])
        y_max = max(100.0, math.ceil(maximum / 20.0) * 20.0)

        canvas.saveState()
        canvas.setFillColor(BLACK)
        canvas.setFont("Helvetica-Bold", 12)
        title = "Progress S-Curve"
        if self.illustrative:
            title += " (Illustrative Snapshot)"
        canvas.drawCentredString(self.width / 2, self.height - 18, title)

        canvas.setStrokeColor(colors.HexColor("#D1D5DB"))
        canvas.setFillColor(MID_GREY)
        canvas.setFont("Helvetica", 7)
        for tick in range(6):
            value = y_max * tick / 5
            y = bottom + chart_height * tick / 5
            canvas.line(left, y, left + chart_width, y)
            canvas.drawRightString(left - 5, y - 2.5, f"{value:.0f}%")

        canvas.setStrokeColor(BLACK)
        canvas.setLineWidth(0.8)
        canvas.line(left, bottom, left, bottom + chart_height)
        canvas.line(left, bottom, left + chart_width, bottom)

        count = len(self.labels)
        x_positions = [
            left + (chart_width * index / max(1, count - 1))
            for index in range(count)
        ]
        canvas.setFillColor(BLACK)
        for index, (x, label) in enumerate(zip(x_positions, self.labels)):
            if count > 10 and index not in {0, count - 1} and index % math.ceil(count / 8) != 0:
                continue
            canvas.drawCentredString(x, bottom - 13, label[:18])

        def draw_series(values: list[float], color, label: str, legend_x: float):
            canvas.setStrokeColor(color)
            canvas.setFillColor(color)
            canvas.setLineWidth(2)
            points = [
                (x, bottom + chart_height * max(0.0, min(y_max, value)) / y_max)
                for x, value in zip(x_positions, values)
            ]
            path = canvas.beginPath()
            path.moveTo(*points[0])
            for point in points[1:]:
                path.lineTo(*point)
            canvas.drawPath(path, stroke=1, fill=0)
            for x, y in points:
                canvas.circle(x, y, 2.3, stroke=1, fill=1)
            legend_y = 15
            canvas.line(legend_x, legend_y, legend_x + 20, legend_y)
            canvas.setFont("Helvetica", 8)
            canvas.drawString(legend_x + 25, legend_y - 3, label)

        draw_series(self.planned, CYAN, "Plan", self.width / 2 - 95)
        draw_series(self.actual, ORANGE, "Actual", self.width / 2 + 20)
        if self.illustrative:
            canvas.setFillColor(ORANGE)
            canvas.setFont("Helvetica-Bold", 7.5)
            canvas.drawCentredString(
                self.width / 2,
                1,
                "ILLUSTRATIVE ONLY - provide an approved time series before final issue",
            )
        canvas.restoreState()


def _normalise_appendices(value: Any, *, has_s_curve: bool) -> list[dict[str, Any]]:
    # ``source_number`` is the stable internal appendix slot.  ``number`` is the
    # display number and may later be reassigned after hidden appendices are
    # removed.  Keeping both lets future data still map to the original 6.1-6.8
    # definitions without forcing gaps in the issued report numbering.
    appendices = [
        {
            "source_number": number,
            "number": number,
            "title": title,
            "status": "Not supplied",
            "content": None,
        }
        for number, title in DEFAULT_APPENDICES
    ]
    by_source_number = {item["source_number"]: item for item in appendices}
    next_index = 9
    for raw in _as_list(value):
        if isinstance(raw, Mapping):
            source_number = _plain(raw.get("source_number") or raw.get("number"))
            title = _plain(_value(raw, "title", "name", "description"))
            status = _plain(raw.get("status"))
            content = _value(raw, "content", "items", "notes", default=None)
        else:
            source_number, title, status, content = "", _plain(raw), "", None
        if not title and not source_number:
            continue
        if source_number in by_source_number:
            item = by_source_number[source_number]
            if title:
                item["title"] = title
            if status:
                item["status"] = status
            if content not in (None, "", [], {}):
                item["content"] = content
                if not status:
                    item["status"] = "Included"
            continue
        source_number = source_number or f"6.{next_index}"
        item = {
            "source_number": source_number,
            "number": source_number,
            "title": title or "Appendix",
            "status": status or ("Included" if content else "Not supplied"),
            "content": content,
        }
        next_index += 1
        appendices.append(item)
        by_source_number[source_number] = item
    if has_s_curve and "6.2" in by_source_number:
        by_source_number["6.2"]["status"] = "Included (generated)"
    return appendices


def _appendix_source_number(item: Mapping[str, Any]) -> str:
    return _plain(item.get("source_number") or item.get("number"))


def _renumber_visible_appendices(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign contiguous display numbers to appendices that will be rendered.

    The stable ``source_number`` remains unchanged, so internal mapping still
    knows that manpower originated from slot 6.5, photographs from 6.6, etc.
    Only the client-facing number is compacted to 6.1, 6.2, ... .
    """

    for index, item in enumerate(items, start=1):
        item["number"] = f"6.{index}"
    return items


def _appendix_label(item: Mapping[str, Any]) -> str:
    number = _plain(item.get("number"))
    title = _plain(item.get("title"), "Appendix")
    status = _plain(item.get("status"))
    label = f"{number}    {title}".strip()
    # Photo Documentation is a single navigational appendix entry.  Its
    # attachment state is self-evident from the rendered photo pages, so keep
    # the TOC/list label concise instead of showing ``[Attached]``.
    if status and _appendix_source_number(item) != "6.6":
        label += f"  [{status}]"
    return label


def _appendix_is_visible(item: Mapping[str, Any]) -> bool:
    """Return True when an appendix should be shown in the issued report.

    Appendix definitions are intentionally retained even when they are not
    supplied.  This keeps the fixed 6.1-6.8 structure available for future
    reports, while preventing empty ``[Not supplied]`` rows from cluttering the
    current report and its table of contents.

    Content always wins over a stale status value: if an appendix has actual
    content, it remains visible even if its status was not updated correctly.
    """

    content = item.get("content")
    if content not in (None, "", [], {}):
        return True

    status = _plain(item.get("status")).strip().casefold()
    return status not in {"", "not supplied"}


def _photo_grid_flowables(
    photos: list[Any],
    styles: Mapping[str, ParagraphStyle],
    *,
    photo_base_dir: str | os.PathLike[str] | None,
) -> list[Flowable]:
    """Render reviewed photos grouped by source date, up to nine cards per page.

    Weekly/Monthly reports are easier to audit when each Daily Report date has a
    visible photo block. Exact duplicates within the same Daily source are removed,
    while an identical asset reused by a different Daily Report date is retained as
    a separate dated reference for source traceability.
    """

    if photo_base_dir is None:
        return [Paragraph("Photo assets are unavailable.", styles["placeholder"])]
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return [Paragraph("Photo rendering requires Pillow.", styles["placeholder"])]

    root = os.path.realpath(os.fspath(photo_base_dir))
    prepared: list[Mapping[str, Any]] = []
    seen_references: set[tuple[str, str, str]] = set()
    for raw in photos:
        if not isinstance(raw, Mapping):
            continue
        asset_id = _plain(raw.get("asset_id"))
        if not is_asset_id(asset_id):
            continue
        reference_key = (
            asset_id,
            _plain(raw.get("source_report_id")),
            _plain(raw.get("source_date")),
        )
        if reference_key in seen_references:
            continue
        seen_references.add(reference_key)
        prepared.append(raw)

    prepared.sort(key=lambda row: (
        _plain(row.get("source_date"), "9999-99-99"),
        int(_number(row.get("order")) or 0),
        _plain(row.get("source")),
        int(_number(row.get("page")) or 0),
    ))

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    group_order: list[str] = []
    for row in prepared:
        source_date = _plain(row.get("source_date"), "Undated source")
        if source_date not in grouped:
            grouped[source_date] = []
            group_order.append(source_date)
        grouped[source_date].append(row)

    def clean_caption(value: Any, fallback: str = "") -> str:
        text = _plain(value, fallback)
        if not text:
            return ""
        # Last-resort protection for legacy drafts imported before the parser
        # boilerplate fix.  Never show page headers/footers inside photo captions.
        text = re.sub(
            r"\s+(?:PT\.?\s+GARUDA\s+PRIMA\s+AKSARA|T\.\s*Garuda\s+Prima\s+Aksara|"
            r"Daily\s+Activity\s+Report|aily\s+Activity\s+Report)\b.*$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        return text

    def build_grid(items: list[Mapping[str, Any]]) -> Table | None:
        columns = 3
        cell_width = BODY_WIDTH / columns
        image_width = cell_width - 8
        image_height = 122.0
        rows: list[list[Any]] = []
        current: list[Any] = []

        # Keep every photo, but avoid printing the same activity caption on all
        # nine cards of a page.  The first card in each repeated Area+Caption group
        # carries the full caption; subsequent cards keep the area label and image.
        # The group heading naturally repeats on a continuation page because each
        # page chunk is built independently.
        caption_counts: Counter[tuple[str, str]] = Counter()
        for candidate in items:
            area_key = clean_caption(candidate.get("source_area")) if isinstance(candidate, Mapping) else ""
            caption_key = clean_caption(candidate.get("caption")) if isinstance(candidate, Mapping) else ""
            caption_counts[(area_key.casefold(), caption_key.casefold())] += 1
        seen_caption_groups: set[tuple[str, str]] = set()

        for raw in items:
            asset_id = _plain(raw.get("asset_id"))
            path = os.path.realpath(os.path.join(root, asset_filename(asset_id)))
            if os.path.dirname(path) != root or not os.path.isfile(path):
                continue
            try:
                with Image.open(path) as opened:
                    if str(opened.format or "").upper() != "JPEG":
                        continue
                    image = ImageOps.exif_transpose(opened).convert("RGB")
                    fitted = ImageOps.fit(
                        image,
                        (max(1, int(image_width * 2)), max(1, int(image_height * 2))),
                        method=Image.Resampling.LANCZOS,
                    )
                    image_buffer = io.BytesIO()
                    fitted.save(image_buffer, format="JPEG", quality=82, optimize=True)
                    image_buffer.seek(0)
                rendered_image: Any = RLImage(image_buffer, width=image_width, height=image_height)
            except Exception:
                continue

            source = _plain(raw.get("source"))
            page = _plain(raw.get("page"))
            area = clean_caption(raw.get("source_area"))
            # A current Daily Report photo card may intentionally have no
            # caption. Do not replace that source-empty caption with a filename
            # or page reference; provenance is already carried in the dated
            # appendix grouping and source metadata.
            photo_card = (
                _plain(raw.get("context_type")).casefold() == "photo_card"
                or _plain(raw.get("photo_match_method")).casefold() == "photo_card_geometry"
            )
            fallback = "" if photo_card else (f"{source} - p.{page}" if source and page else source)
            caption = clean_caption(raw.get("caption"), fallback)
            caption_group = (area.casefold(), caption.casefold())
            repeated_caption = bool(caption) and caption_counts.get(caption_group, 0) > 1
            show_caption = caption
            if repeated_caption and caption_group in seen_caption_groups:
                show_caption = ""
            elif repeated_caption:
                seen_caption_groups.add(caption_group)

            card_rows: list[list[Any]] = []
            if area:
                card_rows.append([Paragraph(f"<b>{_xml(area)}</b>", styles["small"])])
            if caption:
                caption_markup = f"<i>{_xml(show_caption)}</i>" if show_caption else "&#160;"
                card_rows.append([Paragraph(caption_markup, styles["small"])])
            card_rows.append([rendered_image])
            card = Table(card_rows, colWidths=[cell_width - 4])
            card_style = [
                ("BOX", (0, 0), (-1, -1), 0.55, CYAN),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
            if area:
                card_style.extend([
                    ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#DDEBF7")),
                    ("LINEBELOW", (0, 0), (0, 0), 0.35, CYAN),
                ])
                if caption:
                    card_style.append(("BACKGROUND", (0, 1), (0, 1), LIGHT_GREY))
            elif caption:
                card_style.append(("BACKGROUND", (0, 0), (0, 0), LIGHT_GREY))
            card.setStyle(TableStyle(card_style))
            current.append(card)
            if len(current) == columns:
                rows.append(current)
                current = []

        if current:
            current.extend([""] * (columns - len(current)))
            rows.append(current)
        if not rows:
            return None
        grid = Table(rows, colWidths=[cell_width] * columns, hAlign="LEFT", splitByRow=1)
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return grid

    result: list[Flowable] = []
    for date_index, source_date in enumerate(group_order):
        items = grouped[source_date]
        for chunk_index in range(0, len(items), 9):
            chunk = items[chunk_index:chunk_index + 9]
            if result:
                result.append(PageBreak())
            continuation = " (continued)" if chunk_index else ""
            result.append(_display_heading(
                f"Photo Documentation: {source_date}{continuation}",
                styles["h2"],
            ))
            grid = build_grid(chunk)
            if grid is not None:
                result.append(grid)

    if not result:
        return [Paragraph("No selected photo assets are available.", styles["placeholder"])]
    return result

def _manpower_appendix_flowables(
    manpower: Any,
    styles: Mapping[str, ParagraphStyle],
) -> list[Flowable]:
    """Render deterministic attendance and man-hour details in Appendix 6.5."""

    value = manpower if isinstance(manpower, Mapping) else {}
    totals = value.get("totals") if isinstance(value.get("totals"), Mapping) else {}
    daily = [row for row in _as_list(value.get("daily")) if isinstance(row, Mapping)]
    roles = [row for row in _as_list(value.get("roles")) if isinstance(row, Mapping)]
    header = styles["table_header"]
    body = styles["table"]
    center = styles["table_center"]

    def person_days_text(value: Any) -> Any:
        number = _number(value)
        if number is None:
            return value
        return int(number) if float(number).is_integer() else number

    summary_rows = [
        ["Category", "Person-days", "Regular MH", "OT MH", "Total MH"],
        [
            "Direct", person_days_text(totals.get("direct_person_days", "Not supplied")),
            totals.get("regular_direct_man_hours", totals.get("direct_man_hours", "Not supplied")),
            totals.get("direct_overtime_man_hours", "Not supplied"), totals.get("direct_man_hours", "Not supplied"),
        ],
        [
            "Indirect", person_days_text(totals.get("indirect_person_days", "Not supplied")),
            totals.get("regular_indirect_man_hours", totals.get("indirect_man_hours", "Not supplied")),
            totals.get("indirect_overtime_man_hours", "Not supplied"), totals.get("indirect_man_hours", "Not supplied"),
        ],
        [
            "Total", person_days_text(totals.get("total_person_days", "Not supplied")),
            totals.get("regular_man_hours", totals.get("total_man_hours", "Not supplied")),
            totals.get("overtime_man_hours", "Not supplied"), totals.get("total_man_hours", "Not supplied"),
        ],
    ]
    summary = Table(
        [[_paragraph(cell, header if row_index == 0 else (body if column_index == 0 else center))
          for column_index, cell in enumerate(row)] for row_index, row in enumerate(summary_rows)],
        colWidths=[118, 86, 90, 82, 90], repeatRows=1, hAlign="CENTER",
    )
    summary.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("BACKGROUND", (0, 0), (-1, 0), GREY_HEADER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    result: list[Flowable] = [summary, Spacer(1, 10)]
    supplied_days = _number(totals.get("manpower_supplied_day_count"))
    covered_days = _number(totals.get("covered_daily_report_count"))
    expected_days = _number(totals.get("expected_day_count"))
    average_headcount = _number(totals.get("average_daily_headcount"))
    peak_headcount = _number(totals.get("peak_headcount"))
    not_supplied_days = _number(totals.get("manpower_not_supplied_day_count"))
    def metric_text(number: float | None) -> str:
        if number is None:
            return "Not supplied"
        return str(int(number)) if float(number).is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")

    metric_parts: list[str] = []
    if covered_days is not None and expected_days is not None:
        metric_parts.append(f"Daily Report coverage: {metric_text(covered_days)}/{metric_text(expected_days)} days")
    if supplied_days is not None:
        metric_parts.append(f"manpower supplied: {metric_text(supplied_days)} days")
    if average_headcount is not None:
        metric_parts.append(f"average daily headcount: {metric_text(average_headcount)}")
    if peak_headcount is not None:
        metric_parts.append(f"peak daily headcount: {metric_text(peak_headcount)}")
    if metric_parts:
        result.extend([
            _paragraph("; ".join(metric_parts) + ".", styles["placeholder"]),
            Spacer(1, 6),
        ])
    if not_supplied_days is not None and not_supplied_days > 0:
        result.extend([
            _paragraph(
                f"Manpower was not supplied for {metric_text(not_supplied_days)} covered Daily Report date(s); "
                "averages and known totals exclude those dates.",
                styles["placeholder"],
            ),
            Spacer(1, 6),
        ])
    if int(_number(totals.get("cross_category_duplicate_count")) or 0) > 0:
        result.extend([
            _paragraph(
                "Same-day cross-category duplicate names were reconciled once; direct/area assignment takes precedence for category totals.",
                styles["placeholder"],
            ),
            Spacer(1, 6),
        ])
    if daily:
        table_rows: list[list[Any]] = [[
            "Date", "Direct HC", "Indirect HC", "Total HC", "Regular MH", "OT MH", "Total MH",
        ]]
        for row in daily:
            supplied = row.get("supplied") is not False
            if not supplied:
                table_rows.append([
                    row.get("date", ""),
                    "Not supplied",
                    "Not supplied",
                    "Not supplied",
                    "Not supplied",
                    row.get("overtime_man_hours", "Not supplied"),
                    "Not supplied",
                ])
                continue
            regular = row.get("regular_man_hours")
            if regular is None:
                # Prefer the reconciled unique-person daily total. Older drafts
                # may have direct/indirect category overlap, where adding both
                # subtotals would double-count one employee.
                regular = row.get("total_man_hours")
            if regular is None:
                direct_value = _number(row.get("direct_man_hours")) or 0
                indirect_value = _number(row.get("indirect_man_hours")) or 0
                regular = direct_value + indirect_value
            table_rows.append([
                row.get("date", ""), row.get("direct_headcount", 0), row.get("indirect_headcount", 0),
                row.get("total_headcount", 0), regular, row.get("overtime_man_hours", "Not supplied"),
                row.get("total_man_hours", regular),
            ])
        table = LongTable(
            [[_paragraph(cell, header if row_index == 0 else center) for cell in row]
             for row_index, row in enumerate(table_rows)],
            colWidths=[74, 61, 66, 61, 70, 65, 72], repeatRows=1, splitByRow=1,
        )
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.45, BLACK),
            ("BACKGROUND", (0, 0), (-1, 0), CYAN),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        result.extend([_heading("Daily Headcount and Man-hours", styles["h2"], 1), table, Spacer(1, 10)])
    if roles:
        role_rows: list[list[Any]] = [["Role / Position", "Person-days", "Man-hours"]]
        for row in roles:
            role_rows.append([
                row.get("role", "Unspecified"),
                person_days_text(row.get("person_days", row.get("present_person_days", ""))),
                row.get("man_hours", row.get("physical_manhours", "")),
            ])
        table = LongTable(
            [[_paragraph(cell, header if row_index == 0 else (body if column_index == 0 else center))
              for column_index, cell in enumerate(row)] for row_index, row in enumerate(role_rows)],
            colWidths=[270, 100, 100], repeatRows=1,
        )
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.45, BLACK),
            ("BACKGROUND", (0, 0), (-1, 0), CYAN),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        result.extend([_heading("Role Summary", styles["h2"], 1), table])
    if totals.get("source"):
        result.extend([Spacer(1, 8), _paragraph(
            "Source: reviewed attendance workbook. Regular physical man-hours use 10 hours per present day; "
            "overtime is elapsed clock time only when explicitly applied.", styles["placeholder"],
        )])
    elif value.get("hours_method"):
        result.extend([Spacer(1, 8), _paragraph(
            "Source: Daily Reports. Calculation policy: " + _plain(value.get("hours_method")) + ".",
            styles["placeholder"],
        )])
    return result


def _build_story(
    report: Mapping[str, Any],
    styles: Mapping[str, ParagraphStyle],
    *,
    photo_base_dir: str | os.PathLike[str] | None = None,
) -> list[Flowable]:
    report_type = _report_type(report)
    status = _normalise_status(_value(report, "status", "report_mode", default="draft"), report_type=report_type)
    progress = _normalise_progress(report.get("progress", report.get("overall_progress", [])))
    explicit_curve = _normalise_s_curve(report.get("s_curve"), []) if isinstance(report.get("s_curve"), Mapping) else None
    if "include_s_curve" in report:
        include_s_curve = _coerce_bool(report.get("include_s_curve"), False)
    elif explicit_curve is not None:
        include_s_curve = True
    else:
        # Progress rows can produce an illustrative S-Curve in Preview/Draft.
        # A Final report without an approved explicit time series omits the
        # appendix instead of becoming impossible to issue.
        include_s_curve = bool(progress) and status != "FINAL"
    curve = _normalise_s_curve(report.get("s_curve"), progress) if include_s_curve else None
    if status == "FINAL" and curve is not None and curve[3]:
        raise ValueError(
            "Final reports cannot render an illustrative/unapproved S-Curve. "
            "Supply an approved time series or disable the S-Curve."
        )
    appendices = _normalise_appendices(report.get("appendices"), has_s_curve=curve is not None)
    manpower = report.get("manpower") if isinstance(report.get("manpower"), Mapping) else {}
    manpower_totals = manpower.get("totals") if isinstance(manpower.get("totals"), Mapping) else {}
    if manpower.get("daily") or manpower_totals:
        for item in appendices:
            if _appendix_source_number(item) == "6.5":
                item["status"] = "Attached"
                item["content"] = "__reviewed_manpower__"
                break
    photos = [item for item in _as_list(report.get("photo_documentation")) if isinstance(item, Mapping)]
    if photos:
        for item in appendices:
            if _appendix_source_number(item) == "6.6":
                item["status"] = "Attached"
                item["content"] = "__reviewed_photos__"
                break

    # Keep all appendix definitions in ``appendices`` for future data, but only
    # render appendices that are actually available for this report.  Hidden
    # entries are therefore also omitted automatically from the TOC because no
    # heading flowable is emitted for them.
    visible_appendices = _renumber_visible_appendices(
        [item for item in appendices if _appendix_is_visible(item)]
    )

    story: list[Flowable] = [
        Spacer(1, 1),
        NextPageTemplate("body"),
        PageBreak(),
        Paragraph("Table of Contents", styles["toc_title"]),
    ]
    toc = TableOfContents()
    toc.dotsMinLevel = 0
    toc.levelStyles = [
        ParagraphStyle(
            "MonthlyTOCLevel0",
            fontName="Helvetica",
            fontSize=10.5,
            leading=11.5,
            leftIndent=10,
            firstLineIndent=-10,
            spaceBefore=1,
            spaceAfter=0,
            textColor=BLACK,
        ),
        ParagraphStyle(
            "MonthlyTOCLevel1",
            fontName="Helvetica",
            fontSize=9.5,
            leading=10.5,
            leftIndent=26,
            firstLineIndent=-10,
            spaceBefore=0,
            spaceAfter=0,
            textColor=BLACK,
        ),
    ]
    story.extend([toc, PageBreak()])

    story.extend([
        _heading("1. Executive Summary", styles["h1"], 0),
        _paragraph(_executive_summary(report, progress), styles["body"]),
        Spacer(1, 6),
    ])
    story.extend(_coverage_flowables(report, styles))
    story.extend([
        _progress_table(
            progress, styles, report_type=report_type,
            period_label=_plain(report.get("progress_period_label")),
        ),
        Spacer(1, 18),
        _heading("2. Safety Status", styles["h1"], 0),
        Paragraph("<b>Status summary:</b>", styles["body"]),
        _safety_table(report.get("safety"), styles),
        PageBreak(),
    ])

    story.append(_heading("3. Engineering", styles["h1"], 0))
    story.append(_heading("3.1 Status Engineering", styles["h2"], 1))
    story.extend(_content_flowables(
        _client_facing_missing_summary(report.get("engineering"), section="engineering"),
        styles,
        empty_message="No engineering status data was supplied in the available Daily Reports.",
        bullets=True,
    ))
    story.append(PageBreak())

    procurement = _client_facing_missing_summary(
        report.get("procurement"),
        section="procurement",
    )
    summary, po_rows, embedded_equipment, embedded_shipments = _po_rows(procurement)
    if _plain(summary):
        procurement_intro = [_paragraph(summary, styles["body"]), Spacer(1, 3)]
    else:
        procurement_intro = []
    equipment = report.get("equipment_delivery", embedded_equipment)
    shipments = report.get("shipments", embedded_shipments)

    story.append(_heading("4. Procurement", styles["h1"], 0))
    story.extend(procurement_intro)
    story.append(_heading("4.1 Procurement Status", styles["h2"], 1))
    story.extend([_procurement_table(po_rows, styles), Spacer(1, 10)])
    story.append(_heading("4.2 Equipment Delivery Status", styles["h2"], 1))
    equipment_rows = []
    equipment_summary = equipment
    if isinstance(equipment, Mapping):
        equipment_summary = equipment.get("summary")
        equipment_rows = [
            row for row in _as_list(_value(equipment, "rows", "items", default=[]))
            if isinstance(row, Mapping)
        ]
    elif isinstance(equipment, Sequence) and not isinstance(equipment, (str, bytes, bytearray)):
        equipment_rows = [row for row in equipment if isinstance(row, Mapping)]
        if equipment_rows:
            equipment_summary = None
    if equipment_rows:
        story.extend([_equipment_table(equipment_rows, styles), Spacer(1, 10)])
    else:
        story.extend(_content_flowables(
            equipment_summary, styles,
            empty_message="No equipment delivery information supplied.", bullets=True,
        ))
    story.append(_heading("4.3 Shipment Status", styles["h2"], 1))
    shipment_rows = [row for row in _as_list(shipments) if isinstance(row, Mapping)]
    if isinstance(shipments, Mapping):
        shipment_rows = [
            row for row in _as_list(_value(shipments, "rows", "items", default=[]))
            if isinstance(row, Mapping)
        ]
    story.append(_shipment_table(shipment_rows, styles))
    story.append(PageBreak())

    site = report.get("site") if isinstance(report.get("site"), Mapping) else {}
    if report_type == "weekly":
        current_activity_keys = (
            "this_period_activities",
            "current_period_activities",
            "this_period",
            "current_period",
            "this_week_activities",
            "this_week",
            "this_month_activities",
            "this_month",
            "activities",
        )
        next_activity_keys = (
            "next_period_activities",
            "planned_next_period_activities",
            "next_week_activities",
            "planned_next_week",
        )
        current_heading = "5.2 This Week Activities"
        next_heading = "5.3 Planned Activities Next Week"
        current_empty = "No current-week activities supplied."
        next_empty = "No next-week activities supplied."
    else:
        current_activity_keys = (
            "this_month_activities",
            "this_month",
            "this_period_activities",
            "current_period_activities",
            "this_period",
            "current_period",
            "activities",
        )
        next_activity_keys = (
            "next_month_activities",
            "next_period_activities",
            "planned_next_period_activities",
            "planned_next_month",
        )
        current_heading = "5.2 This Month Activities"
        next_heading = "5.3 Planned Activities Next Month"
        current_empty = "No current-month activities supplied."
        next_empty = "No next-month activities supplied."

    current_activities = _value(
        site,
        *current_activity_keys,
        default=_value(report, *current_activity_keys, default=[]),
    )
    next_activities = _value(
        site,
        *next_activity_keys,
        default=_value(report, *next_activity_keys, default=[]),
    )
    story.append(_heading("5. Site Services / Construction", styles["h1"], 0))
    site_summary = _plain(site.get("summary"))
    if site_summary:
        story.extend([_paragraph(site_summary, styles["body"]), Spacer(1, 3)])
    story.append(_heading("5.1 Project Schedule Status", styles["h2"], 1))
    story.extend(_content_flowables(
        _value(site, "schedule_status", "project_schedule_status"), styles,
        empty_message="Overall schedule/progress data was not supplied for this reporting period.",
    ))
    story.append(_heading(current_heading, styles["h2"], 1))
    story.extend(_activity_flowables(
        current_activities,
        styles, empty_message=current_empty,
    ))
    story.append(_heading(next_heading, styles["h2"], 1))
    story.extend(_activity_flowables(
        next_activities,
        styles, empty_message=next_empty,
    ))
    story.append(_heading(
        "5.4 Area of Concern and Suggested Corrective Action", styles["h2"], 1
    ))
    concern_rows = _as_list(_value(site, "concerns", "constraints", default=report.get("constraints")))
    concerns_table = _concerns_table(concern_rows, styles)
    if concerns_table is None:
        story.append(_paragraph(_constraint_reporting_message(report, site), styles["placeholder"]))
    else:
        story.append(concerns_table)
    findings_table = _key_findings_table(_as_list(site.get("key_findings")), styles)
    if findings_table is not None:
        story.extend([Spacer(1, 5), _display_heading("Key Remarks / Findings", styles["h2"]), findings_table])
    story.append(PageBreak())

    if visible_appendices:
        story.append(_heading("6. Appendices", styles["h1"], 0))
        for item in visible_appendices:
            story.append(_heading(_appendix_label(item), styles["appendix_item"], 1))

    if curve is not None:
        labels, planned, actual, illustrative = curve
        curve_item = next(
            (item for item in visible_appendices if _appendix_source_number(item) == "6.2"),
            None,
        )
        curve_number = _plain(curve_item.get("number")) if curve_item else "6.1"
        curve_title = _plain(curve_item.get("title"), "Progress S-Curve") if curve_item else "Progress S-Curve"
        story.extend([
            PageBreak(),
            Paragraph(
                escape(f"Appendix {curve_number} - {curve_title}", quote=False),
                styles["h1"],
            ),
            Spacer(1, 10),
            _SCurveFlowable(labels, planned, actual, illustrative=illustrative),
        ])

    for item in visible_appendices:
        content = item.get("content")
        if content in (None, "", [], {}):
            continue
        story.extend([
            PageBreak(),
            Paragraph(
                escape(f"Appendix {item['number']} - {item['title']}", quote=False),
                styles["h1"],
            ),
        ])
        if content == "__reviewed_photos__":
            story.extend(_photo_grid_flowables(
                photos,
                styles,
                photo_base_dir=photo_base_dir,
            ))
        elif content == "__reviewed_manpower__":
            story.extend(_manpower_appendix_flowables(manpower, styles))
        else:
            story.extend(_content_flowables(
                content, styles, empty_message="No appendix content supplied.", bullets=True,
            ))
    return story


def render_monthly_report(
    report: Mapping[str, Any],
    *,
    logo_path: str | os.PathLike[str] | None = None,
    photo_base_dir: str | os.PathLike[str] | None = None,
) -> io.BytesIO:
    """Render a Weekly or Monthly Progress Report and return a rewound buffer.

    ``report`` is a reviewed runtime mapping.  The main supported keys are
    ``status``, project/document metadata, ``revision_rows``, ``progress``,
    ``safety``, ``engineering``, ``procurement``, ``equipment_delivery``,
    ``shipments``, ``site``, ``appendices``, and optional ``s_curve``.

    ``report_type`` accepts ``weekly`` or ``monthly`` and defaults to monthly
    for backward compatibility. ``status`` accepts ``draft``, ``wtd``, ``mtd``,
    or ``final``. Missing/unknown statuses deliberately render as DRAFT. The
    function never writes to disk.
    """
    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")

    report_type = _report_type(report)
    status = _normalise_status(
        _value(report, "status", "report_mode", default="draft"),
        report_type=report_type,
    )
    resolved_logo = logo_path if logo_path is not None else report.get("logo_path")
    styles = _styles()
    buffer = io.BytesIO()
    document = _MonthlyDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=BODY_LEFT,
        rightMargin=BODY_RIGHT,
        topMargin=BODY_TOP_MARGIN,
        bottomMargin=BODY_BOTTOM,
        allowSplitting=1,
        title=(
            f"{_progress_report_title(report)} - "
            f"{_plain(_value(report, 'project_name', 'project_title'), 'Project')}"
        ),
        author=_plain(_value(report, "company_name", "vendor_name")),
    )
    cover_frame = Frame(
        24, 24, PAGE_WIDTH - 48, PAGE_HEIGHT - 48,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="monthly-cover-frame",
    )
    body_frame = Frame(
        BODY_LEFT, BODY_BOTTOM, BODY_WIDTH, BODY_HEIGHT,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        id="monthly-body-frame",
    )
    document.addPageTemplates([
        PageTemplate(
            id="cover",
            frames=[cover_frame],
            onPage=lambda canv, doc: _draw_cover(
                canv, doc, report, status, resolved_logo
            ),
        ),
        PageTemplate(
            id="body",
            frames=[body_frame],
            onPage=lambda canv, doc: _draw_body_page(
                canv, doc, report, status, resolved_logo
            ),
        ),
    ])

    def canvas_maker(*args, **kwargs):
        return _NumberedCanvas(*args, status=status, **kwargs)

    document.multiBuild(
        _build_story(report, styles, photo_base_dir=photo_base_dir),
        canvasmaker=canvas_maker,
    )
    buffer.seek(0)
    return buffer
