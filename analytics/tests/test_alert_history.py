# analytics/tests/test_alert_history.py
"""
Alert history — AlertLog, sync_alert_logs(), and
GET /api/farms/<farm_pk>/alerts/history/.

The bell is still computed live; the history remembers when each condition
was first seen and when it cleared. These tests pin the sync rules (one
open log per condition, resolve on clear, reopen on return, always from the
farm-wide set), retention, and the history endpoint's role filter,
pagination, and access rules.
"""
import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib import admin
from django.core.management import call_command
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from analytics.alert_history import RETENTION_DAYS, sync_alert_logs
from analytics.alerts import compute_alerts
from analytics.models import AlertLog
from production.models import (
    Batch,
    DailyRecord,
    House,
    InventoryItem,
    InventoryStockIn,
    InventoryUsageLog,
)

pytestmark = pytest.mark.django_db

EVERYONE = ["OWNER", "MANAGER", "WORKER"]
OWNER_MANAGER = ["OWNER", "MANAGER"]
ROW_KEYS = {
    "id",
    "alert_key",
    "severity",
    "title",
    "detail",
    "link",
    "first_seen_at",
    "resolved_at",
}


@pytest.fixture
def alerts_url(staffed_farm):
    return reverse("analytics:alerts", kwargs={"farm_pk": staffed_farm.pk})


@pytest.fixture
def history_url(staffed_farm):
    return reverse("analytics:alert-history", kwargs={"farm_pk": staffed_farm.pk})


@pytest.fixture
def house(staffed_farm):
    return House.objects.create(farm=staffed_farm, name="House 1", capacity=20000)


def make_batch(house, owner, *, code, birds=1000, started_days_ago=10):
    return Batch.objects.create(
        house=house,
        batch_code=code,
        initial_bird_count=birds,
        start_date=timezone.localdate() - timedelta(days=started_days_ago),
        created_by=owner,
    )


def add_record(batch, owner, *, day, disease=0, feed="0.00"):
    return DailyRecord.objects.create(
        batch=batch,
        record_date=day,
        mortality_disease=disease,
        mortality_heat=0,
        mortality_culled=0,
        mortality_unknown=0,
        feed_kg=Decimal(feed),
        recorded_by=owner,
    )


def make_log(farm, key, *, audience, first_seen_at=None, resolved_at=None):
    return AlertLog.objects.create(
        farm=farm,
        alert_key=key,
        severity="info",
        title=f"Title for {key}",
        detail="",
        link="/somewhere",
        audience=audience,
        first_seen_at=first_seen_at or timezone.now(),
        resolved_at=resolved_at,
    )


def an_alert(key, *, audience=OWNER_MANAGER, severity="warning", title=None):
    return {
        "id": key,
        "severity": severity,
        "title": title or f"Alert {key}",
        "detail": f"Detail for {key}",
        "link": "/records",
        "audience": list(audience),
    }


def open_keys(farm):
    return set(
        AlertLog.objects.filter(farm=farm, resolved_at__isnull=True).values_list(
            "alert_key", flat=True
        )
    )


# ─────────────────────────────────────────────────────────────
# Sync from GET /alerts/
# ─────────────────────────────────────────────────────────────


