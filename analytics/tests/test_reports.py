# analytics/tests/test_reports.py
"""
CSV report exports.

Two questions these cover: does the permission wall hold on every report
(including the non-financial ones — a worker has no business pulling an
aggregate export), and does each file carry the disclosures the on-screen
charts show, so it does not mislead once it is opened in Excel months
later with no tooltip in sight.
"""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from production.models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    Harvest,
    House,
    InventoryItem,
    InventoryUsageLog,
    TaskCompletion,
    TaskTemplate,
)

pytestmark = pytest.mark.django_db

REPORT_NAMES = [
    "report-daily-records",
    "report-mortality-summary",
    "report-fcr",
    "report-feed-margin",
    "report-inventory-usage",
    "report-routine-completion",
]

HEADER_FIRST_COL = {
    "report-daily-records": "record_date",
    "report-mortality-summary": "batch_code",
    "report-fcr": "batch_code",
    "report-feed-margin": "batch_code",
    "report-inventory-usage": "usage_date",
    "report-routine-completion": "date",
}

# fcr and feed-margin always append a disclosure block after the data
# section, so "no data" for them means: header, then the blank separator.
HAS_DISCLOSURE_BLOCK = {"report-fcr", "report-feed-margin"}


def _url(name, farm):
    return reverse(f"analytics:{name}", kwargs={"farm_pk": farm.pk})


def _text(response):
    return b"".join(response.streaming_content).decode("utf-8")


def _rows(response):
    return list(csv.reader(io.StringIO(_text(response))))


@pytest.fixture
def populated_farm(staffed_farm, owner, worker):
    """
    One harvested batch (feeds FCR and feed margin), one still-active batch
    (so the FCR exclusion disclosure has something to report), plus feed,
    inventory, and one routine completion.
    """
    farm = staffed_farm
    house1 = House.objects.create(farm=farm, name="House 1", capacity=10000)
    house2 = House.objects.create(farm=farm, name="House 2", capacity=10000)

    harvested = Batch.objects.create(
        house=house1,
        batch_code="B-2026-01",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )
    for day in range(2, 7):  # 5 records, Jan 2–6
        DailyRecord.objects.create(
            batch=harvested,
            record_date=date(2026, 1, day),
            mortality_disease=2,
            mortality_heat=1,
            feed_kg=Decimal("120.00"),
            recorded_by=worker,
        )
    Harvest.objects.create(
        batch=harvested,
        harvest_date=date(2026, 2, 20),
        birds_harvested=985,
        total_weight_kg=Decimal("1800.00"),
        revenue=Decimal("250000.00"),
        recorded_by=owner,
    )
    harvested.status = Batch.Status.HARVESTED
    harvested.save(update_fields=["status"])

    active = Batch.objects.create(
        house=house2,
        batch_code="B-2026-02",
        initial_bird_count=800,
        start_date=date(2026, 3, 1),
        created_by=owner,
    )
    DailyRecord.objects.create(
        batch=active,
        record_date=date(2026, 3, 2),
        mortality_unknown=3,
        feed_kg=Decimal("60.00"),
        recorded_by=worker,
    )

    FeedDelivery.objects.create(
        farm=farm,
        delivery_date=date(2026, 1, 1),
        feed_type=FeedDelivery.FeedType.STARTER,
        quantity_kg=Decimal("2000"),
        unit_cost=Decimal("35.00"),
        recorded_by=owner,
    )

    item = InventoryItem.objects.create(
        farm=farm, name="Disinfectant", unit="litre"
    )
    InventoryUsageLog.objects.create(
        item=item,
        quantity_used=Decimal("5.00"),
        usage_date=date(2026, 1, 10),
        recorded_by=worker,
    )
    InventoryUsageLog.objects.create(
        item=item,
        quantity_used=Decimal("3.00"),
        usage_date=date(2026, 2, 10),
        recorded_by=worker,
    )

    template = TaskTemplate.objects.create(
        farm=farm, name="Morning feed", created_by=owner
    )
    TaskCompletion.objects.create(
        template=template,
        house=house1,
        completion_date=date(2026, 1, 10),
        recorded_by=worker,
    )
    return farm


# ─────────────────────────────────────────────────────────────
# Access
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", REPORT_NAMES)
class TestReportAccess:
    def test_owner_can_download(self, auth, owner, populated_farm, name):
        response = auth(owner).get(_url(name, populated_farm))
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")
        disposition = response["Content-Disposition"]
        assert disposition.startswith(
            'attachment; filename="santos-layer-farm-'
        )
        assert disposition.endswith('.csv"')

    def test_manager_can_download(self, auth, manager, populated_farm, name):
        response = auth(manager).get(_url(name, populated_farm))
        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/csv")

    def test_worker_is_forbidden(self, auth, worker, populated_farm, name):
        """A report is an aggregate export — not a worker's function, even
        for the non-financial ones."""
        assert auth(worker).get(_url(name, populated_farm)).status_code == 403

    def test_other_farms_member_is_forbidden(
        self, auth, rival_owner, populated_farm, name
    ):
        assert (
            auth(rival_owner).get(_url(name, populated_farm)).status_code == 403
        )

    def test_anonymous_is_rejected(self, api, populated_farm, name):
        assert api.get(_url(name, populated_farm)).status_code in (401, 403)


