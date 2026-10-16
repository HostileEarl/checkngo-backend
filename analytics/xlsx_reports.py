# analytics/xlsx_reports.py
"""
Builds the XLSX workbook for a report, from the exact same row generators
(analytics.reports) that back the CSV export. No query or arithmetic is
duplicated here — this module only lays out and formats values that
analytics.reports already computed.

Layout (per the 7 report routes):
    Row 1  Title: "CheckN Go — <Report name>"            bold, 14pt
    Row 2  Farm name
    Row 3  Period label (e.g. "October 2026 (Oct 1-31)")
    Row 4  "Generated <date> <time> Asia/Manila"
    Row 5  (blank)
    Row 6  Column headers                                 bold, grey fill
    Row 7+ Data rows (zebra-striped), then a Totals row
"""
import calendar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.utils.text import slugify
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import reports
from .report_specs import NUMBER_FORMATS, REPORT_SPECS, SUMMABLE_TYPES

_MANILA = ZoneInfo("Asia/Manila")

_HEADER_FILL = PatternFill(fill_type="solid", fgColor="F3F4F6")
_ZEBRA_FILL = PatternFill(fill_type="solid", fgColor="FAFAFA")
_THIN_BOTTOM = Border(bottom=Side(style="thin"))
_TOP_BORDER = Border(top=Side(style="thin"))
_TITLE_FONT = Font(bold=True, size=14)
_HEADER_FONT = Font(bold=True)
_WRAP_CENTER = Alignment(wrap_text=True, vertical="center")
_RIGHT = Alignment(horizontal="right")
_LEFT = Alignment(horizontal="left")

# Row generators from analytics.reports, keyed the same way as the URL
# slug / report_specs keys. Each callable takes (farm, date_from, date_to,
# extra_filter_value_or_None) — reports with fewer parameters simply
# ignore the trailing args via *_ in the lambda.
_ROW_GENERATORS = {
    "daily-records": lambda farm, date_from, date_to, extra: reports.daily_records_rows(
        farm, date_from, date_to, extra
    ),
    "mortality-summary": lambda farm, date_from, date_to, extra: reports.mortality_summary_rows(
        farm, date_from, date_to
    ),
    "fcr": lambda farm, date_from, date_to, extra: reports.fcr_rows(farm),
    "feed-margin": lambda farm, date_from, date_to, extra: reports.feed_margin_rows(farm),
    "inventory-usage": lambda farm, date_from, date_to, extra: reports.inventory_usage_rows(
        farm, date_from, date_to, extra
    ),
    "routine-completion": lambda farm, date_from, date_to, extra: reports.routine_completion_rows(
        farm, date_from, date_to, extra
    ),
    "sales-history": lambda farm, date_from, date_to, extra: reports.sales_history_rows(
        farm, date_from, date_to
    ),
}

# Reports that accept no date range at all (fcr, feed-margin) still go
# through the same _date_range()-shaped call signature above; date_from/
# date_to are simply ignored by their generator.

REPORTS_WITHOUT_DATE_RANGE = {"fcr", "feed-margin"}


def report_rows(report_key, farm, date_from=None, date_to=None, extra=None):
    """The one call site both the CSV view and the XLSX view could use to
    get a report's raw rows — kept here so the mapping from URL slug to
    generator function lives in exactly one place."""
    return _ROW_GENERATORS[report_key](farm, date_from, date_to, extra)


def _convert(value, value_type):
    """Raw CSV-row cell value -> a typed value fit for a spreadsheet cell."""
    if value_type == "text":
        return "" if value is None else str(value)

    if value in (None, ""):
        return None

    if value_type == "date":
        if isinstance(value, (date, datetime)):
            return value
        return date.fromisoformat(str(value))

    if value_type == "percent":
        try:
            return float(Decimal(str(value)) / Decimal("100"))
        except InvalidOperation:
            return None

    if value_type == "count":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # money, kg, fcr — all plain decimals
    try:
        return float(Decimal(str(value)))
    except InvalidOperation:
        return None


def _is_full_calendar_month(date_from, date_to):
    if date_from is None or date_to is None:
        return False
    if date_from.day != 1:
        return False
    last_day = calendar.monthrange(date_to.year, date_to.month)[1]
    return (
        date_from.year == date_to.year
        and date_from.month == date_to.month
        and date_to.day == last_day
    )


def period_label(date_from, date_to):
    """Human-readable period for row 3, e.g. 'October 2026 (Oct 1-31)'."""
    if _is_full_calendar_month(date_from, date_to):
        return (
            f"{date_from.strftime('%B %Y')} "
            f"({date_from.strftime('%b')} {date_from.day}-{date_to.day})"
        )
    if date_from and date_to:
        return (
            f"{date_from.strftime('%b')} {date_from.day}, {date_from.year} - "
            f"{date_to.strftime('%b')} {date_to.day}, {date_to.year}"
        )
    if date_from:
        return f"From {date_from.strftime('%b')} {date_from.day}, {date_from.year}"
    if date_to:
        return f"Through {date_to.strftime('%b')} {date_to.day}, {date_to.year}"
    return "All records"


def period_tag(date_from, date_to):
    """Filename-safe period tag, e.g. '2026-10' for a full month."""
    if _is_full_calendar_month(date_from, date_to):
        return date_from.strftime("%Y-%m")
    if date_from and date_to:
        return f"{date_from.isoformat()}_to_{date_to.isoformat()}"
    if date_from:
        return f"from-{date_from.isoformat()}"
    if date_to:
        return f"through-{date_to.isoformat()}"
    return "all"