class TestSyncFromAlertsEndpoint:
    def test_get_opens_one_log_per_alert(
        self, auth, owner, alerts_url, house, staffed_farm
    ):
        batch = make_batch(house, owner, code="LOG-1")
        # 9% mortality yesterday, nothing recorded today → two alerts.
        add_record(batch, owner, day=timezone.localdate() - timedelta(days=1), disease=90)

        resp = auth(owner).get(alerts_url)
        assert resp.status_code == 200

        expected = {a["id"] for a in compute_alerts(staffed_farm)}
        assert {f"mortality:{batch.id}", f"today:{batch.id}"} <= expected
        assert AlertLog.objects.filter(farm=staffed_farm).count() == len(expected)
        assert open_keys(staffed_farm) == expected

        log = AlertLog.objects.get(alert_key=f"mortality:{batch.id}")
        assert log.severity == "danger"
        assert log.title == "High mortality in LOG-1"
        assert "9.00%" in log.detail
        assert log.link == "/analytics"
        assert log.audience == EVERYONE
        assert log.resolved_at is None

    def test_repeated_gets_do_not_duplicate_logs(
        self, auth, owner, alerts_url, house, staffed_farm
    ):
        batch = make_batch(house, owner, code="LOG-2")

        auth(owner).get(alerts_url)
        first = AlertLog.objects.get(alert_key=f"today:{batch.id}")
        auth(owner).get(alerts_url)
        auth(owner).get(alerts_url)

        logs = AlertLog.objects.filter(alert_key=f"today:{batch.id}")
        assert logs.count() == 1
        assert logs.get().pk == first.pk
        assert logs.get().first_seen_at == first.first_seen_at

    def test_cleared_condition_is_resolved_and_reopens_if_it_returns(
        self, auth, owner, alerts_url, house, staffed_farm
    ):
        batch = make_batch(house, owner, code="LOG-3")
        key = f"today:{batch.id}"

        auth(owner).get(alerts_url)
        assert key in open_keys(staffed_farm)

        record = add_record(batch, owner, day=timezone.localdate())
        auth(owner).get(alerts_url)
        first = AlertLog.objects.get(alert_key=key)
        assert first.resolved_at is not None
        assert key not in open_keys(staffed_farm)

        record.delete()
        auth(owner).get(alerts_url)
        logs = AlertLog.objects.filter(alert_key=key).order_by("pk")
        assert logs.count() == 2
        resolved, reopened = logs
        assert resolved.pk == first.pk
        assert resolved.resolved_at == first.resolved_at
        assert reopened.resolved_at is None
        assert reopened.first_seen_at >= resolved.resolved_at

    def test_open_log_wording_is_refreshed_while_the_condition_stands(
        self, auth, owner, alerts_url, house, staffed_farm
    ):
        batch = make_batch(house, owner, code="LOG-4")
        add_record(batch, owner, day=timezone.localdate() - timedelta(days=2), disease=90)

        auth(owner).get(alerts_url)
        before = AlertLog.objects.get(alert_key=f"mortality:{batch.id}")
        assert "9.00%" in before.detail

        add_record(batch, owner, day=timezone.localdate() - timedelta(days=1), disease=30)
        auth(owner).get(alerts_url)

        after = AlertLog.objects.get(alert_key=f"mortality:{batch.id}")
        assert after.pk == before.pk
        assert after.first_seen_at == before.first_seen_at
        assert after.resolved_at is None
        assert "12.00%" in after.detail

    def test_restricted_worker_does_not_resolve_other_houses_today_logs(
        self, auth, owner, worker, alerts_url, house, staffed_farm
    ):
        house2 = House.objects.create(farm=staffed_farm, name="House 2", capacity=20000)
        mine = make_batch(house, owner, code="MINE-1")
        theirs = make_batch(house2, owner, code="THEIRS-1")
        membership = staffed_farm.memberships.get(user=worker)
        membership.houses.set([house])

        auth(owner).get(alerts_url)
        assert {f"today:{mine.id}", f"today:{theirs.id}"} <= open_keys(staffed_farm)

        resp = auth(worker).get(alerts_url)
        worker_ids = {a["id"] for a in resp.data["alerts"]}
        # The worker's bell is still house-scoped...
        assert f"today:{mine.id}" in worker_ids
        assert f"today:{theirs.id}" not in worker_ids
        # ...but the history keeps the other house's alert open.
        assert {f"today:{mine.id}", f"today:{theirs.id}"} <= open_keys(staffed_farm)
        assert not AlertLog.objects.filter(resolved_at__isnull=False).exists()

    def test_restricted_worker_poll_records_the_farm_wide_set(
        self, auth, owner, worker, alerts_url, house, staffed_farm
    ):
        house2 = House.objects.create(farm=staffed_farm, name="House 2", capacity=20000)
        make_batch(house, owner, code="MINE-2")
        make_batch(house2, owner, code="THEIRS-2")
        add_record(  # negative feed balance: an owner/manager-only alert
            Batch.objects.get(batch_code="MINE-2"),
            owner,
            day=timezone.localdate() - timedelta(days=1),
            feed="500.00",
        )
        staffed_farm.memberships.get(user=worker).houses.set([house])

        auth(worker).get(alerts_url)

        assert open_keys(staffed_farm) == {a["id"] for a in compute_alerts(staffed_farm)}
        assert "feed-balance" in open_keys(staffed_farm)

    @pytest.mark.parametrize("who", ["owner", "manager", "worker", "restricted_worker"])
    def test_alerts_response_is_unchanged(
        self, request, auth, owner, worker, alerts_url, house, staffed_farm, who
    ):
        house2 = House.objects.create(farm=staffed_farm, name="House 2", capacity=20000)
        b1 = make_batch(house, owner, code="SAME-1")
        make_batch(house2, owner, code="SAME-2")
        add_record(b1, owner, day=timezone.localdate() - timedelta(days=1), disease=90, feed="500.00")
        if who == "restricted_worker":
            staffed_farm.memberships.get(user=worker).houses.set([house])
            user = worker
        else:
            user = request.getfixturevalue(who)
        membership = staffed_farm.membership_for(user)

        resp = auth(user).get(alerts_url)

        expected = [
            a
            for a in compute_alerts(staffed_farm, membership)
            if membership.role in a["audience"]
        ]
        assert set(resp.data) == {"alerts", "counts"}
        assert resp.data["alerts"] == expected
        assert resp.data["counts"] == {
            sev: sum(1 for a in expected if a["severity"] == sev)
            for sev in ("danger", "warning", "info")
        }