# ─────────────────────────────────────────────────────────────
# Shape and content
# ─────────────────────────────────────────────────────────────


class TestReportContent:
    def test_daily_records_columns_and_body(self, auth, owner, populated_farm):
        rows = _rows(auth(owner).get(_url("report-daily-records", populated_farm)))
        header = rows[0]
        assert header[:3] == ["record_date", "batch_code", "house"]
        assert "mortality_total" in header
        assert "feed_kg" in header
        assert "recorded_by" in header
        assert "was_corrected" in header

        body = rows[1:]
        assert len(body) == 6  # 5 for the harvested batch + 1 for the active
        first = body[0]
        assert first[1] == "B-2026-01"
        assert first[header.index("recorded_by")] == "Ana Reyes"
        assert first[header.index("mortality_total")] == "3"  # 2 + 1
        assert first[header.index("was_corrected")] == "no"

    def test_daily_records_date_filter_narrows_rows(
        self, auth, owner, populated_farm
    ):
        response = auth(owner).get(
            _url("report-daily-records", populated_farm), {"from": "2026-03-01"}
        )
        body = _rows(response)[1:]
        assert len(body) == 1
        assert body[0][1] == "B-2026-02"

    def test_daily_records_batch_filter(self, auth, owner, populated_farm):
        harvested = Batch.objects.get(batch_code="B-2026-01")
        response = auth(owner).get(
            _url("report-daily-records", populated_farm),
            {"batch": str(harvested.id)},
        )
        body = _rows(response)[1:]
        assert len(body) == 5
        assert {row[1] for row in body} == {"B-2026-01"}

    def test_mortality_summary_rate_and_causes(self, auth, owner, populated_farm):
        rows = _rows(
            auth(owner).get(_url("report-mortality-summary", populated_farm))
        )
        header = rows[0]
        body = {row[0]: row for row in rows[1:]}

        harvested = body["B-2026-01"]
        assert harvested[header.index("birds_placed")] == "1000"
        assert harvested[header.index("birds_lost")] == "15"  # 5 days x 3
        assert harvested[header.index("mortality_rate_pct")] == "1.50"
        assert harvested[header.index("lost_disease")] == "10"
        assert harvested[header.index("lost_heat")] == "5"

    def test_fcr_csv_contains_exclusion_note(self, auth, owner, populated_farm):
        text = _text(auth(owner).get(_url("report-fcr", populated_farm)))
        assert "1 active batch(es)" in text
        assert "excluded from this report" in text.lower()
        assert "FCR requires a harvest weight." in text

    def test_fcr_csv_data_row_for_harvested_batch(
        self, auth, owner, populated_farm
    ):
        rows = _rows(auth(owner).get(_url("report-fcr", populated_farm)))
        header = rows[0]
        data = [r for r in rows[1:] if r and r[0] == "B-2026-01"]
        assert len(data) == 1
        row = data[0]
        # 600 kg feed / 1800 kg weight = 0.333
        assert row[header.index("fcr")] == "0.333"
        assert row[header.index("birds_harvested")] == "985"

    def test_feed_margin_methodology_rows_present(
        self, auth, owner, populated_farm
    ):
        text = _text(auth(owner).get(_url("report-feed-margin", populated_farm)))
        assert "METHODOLOGY" in text
        assert "Farm-wide average cost per kg" in text
        assert "day-old chicks" in text
        assert "Feed margin, not net profit" in text

    def test_feed_margin_never_says_profit_except_the_disclaimer(
        self, auth, owner, populated_farm
    ):
        text = _text(
            auth(owner).get(_url("report-feed-margin", populated_farm))
        ).lower()
        assert "profit" in text  # it is in the disclaimer
        assert text.replace("not net profit", "").count("profit") == 0

    def test_feed_margin_values_are_plain_decimal_strings(
        self, auth, owner, populated_farm
    ):
        rows = _rows(auth(owner).get(_url("report-feed-margin", populated_farm)))
        header = rows[0]
        row = next(r for r in rows[1:] if r and r[0] == "B-2026-01")
        # 2000 kg delivered at 35.00 → 35.00/kg; 600 kg consumed → 21000.00
        assert row[header.index("revenue")] == "250000.00"
        assert row[header.index("allocated_feed_cost")] == "21000.00"
        assert row[header.index("feed_margin")] == "229000.00"

    def test_inventory_usage_columns_and_filter(
        self, auth, owner, populated_farm
    ):
        response = auth(owner).get(
            _url("report-inventory-usage", populated_farm)
        )
        rows = _rows(response)
        assert rows[0] == [
            "usage_date",
            "item",
            "quantity_used",
            "unit",
            "recorded_by",
        ]
        assert len(rows[1:]) == 2

        filtered = _rows(
            auth(owner).get(
                _url("report-inventory-usage", populated_farm),
                {"from": "2026-02-01"},
            )
        )[1:]
        assert len(filtered) == 1
        assert filtered[0][0] == "2026-02-10"

    def test_routine_completion_is_a_completed_or_not_grid(
        self, auth, owner, populated_farm
    ):
        response = auth(owner).get(
            _url("report-routine-completion", populated_farm),
            {"from": "2026-01-10", "to": "2026-01-11"},
        )
        rows = _rows(response)
        assert rows[0] == ["date", "task", "house", "completed", "completed_by"]

        body = rows[1:]
        assert len(body) == 4  # 2 days x 1 task x 2 houses

        done = [r for r in body if r[3] == "yes"]
        assert len(done) == 1
        assert done[0][0] == "2026-01-10"
        assert done[0][2] == "House 1"
        assert done[0][4] == "Ana Reyes"

        assert all(r[3] == "no" for r in body if r[0] == "2026-01-11")

    def test_routine_completion_house_filter(self, auth, owner, populated_farm):
        house1 = House.objects.get(farm=populated_farm, name="House 1")
        rows = _rows(
            auth(owner).get(
                _url("report-routine-completion", populated_farm),
                {"from": "2026-01-10", "to": "2026-01-10", "house": str(house1.id)},
            )
        )
        body = rows[1:]
        assert len(body) == 1
        assert body[0][2] == "House 1"
        assert body[0][3] == "yes"