def xlsx_filename(report_key, date_from=None, date_to=None):
    """checkngo_<key>_<period>_<start>.xlsx"""
    start = date_from.isoformat() if date_from else "all"
    return f"checkngo_{report_key}_{period_tag(date_from, date_to)}_{start}.xlsx"


def _generated_timestamp():
    now = datetime.now(tz=_MANILA)
    return f"Generated {now.strftime('%Y-%m-%d %H:%M')} Asia/Manila"


def _set_text(cell, value):
    """Write a free-text value as a literal string — never as a formula,
    even if the value starts with '=', '+', '-' or '@'."""
    cell.value = value
    cell.data_type = "s"


def _column_width(values):
    width = max((len(v) for v in values), default=10)
    return max(10, min(40, width + 2))


def build_workbook(report_key, farm, date_from=None, date_to=None, extra=None):
    """
    Build the full openpyxl Workbook for one report. Returns the Workbook;
    the caller decides how to serialize/stream it.
    """
    spec = REPORT_SPECS[report_key]
    columns = spec.columns
    n_cols = len(columns)

    header, raw_data_rows, trailing = reports.split_report_rows(
        report_rows(report_key, farm, date_from, date_to, extra)
    )
    assert len(header) == n_cols, (
        f"{report_key}: CSV header has {len(header)} columns, "
        f"report_specs has {n_cols} — these must stay in lock-step"
    )

    wb = Workbook()
    ws = wb.active
    ws.title = spec.title[:31]

    # Row 1-4: title block
    ws.cell(row=1, column=1, value=f"CheckN Go — {spec.title}")
    ws.cell(row=1, column=1).font = _TITLE_FONT
    # farm.name is operator-supplied free text — defused the same way as
    # any other text cell, even though it sits outside the data table.
    _set_text(ws.cell(row=2, column=1), farm.name)
    ws.cell(row=3, column=1, value=period_label(date_from, date_to))
    ws.cell(row=4, column=1, value=_generated_timestamp())
    for r in (1, 2, 3, 4):
        last = n_cols if n_cols > 1 else 2
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=last)
    # Row 5 is left blank deliberately.

    # Row 6: headers
    header_row = 6
    for col_idx, column in enumerate(columns, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=column.label)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.border = _THIN_BOTTOM
        cell.alignment = _WRAP_CENTER

    col_text_widths = [[column.label] for column in columns]

    first_data_row = header_row + 1
    current_row = first_data_row

    if not raw_data_rows:
        ws.cell(row=current_row, column=1, value="No records for this period")
        ws.cell(row=current_row, column=1).font = Font(italic=True)
        last_data_row = None
        current_row += 1
    else:
        last_data_row = first_data_row + len(raw_data_rows) - 1
        for offset, row in enumerate(raw_data_rows):
            r = first_data_row + offset
            zebra = offset % 2 == 1
            for col_idx, (column, raw_value) in enumerate(zip(columns, row), start=1):
                cell = ws.cell(row=r, column=col_idx)
                value = _convert(raw_value, column.type)
                if column.type == "text":
                    _set_text(cell, value)
                    cell.alignment = _LEFT
                else:
                    cell.value = value
                    cell.alignment = _RIGHT
                    if column.type in NUMBER_FORMATS:
                        cell.number_format = NUMBER_FORMATS[column.type]
                if zebra:
                    cell.fill = _ZEBRA_FILL
                col_text_widths[col_idx - 1].append(str(raw_value) if raw_value is not None else "")
        current_row = last_data_row + 1

        # Totals row
        totals_row = current_row
        ws.cell(row=totals_row, column=1, value="Total")
        ws.cell(row=totals_row, column=1).font = Font(bold=True)
        ws.cell(row=totals_row, column=1).border = _TOP_BORDER
        for col_idx, column in enumerate(columns, start=1):
            cell = ws.cell(row=totals_row, column=col_idx)
            cell.border = _TOP_BORDER
            if col_idx == 1:
                continue
            if column.type in SUMMABLE_TYPES:
                col_letter = get_column_letter(col_idx)
                cell.value = f"=SUM({col_letter}{first_data_row}:{col_letter}{last_data_row})"
                cell.number_format = NUMBER_FORMATS.get(column.type, "General")
                cell.font = Font(bold=True)
                cell.alignment = _RIGHT
        current_row = totals_row + 1

    # Trailing disclosure rows (NOTE / METHODOLOGY / SUMMARY), if any —
    # preserved so nothing the CSV says is silently dropped from the
    # spreadsheet, just not treated as part of the summable table.
    if trailing:
        current_row += 1  # blank separator
        for note_row in trailing:
            text = " — ".join(str(c) for c in note_row if c not in (None, ""))
            cell = ws.cell(row=current_row, column=1)
            _set_text(cell, text)
            cell.font = Font(italic=True, size=9)
            current_row += 1

    # Column widths
    for col_idx in range(1, n_cols + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = _column_width(
            col_text_widths[col_idx - 1]
        )

    # Freeze panes / autofilter / print setup
    ws.freeze_panes = "A7"
    if last_data_row is not None:
        ws.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(n_cols)}{last_data_row}"
        )

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{header_row}:{header_row}"

    return wb
