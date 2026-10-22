# analytics/tests/test_kpi_tc25.py
"""
The four KPI formulas on the TC-25 batch, end to end.

TC-25 as written in production/tests/test_sale_events.py
(TestOthersSaleType::test_tc25_fcr_and_survival_unaffected_by_an_others_sale):
1000 birds placed, 50 lost, 1500 kg of feed, 900 birds and 900 kg sold
live at ₱70.00/kg, plus a 10 kg parts (OTHERS) sale at ₱50.00/kg. That
test checks survival and FCR from the service function. This one enters
the same batch the way the app does (bulk sync, then closing) and reads
all four KPIs back from the endpoints and the CSV exports a panel would
see:

  mortality rate = lost / placed            = 50 / 1000       = 5.00%
  survival rate  = sold (whole birds) / placed = 900 / 1000   = 90.00%
  FCR            = feed kg / live kg sold   = 1500 / 900      = 1.667
  feed margin    = revenue − feed kg × average feed cost/kg
                 = (63,000 + 500) − 1500 × 30.00 = 18,500.00
  margin per kg  = 18,500 / 900 live kg                       = 20.56

TC-25 names no feed price; FEED_PRICE below is this test's own input. If
the manuscript's TC-25 table uses another price, change FEED_PRICE and the
two figures that follow from it.
"""
import csv
import io
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from farms.models import Farm
from farms.tests.two_farms import FAST_HASHERS, make_user
from production.models import Batch, House

pytestmark = pytest.mark.django_db

FEED_PRICE = Decimal("30.00")


@pytest.fixture(autouse=True)
def _fast_hasher(settings):
    settings.PASSWORD_HASHERS = FAST_HASHERS


@pytest.fixture
def tc25():
    owner = make_user("+639178000001", "TC25 Owner", User.Role.OWNER)
    farm = Farm.objects.create(name="TC25 Farm", owner=owner)
    house = House.objects.create(farm=farm, name="House 1", capacity=2000)
    start = timezone.localdate() - timedelta(days=40)
    batch = Batch.objects.create(
        house=house, batch_code="TC-25", initial_bird_count=1000, start_date=start, created_by=owner
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    farm_kw = {"farm_pk": farm.pk}
    batch_kw = {"farm_pk": farm.pk, "batch_pk": batch.pk}

    def ok(response, status=(200, 201)):
        assert response.status_code in status, getattr(response, "data", response.status_code)
        return response

    ok(client.post(reverse("production:feed-list", kwargs=farm_kw), {
        "delivery_date": start.isoformat(),
        "feed_type": "STARTER",
        "quantity_kg": "1500.00",
        "unit_cost": str(FEED_PRICE),
    }, format="json"))

    days = [
        {
            "id": str(uuid.uuid4()),
            "record_date": (start + timedelta(days=n)).isoformat(),
            "mortality_disease": 10,
            "mortality_heat": 0,
            "mortality_culled": 0,
            "mortality_unknown": 0,
            "feed_kg": "300.00",
        }
        for n in range(1, 6)
    ]
    ok(client.post(reverse("production:daily-bulk-sync", kwargs=batch_kw), {"records": days}, format="json"))

    sale_day = (start + timedelta(days=35)).isoformat()
    ok(client.post(reverse("production:sale-bulk-sync", kwargs=batch_kw), {"records": [
        {"id": str(uuid.uuid4()), "sale_date": sale_day, "sale_type": "LIVE",
         "bird_count": 900, "total_weight_kg": "900.00", "price_per_kg": "70.00"},
        {"id": str(uuid.uuid4()), "sale_date": sale_day, "sale_type": "OTHERS",
         "part_name": "Gizzards", "total_weight_kg": "10.00", "price_per_kg": "50.00"},
    ]}, format="json"))
    ok(client.post(reverse("production:batch-close", kwargs=batch_kw), {}, format="json"))
    return client, farm, batch


def _csv_row(client, url_name, farm, batch_code):
    response = client.get(reverse(url_name, kwargs={"farm_pk": farm.pk}))
    assert response.status_code == 200
    text = b"".join(response.streaming_content).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    return next(row for row in rows if batch_code in row.values())


def test_mortality_rate(tc25):
    client, farm, batch = tc25
    detail = client.get(reverse("production:batch-detail", kwargs={"farm_pk": farm.pk, "pk": batch.pk})).data
    assert detail["total_mortality"] == 50
    assert detail["mortality_rate_pct"] == "5.00"
    chart = client.get(
        reverse("analytics:batch-mortality", kwargs={"farm_pk": farm.pk, "batch_pk": batch.pk})
    ).data
    assert chart["summary"]["mortality_rate_pct"] == "5.00"


def test_survival_and_fcr(tc25):
    client, farm, _ = tc25
    row = client.get(reverse("analytics:fcr", kwargs={"farm_pk": farm.pk})).data["rows"][0]
    assert row["birds_harvested"] == 900
    assert row["survival_rate_pct"] == "90.00"
    assert row["total_feed_kg"] == "1500.00"
    assert row["total_weight_kg"] == "900.00"
    assert row["fcr"] == "1.667"


def test_feed_margin(tc25):
    client, farm, _ = tc25
    row = client.get(reverse("analytics:profitability", kwargs={"farm_pk": farm.pk})).data["rows"][0]
    assert row["revenue"] == "63500.00"
    assert row["allocated_feed_cost"] == str(Decimal("1500") * FEED_PRICE)
    assert row["feed_margin"] == "18500.00"
    assert row["feed_margin_per_kg"] == "20.56"


def test_the_exports_carry_the_same_figures(tc25):
    client, farm, batch = tc25
    fcr = _csv_row(client, "analytics:report-fcr", farm, batch.batch_code)
    margin = _csv_row(client, "analytics:report-feed-margin", farm, batch.batch_code)
    assert "1.667" in fcr.values()
    assert "90.00" in fcr.values()
    assert "18500.00" in margin.values()