# ─────────────────────────────────────────────────────────────
# Row shape — every data row must be exactly as wide as the header
# ─────────────────────────────────────────────────────────────
#
# A shifted column is invisible to a spot check but corrupts every value
# after it. This runs against a farm with data in every report, so each
# generator actually emits body rows to measure. Disclosure rows (NOTE /
# METHODOLOGY / SUMMARY) sit after a blank separator and are deliberately
# narrower — the check stops at that blank line.


class TestRowWidthMatchesHeader:
    # Keep the routine-completion grid small; the rest need no params.
    QUERY = {
        "report-daily-records": {},
        "report-mortality-summary": {},
        "report-fcr": {},
        "report-feed-margin": {},
        "report-inventory-usage": {},
        "report-routine-completion": {"from": "2026-01-01", "to": "2026-01-05"},
    }

    @pytest.mark.parametrize("name", REPORT_NAMES)
    def test_data_rows_have_header_width(
        self, auth, owner, populated_farm, name
    ):
        rows = _rows(
            auth(owner).get(_url(name, populated_farm), self.QUERY[name])
        )
        header_width = len(rows[0])
        assert header_width > 1, f"{name}: header looks empty ({rows[0]!r})"

        data_rows = 0
        for row in rows[1:]:
            if row == []:
                break  # blank separator; anything after is a disclosure note
            assert len(row) == header_width, (
                f"{name}: data row {row!r} has {len(row)} fields, "
                f"header has {header_width}"
            )
            data_rows += 1

        assert data_rows > 0, (
            f"{name}: fixture produced no data rows, so this test proved "
            f"nothing — widen the fixture"
        )


# ─────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────


class TestReportValidation:
    DATE_REPORTS = [
        "report-daily-records",
        "report-mortality-summary",
        "report-inventory-usage",
        "report-routine-completion",
    ]

    @pytest.mark.parametrize("name", DATE_REPORTS)
    def test_from_after_to_is_400(self, auth, owner, populated_farm, name):
        response = auth(owner).get(
            _url(name, populated_farm),
            {"from": "2026-05-01", "to": "2026-01-01"},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("name", DATE_REPORTS)
    def test_malformed_date_is_400(self, auth, owner, populated_farm, name):
        response = auth(owner).get(
            _url(name, populated_farm), {"from": "last-tuesday"}
        )
        assert response.status_code == 400

    def test_equal_from_and_to_is_allowed(self, auth, owner, populated_farm):
        response = auth(owner).get(
            _url("report-daily-records", populated_farm),
            {"from": "2026-01-03", "to": "2026-01-03"},
        )
        assert response.status_code == 200
        assert len(_rows(response)[1:]) == 1


# ─────────────────────────────────────────────────────────────
# Empty farm — a valid empty CSV, not an error
# ─────────────────────────────────────────────────────────────


class TestEmptyFarm:
    @pytest.mark.parametrize("name", REPORT_NAMES)
    def test_returns_header_and_no_data_rows(
        self, auth, owner, staffed_farm, name
    ):
        response = auth(owner).get(_url(name, staffed_farm))
        assert response.status_code == 200

        rows = _rows(response)
        assert rows[0][0] == HEADER_FIRST_COL[name]

        if name in HAS_DISCLOSURE_BLOCK:
            # No batch rows: the blank separator comes straight after the header.
            assert rows[1] == []
        else:
            assert len(rows) == 1