# ─────────────────────────────────────────────────────────────
# sync_alert_logs() rules
# ─────────────────────────────────────────────────────────────


class TestSyncRules:
    def test_retention_deletes_old_resolved_logs_and_keeps_open_ones(
        self, staffed_farm, rival_farm
    ):
        now = timezone.now()
        old = now - timedelta(days=RETENTION_DAYS + 1)
        recent = now - timedelta(days=RETENTION_DAYS - 1)

        expired = make_log(
            staffed_farm, "expired", audience=EVERYONE,
            first_seen_at=old - timedelta(days=1), resolved_at=old,
        )
        kept_resolved = make_log(
            staffed_farm, "recent", audience=EVERYONE,
            first_seen_at=recent - timedelta(days=1), resolved_at=recent,
        )
        long_open = make_log(
            staffed_farm, "still-standing", audience=EVERYONE,
            first_seen_at=now - timedelta(days=RETENTION_DAYS * 3),
        )
        other_farm = make_log(
            rival_farm, "expired", audience=EVERYONE,
            first_seen_at=old - timedelta(days=1), resolved_at=old,
        )

        sync_alert_logs(staffed_farm, [an_alert("still-standing")])

        remaining = set(AlertLog.objects.values_list("pk", flat=True))
        assert expired.pk not in remaining
        assert {kept_resolved.pk, long_open.pk, other_farm.pk} <= remaining
        long_open.refresh_from_db()
        assert long_open.resolved_at is None

    def test_only_this_farms_open_logs_are_resolved(self, staffed_farm, rival_farm):
        sync_alert_logs(rival_farm, [an_alert("feed-balance")])
        sync_alert_logs(staffed_farm, [an_alert("feed-balance")])

        sync_alert_logs(staffed_farm, [])

        assert open_keys(staffed_farm) == set()
        assert open_keys(rival_farm) == {"feed-balance"}

    def test_severity_and_wording_follow_the_alert_but_audience_is_fixed(
        self, staffed_farm
    ):
        sync_alert_logs(staffed_farm, [an_alert("low-stock:1", title="Low stock: Feed")])
        sync_alert_logs(
            staffed_farm,
            [
                an_alert(
                    "low-stock:1",
                    severity="danger",
                    title="Out of stock: Feed",
                    audience=EVERYONE,
                )
            ],
        )

        log = AlertLog.objects.get(alert_key="low-stock:1")
        assert log.severity == "danger"
        assert log.title == "Out of stock: Feed"
        assert log.audience == OWNER_MANAGER

    def test_overlong_title_is_clipped_not_an_error(self, staffed_farm):
        sync_alert_logs(staffed_farm, [an_alert("long", title="x" * 400)])

        assert len(AlertLog.objects.get(alert_key="long").title) == 255

    def test_losing_a_race_to_open_the_same_log_is_not_an_error(
        self, staffed_farm, monkeypatch
    ):
        real_bulk_create = AlertLog.objects.bulk_create

        def racing_bulk_create(objs, **kwargs):
            # Another request opens the same log between this sync's read
            # of the open logs and its insert.
            make_log(staffed_farm, "feed-balance", audience=OWNER_MANAGER)
            return real_bulk_create(objs, **kwargs)

        monkeypatch.setattr(AlertLog.objects, "bulk_create", racing_bulk_create)

        sync_alert_logs(staffed_farm, [an_alert("feed-balance")])

        assert AlertLog.objects.filter(alert_key="feed-balance").count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_simultaneous_syncs_neither_fail_nor_duplicate(self, farm):
        errors = []

        def run(alerts, barrier):
            try:
                barrier.wait()
                sync_alert_logs(farm, alerts)
            except Exception as exc:  # collected and asserted below
                errors.append(exc)
            finally:
                connection.close()

        # Each round opens new conditions and clears the previous round's,
        # so both the insert race and the resolve race are exercised.
        for round_no in range(5):
            alerts = [an_alert(f"cond-{round_no}-{i}") for i in range(3)]
            barrier = threading.Barrier(2)
            threads = [
                threading.Thread(target=run, args=(alerts, barrier)) for _ in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert errors == []
        assert open_keys(farm) == {f"cond-4-{i}" for i in range(3)}
        assert AlertLog.objects.filter(farm=farm).count() == 15


# ─────────────────────────────────────────────────────────────
# GET /alerts/history/
# ─────────────────────────────────────────────────────────────


class TestHistoryAudience:
    @pytest.fixture
    def logs(self, staffed_farm):
        return {
            "everyone": make_log(staffed_farm, "today:1", audience=EVERYONE),
            "owner_manager": make_log(staffed_farm, "feed-balance", audience=OWNER_MANAGER),
            "owner_only": make_log(staffed_farm, "owner-only", audience=["OWNER"]),
        }

    def keys(self, resp):
        assert resp.status_code == 200
        return {row["alert_key"] for row in resp.data["results"]}

    def test_owner_sees_every_log_addressed_to_owners(self, auth, owner, history_url, logs):
        assert self.keys(auth(owner).get(history_url)) == {
            "today:1", "feed-balance", "owner-only",
        }

    def test_manager_does_not_see_owner_only_logs(self, auth, manager, history_url, logs):
        assert self.keys(auth(manager).get(history_url)) == {"today:1", "feed-balance"}

    def test_worker_sees_only_everyone_logs(self, auth, worker, history_url, logs):
        assert self.keys(auth(worker).get(history_url)) == {"today:1"}


class TestHistoryShape:
    def test_row_has_exactly_the_contract_fields(self, auth, owner, history_url, staffed_farm):
        resolved_at = timezone.now()
        log = make_log(
            staffed_farm, "low-stock:7", audience=OWNER_MANAGER,
            first_seen_at=resolved_at - timedelta(hours=3), resolved_at=resolved_at,
        )

        resp = auth(owner).get(history_url)

        assert resp.status_code == 200
        assert set(resp.data) == {"count", "next", "previous", "results"}
        (row,) = resp.data["results"]
        assert set(row) == ROW_KEYS
        assert row["id"] == log.pk
        assert row["alert_key"] == "low-stock:7"
        assert row["severity"] == "info"
        assert row["title"] == "Title for low-stock:7"
        assert row["link"] == "/somewhere"
        assert row["first_seen_at"] is not None
        assert row["resolved_at"] is not None

    def test_open_log_has_null_resolved_at(self, auth, owner, history_url, staffed_farm):
        make_log(staffed_farm, "feed-balance", audience=OWNER_MANAGER)

        (row,) = auth(owner).get(history_url).data["results"]
        assert row["resolved_at"] is None

    def test_pagination_shape_and_order_with_25_logs(
        self, auth, owner, history_url, staffed_farm
    ):
        base = timezone.now() - timedelta(days=10)
        logs = [
            make_log(
                staffed_farm, f"cond-{i}", audience=EVERYONE,
                # Pairs share a timestamp, so the id tiebreak is exercised.
                first_seen_at=base + timedelta(hours=i // 2),
            )
            for i in range(25)
        ]
        expected = [
            log.pk
            for log in sorted(logs, key=lambda l: (l.first_seen_at, l.pk), reverse=True)
        ]
        client = auth(owner)

        page1 = client.get(history_url)
        assert page1.status_code == 200
        assert page1.data["count"] == 25
        assert len(page1.data["results"]) == 20
        assert page1.data["previous"] is None
        assert page1.data["next"].endswith("?page=2")

        page2 = client.get(history_url, {"page": 2})
        assert page2.status_code == 200
        assert page2.data["count"] == 25
        assert len(page2.data["results"]) == 5
        assert page2.data["next"] is None
        assert page2.data["previous"] is not None

        got = [row["id"] for row in page1.data["results"] + page2.data["results"]]
        assert got == expected

    def test_page_past_the_end_is_404(self, auth, owner, history_url, staffed_farm):
        make_log(staffed_farm, "feed-balance", audience=OWNER_MANAGER)

        assert auth(owner).get(history_url, {"page": 2}).status_code == 404

    def test_empty_history(self, auth, owner, history_url):
        resp = auth(owner).get(history_url)
        assert resp.status_code == 200
        assert resp.data == {"count": 0, "next": None, "previous": None, "results": []}

    def test_history_does_not_sync(self, auth, owner, history_url, house):
        make_batch(house, owner, code="NO-SYNC")

        auth(owner).get(history_url)

        assert not AlertLog.objects.exists()


class TestHistoryAccess:
    def test_member_of_another_farm_gets_403_like_alerts(
        self, auth, rival_owner, rival_farm, alerts_url, history_url, staffed_farm
    ):
        make_log(staffed_farm, "feed-balance", audience=OWNER_MANAGER)
        client = auth(rival_owner)

        assert client.get(alerts_url).status_code == 403
        assert client.get(history_url).status_code == 403

    def test_other_farms_logs_never_appear(
        self, auth, rival_owner, rival_farm, staffed_farm
    ):
        make_log(staffed_farm, "feed-balance", audience=OWNER_MANAGER)
        rival_history = reverse(
            "analytics:alert-history", kwargs={"farm_pk": rival_farm.pk}
        )

        resp = auth(rival_owner).get(rival_history)

        assert resp.status_code == 200
        assert resp.data["count"] == 0

    def test_buyer_gets_403_like_alerts(
        self, auth, consumer, alerts_url, history_url, staffed_farm
    ):
        make_log(staffed_farm, "today:1", audience=EVERYONE)
        client = auth(consumer)

        assert client.get(alerts_url).status_code == 403
        assert client.get(history_url).status_code == 403

    def test_revoked_member_gets_403(self, auth, worker, history_url, staffed_farm):
        staffed_farm.memberships.filter(user=worker).update(is_active=False)

        assert auth(worker).get(history_url).status_code == 403

    def test_unauthenticated_gets_401(self, api, history_url):
        assert api.get(history_url).status_code == 401


# ─────────────────────────────────────────────────────────────
# send_stock_alerts job
# ─────────────────────────────────────────────────────────────


def _make_low_item(farm, name, worker):
    item = InventoryItem.objects.create(
        farm=farm, name=name, unit="sack", low_stock_threshold=Decimal("10")
    )
    InventoryStockIn.objects.create(
        item=item, quantity=Decimal("10"), stock_in_date=timezone.localdate(), recorded_by=worker
    )
    InventoryUsageLog.objects.create(
        item=item, quantity_used=Decimal("5"), usage_date=timezone.localdate(), recorded_by=worker
    )
    return item


class TestSendStockAlertsRecordsHistory:
    def test_records_a_low_stock_log(self, farm, worker):
        item = _make_low_item(farm, "Feed Sacks", worker)

        call_command("send_stock_alerts")

        log = AlertLog.objects.get(farm=farm, alert_key=f"low-stock:{item.id}")
        assert log.resolved_at is None
        assert log.severity == "warning"
        assert log.title == "Low stock: Feed Sacks"
        assert log.audience == OWNER_MANAGER

    def test_resolves_the_log_once_restocked(self, farm, worker):
        item = _make_low_item(farm, "Feed Sacks", worker)
        call_command("send_stock_alerts")

        InventoryStockIn.objects.create(
            item=item, quantity=Decimal("100"), stock_in_date=timezone.localdate(),
            recorded_by=worker,
        )
        call_command("send_stock_alerts")

        log = AlertLog.objects.get(farm=farm, alert_key=f"low-stock:{item.id}")
        assert log.resolved_at is not None

    def test_running_twice_does_not_duplicate(self, farm, worker):
        item = _make_low_item(farm, "Feed Sacks", worker)

        call_command("send_stock_alerts")
        call_command("send_stock_alerts")

        assert AlertLog.objects.filter(alert_key=f"low-stock:{item.id}").count() == 1

    def test_archived_farm_is_not_synced(self, farm, worker):
        _make_low_item(farm, "Feed Sacks", worker)
        farm.archive()

        call_command("send_stock_alerts")

        assert not AlertLog.objects.exists()


class TestAdmin:
    def test_alert_log_is_not_registered_in_the_admin(self):
        assert not admin.site.is_registered(AlertLog)

    def test_no_analytics_model_is_registered_in_the_admin(self):
        registered = {
            model._meta.model_name
            for model in admin.site._registry
            if model._meta.app_label == "analytics"
        }
        assert registered == set()
