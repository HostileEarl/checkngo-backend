# analytics/report_specs.py
"""
Column metadata for the 7 farm reports — the single place that knows what
each column in a report *means* (its display label and value type), shared
by the CSV export (whose byte output is untouched) and the new XLSX export
(which needs units in the header, number formats, and real date/percent
cell types).

This module adds metadata only. It does not duplicate the queries or the
arithmetic in analytics/reports.py or analytics/services.py — the row data
for both CSV and XLSX still comes from exactly one place: the row
generators in analytics/reports.py. A ColumnSpec here just says, for the
column at a given position, what to call it in a spreadsheet and how to
format/convert its value.

Column order here MUST match the header row each generator in reports.py
actually yields — analytics/tests/test_reports_xlsx.py asserts this for
every report so a drift is caught immediately, not discovered by a user
opening a shifted spreadsheet.
"""
from collections import namedtuple

# One of: "money", "kg", "count", "percent", "fcr", "date", "text".
ColumnSpec = namedtuple("ColumnSpec", ["label", "type"])

# Value types that are plain additive quantities — these get a real SUM()
# formula in the totals row. Ratio-like types (percent, fcr) do not.
SUMMABLE_TYPES = {"money", "kg", "count"}

NUMBER_FORMATS = {
    "money": "#,##0.00",
    "kg": "#,##0.00",
    "count": "#,##0",
    "percent": "0.00%",
    "fcr": "0.00",
    "date": "yyyy-mm-dd",
}


ReportSpec = namedtuple("ReportSpec", ["title", "columns"])


REPORT_SPECS = {
    "daily-records": ReportSpec(
        title="Daily Records",
        columns=[
            ColumnSpec("Date", "date"),
            ColumnSpec("Batch code", "text"),
            ColumnSpec("House", "text"),
            ColumnSpec("Mortality – disease", "count"),
            ColumnSpec("Mortality – heat", "count"),
            ColumnSpec("Mortality – culled", "count"),
            ColumnSpec("Mortality – unknown", "count"),
            ColumnSpec("Mortality – total", "count"),
            ColumnSpec("Feed used (kg)", "kg"),
            ColumnSpec("Recorded by", "text"),
            ColumnSpec("Corrected?", "text"),
        ],
    ),
    "mortality-summary": ReportSpec(
        title="Mortality Summary",
        columns=[
            ColumnSpec("Batch code", "text"),
            ColumnSpec("House", "text"),
            ColumnSpec("Status", "text"),
            ColumnSpec("Birds placed", "count"),
            ColumnSpec("Birds lost", "count"),
            ColumnSpec("Mortality (%)", "percent"),
            ColumnSpec("Lost – disease", "count"),
            ColumnSpec("Lost – heat", "count"),
            ColumnSpec("Lost – culled", "count"),
            ColumnSpec("Lost – unknown", "count"),
        ],
    ),
    "fcr": ReportSpec(
        title="Feed Conversion Ratio (FCR)",
        columns=[
            ColumnSpec("Batch code", "text"),
            ColumnSpec("House", "text"),
            ColumnSpec("Harvest date", "date"),
            ColumnSpec("Cycle (days)", "count"),
            ColumnSpec("Birds placed", "count"),
            ColumnSpec("Birds harvested", "count"),
            ColumnSpec("Survival (%)", "percent"),
            ColumnSpec("Total feed (kg)", "kg"),
            ColumnSpec("Total weight (kg)", "kg"),
            ColumnSpec("Avg bird weight (kg)", "kg"),
            ColumnSpec("FCR", "fcr"),
        ],
    ),
    "feed-margin": ReportSpec(
        title="Feed Margin",
        columns=[
            ColumnSpec("Batch code", "text"),
            ColumnSpec("House", "text"),
            ColumnSpec("Harvest date", "date"),
            ColumnSpec("Total weight (kg)", "kg"),
            ColumnSpec("Revenue (PHP)", "money"),
            ColumnSpec("Revenue per kg (PHP)", "money"),
            ColumnSpec("Allocated feed cost (PHP)", "money"),
            ColumnSpec("Feed margin (PHP)", "money"),
            ColumnSpec("Feed margin per kg (PHP)", "money"),
            ColumnSpec("Buyer", "text"),
        ],
    ),
    "inventory-usage": ReportSpec(
        title="Inventory Usage",
        columns=[
            ColumnSpec("Date", "date"),
            ColumnSpec("Item", "text"),
            ColumnSpec("Quantity used", "kg"),
            ColumnSpec("Unit", "text"),
            ColumnSpec("Recorded by", "text"),
        ],
    ),
    "routine-completion": ReportSpec(
        title="Routine Completion",
        columns=[
            ColumnSpec("Date", "date"),
            ColumnSpec("Task", "text"),
            ColumnSpec("House", "text"),
            ColumnSpec("Completed", "text"),
            ColumnSpec("Completed by", "text"),
        ],
    ),
    "sales-history": ReportSpec(
        title="Sales History",
        columns=[
            ColumnSpec("Date", "date"),
            ColumnSpec("Batch code", "text"),
            ColumnSpec("House", "text"),
            ColumnSpec("Sale type", "text"),
            ColumnSpec("Birds sold", "count"),
            ColumnSpec("Total weight (kg)", "kg"),
            ColumnSpec("Sale value (PHP)", "money"),
            ColumnSpec("Buyer", "text"),
        ],
    ),
}
