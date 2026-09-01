# production/tests/test_daily_record_timezone.py
"""
Regression test for the settings.TIME_ZONE bug.

DailyRecordSerializer.validate_record_date rejects a record_date after
timezone.localdate() as "in the future." That check is only correct if
"today" is computed in the timezone the farm actually operates in. With
TIME_ZONE='UTC', a worker in Manila (UTC+8) recording their own "today"
between midnight and 8am local time gets rejected — the server's UTC
clock still reads yesterday's date.

These tests freeze django.utils.timezone.now() to an instant chosen so
Manila and UTC disagree about the calendar date, and prove the serializer
follows whichever TIME_ZONE is configured — accepting the record under
Asia/Manila (the fix) and rejecting the identical payload under UTC (the
bug this settings change closes).
"""
from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.test import override_settings

from production.models import Batch, House

pytestmark = pytest.mark.django_db

# 19:00 UTC on 30 Aug 2026 is 03:00 Asia/Manila on 31 Aug 2026 — Manila has
# already turned over to the next calendar day; UTC has not.
FROZEN_UTC_INSTANT = datetime(2026, 8, 30, 19, 0, 0, tzinfo=dt_timezone.utc)
MANILA_TODAY = date(2026, 8, 31)


@pytest.fixture
def house(farm):
    return House.objects.create(farm=farm, name="Test House", capacity=5000)


@pytest.fixture
def batch(house, owner):
    return Batch.objects.create(
        house=house,
        batch_code="TZ-TEST-001",
        initial_bird_count=1000,
        start_date=date(2026, 1, 1),
        created_by=owner,
    )


def _bulk_sync_payload(record_date):
    return {
        "records": [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "recorded_at": "2026-08-30T19:05:00Z",
                "record_date": record_date.isoformat(),
                "mortality_disease": 0,
                "mortality_heat": 0,
                "mortality_culled": 0,
                "mortality_unknown": 0,
                "feed_kg": "12.50",
                "notes": "",
            }
        ]
    }


def _sync(api_client, farm, batch, record_date):
    return api_client.post(
        f"/api/farms/{farm.id}/batches/{batch.id}/daily-records/bulk-sync/",
        _bulk_sync_payload(record_date),
        format="json",
    )


def test_record_dated_today_in_manila_is_accepted_at_a_utc_clock_lagging_a_day_behind(
    auth, owner, farm, batch,
):
    with patch("django.utils.timezone.now", return_value=FROZEN_UTC_INSTANT):
        response = _sync(auth(owner), farm, batch, MANILA_TODAY)

    assert response.status_code == 200, response.data
    assert response.data["failed"] == []
    assert len(response.data["created"]) == 1

    record = batch.daily_records.get(pk=response.data["created"][0])
    assert record.record_date == MANILA_TODAY


def test_same_record_would_have_been_rejected_under_the_old_utc_time_zone(
    auth, owner, farm, batch,
):
    """
    Documents the bug this settings change fixes: the exact same instant
    and the exact same record_date, but with TIME_ZONE reverted to UTC —
    the server's "today" lags Manila's by a calendar day, so the honestly
    same-day record reads as being from the future.
    """
    with override_settings(TIME_ZONE="UTC"), patch(
        "django.utils.timezone.now", return_value=FROZEN_UTC_INSTANT
    ):
        response = _sync(auth(owner), farm, batch, MANILA_TODAY)

    assert response.status_code == 400
    assert "future date" in str(response.data).lower()
