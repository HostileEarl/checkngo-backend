# partners/tests/test_buyer_two_farms.py
"""
A buyer linked to two farms (manuscript review, item A2).

Buyer X is linked to ALPHA and BRAVO and has sales and payments on both.
Buyer Y is linked to ALPHA only. Buyer Z is linked to BRAVO only and buys
from the same BRAVO batch as X. Every buyer-facing endpoint
(/partners/my-farms/, /my-purchases/, /my-payments/) is checked for:

  - each row naming the farm the sale or payment actually happened on;
  - X's rows being exactly X's: never Y's or Z's, even on a shared batch;
  - per-farm figures staying per farm, with and without ?farm=;
  - Y seeing nothing from BRAVO, even when asking for it by id;
  - no price, revenue, balance, or other buyer's name in any payload.

The single-farm cases (one buyer per farm, revenue withheld, ?farm=
narrowing) are in test_my_purchases.py and test_my_payments.py; this file
adds the shape the adviser asked for, one buyer across two farms beside
buyers who share a farm with them.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from farms.tests.two_farms import FAST_HASHERS, build_farm, make_user
from partners.models import FarmPartnerLink
from production.models import Payment, SaleEvent

pytestmark = pytest.mark.django_db

PURCHASES = reverse("partners:my-purchases")
PAYMENTS = reverse("partners:my-payments")
MY_FARMS = reverse("partners:my-partner-farms")
BUYER_ENDPOINTS = (MY_FARMS, PURCHASES, PAYMENTS)

# Keys a buyer must never receive, at any depth: the farm's price, revenue
# and cost figures, the sale's balance, and the fields that name a sale's
# buyer (which on a shared batch would be someone else).
FORBIDDEN_KEYS = {
    "price_per_kg", "revenue", "revenue_per_kg", "total_paid", "outstanding",
    "is_fully_paid", "unit_cost", "total_cost", "buyer_name", "buyer_display",
    "buyer_link",
}


@pytest.fixture(autouse=True)
def _fast_hasher(settings):
    settings.PASSWORD_HASHERS = FAST_HASHERS


def _link(farm, buyer, owner, business_name):
    return FarmPartnerLink.objects.create(
        farm=farm,
        partner=buyer,
        link_type=FarmPartnerLink.LinkType.CONSUMER,
        business_name=business_name,
        linked_by=owner,
    )


def _sale(batch, link, *, birds, kg, price, days_ago=2):
    return SaleEvent.objects.create(
        batch=batch,
        sale_date=timezone.localdate() - timedelta(days=days_ago),
        sale_type=SaleEvent.SaleType.DRESSED,
        bird_count=birds,
        total_weight_kg=Decimal(kg),
        price_per_kg=Decimal(price),
        buyer_link=link,
        notes="internal sale note",
    )


def _payment(sale, link, amount, owner):
    return Payment.objects.create(
        sale=sale,
        amount=Decimal(amount),
        received_date=timezone.localdate() - timedelta(days=1),
        buyer_link=link,
        note="internal payment note",
        recorded_by=owner,
    )


@pytest.fixture
def market(db):
    """
    X on both farms (build_farm links the same buyer to each, with a sale
    and closed sale and a payment on each farm), Y on ALPHA only, Z on
    BRAVO only. Y and Z buy from the same live batches as X.
    """
    x = make_user("+639173000001", "Buyer X Shared", User.Role.CONSUMER)
    alpha = build_farm("ALPHA", "+639171000", buyer=x)
    bravo = build_farm("BRAVO", "+639172000", buyer=x)

    y = make_user("+639173000002", "Buyer Y Alpha Only", User.Role.CONSUMER)
    z = make_user("+639173000003", "Buyer Z Bravo Only", User.Role.CONSUMER)
    y_link = _link(alpha.farm, y, alpha.owner, "Y Trading")
    z_link = _link(bravo.farm, z, bravo.owner, "Z Poultry Mart")

    y_sale = _sale(alpha.batch, y_link, birds=30, kg="66.00", price="171.00")
    z_sale = _sale(bravo.batch, z_link, birds=40, kg="88.00", price="173.00")
    y_payment = _payment(y_sale, y_link, "1111.00", alpha.owner)
    z_payment = _payment(z_sale, z_link, "2222.00", bravo.owner)

    # A second X sale on BRAVO, plus a payment recorded with no buyer of its
    # own on it: an older client sent none, and the sale names X.
    x_extra_sale = _sale(bravo.batch, bravo.buyer_link, birds=10, kg="21.50", price="177.00", days_ago=3)
    x_unattributed_payment = _payment(x_extra_sale, None, "333.00", bravo.owner)

    return {
        "alpha": alpha, "bravo": bravo, "x": x, "y": y, "z": z,
        "y_link": y_link, "z_link": z_link,
        "y_sale": y_sale, "z_sale": z_sale,
        "y_payment": y_payment, "z_payment": z_payment,
        "x_extra_sale": x_extra_sale,
        "x_unattributed_payment": x_unattributed_payment,
    }


def _get(api, user, url, **params):
    api.force_authenticate(user=user)
    response = api.get(url, params)
    assert response.status_code == 200, response.data
    return response.data


def _walk(value):
    """Every (key, value) pair in a JSON payload, at any depth."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key, inner
            yield from _walk(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _walk(inner)


def _x_sales(m):
    return {
        m["alpha"].farm.pk: [m["alpha"].sale, m["alpha"].closed_sale],
        m["bravo"].farm.pk: [m["bravo"].sale, m["bravo"].closed_sale, m["x_extra_sale"]],
    }


def _x_payments(m):
    return {
        m["alpha"].farm.pk: [m["alpha"].payment],
        m["bravo"].farm.pk: [m["bravo"].payment, m["x_unattributed_payment"]],
    }


class TestBuyerOnTwoFarms:
    def test_purchases_are_exactly_xs_each_on_its_own_farm(self, api, market):
        rows = _get(api, market["x"], PURCHASES)
        expected = {
            str(sale.pk): farm_pk
            for farm_pk, sales in _x_sales(market).items()
            for sale in sales
        }
        # Every closed batch here is in per-sale mode (its sale names X), so
        # the Harvest row is dropped and only sale rows remain.
        assert {row["source"] for row in rows} == {"sale"}
        assert {str(row["id"]): row["farm"] for row in rows} == expected
        farm_names = {market["alpha"].farm.pk: "ALPHA Farm", market["bravo"].farm.pk: "BRAVO Farm"}
        for row in rows:
            assert row["farm_name"] == farm_names[row["farm"]]

    def test_payments_are_exactly_xs_each_on_its_own_farm(self, api, market):
        rows = _get(api, market["x"], PAYMENTS)
        expected = {
            payment.pk: farm_pk
            for farm_pk, payments in _x_payments(market).items()
            for payment in payments
        }
        assert {row["id"]: row["farm"] for row in rows} == expected

    def test_other_buyers_rows_on_the_same_batch_never_appear(self, api, market):
        purchase_ids = {str(r["id"]) for r in _get(api, market["x"], PURCHASES)}
        payment_ids = {r["id"] for r in _get(api, market["x"], PAYMENTS)}
        assert str(market["y_sale"].pk) not in purchase_ids
        assert str(market["z_sale"].pk) not in purchase_ids
        assert market["y_payment"].pk not in payment_ids
        assert market["z_payment"].pk not in payment_ids

    def test_per_farm_totals_are_not_mixed(self, api, market):
        """
        Weight bought and money paid, summed per farm from the unfiltered
        list, equal what the database holds for X on that farm alone, and
        equal the ?farm= filtered list's own total.
        """
        x = market["x"]
        purchases = _get(api, x, PURCHASES)
        payments = _get(api, x, PAYMENTS)

        weight_by_farm = defaultdict(Decimal)
        for row in purchases:
            weight_by_farm[row["farm"]] += Decimal(row["total_weight_kg"])
        paid_by_farm = defaultdict(Decimal)
        for row in payments:
            paid_by_farm[row["farm"]] += Decimal(row["amount"])

        for farm_pk, sales in _x_sales(market).items():
            expected_weight = sum((s.total_weight_kg for s in sales), Decimal("0"))
            assert weight_by_farm[farm_pk] == expected_weight
            filtered = _get(api, x, PURCHASES, farm=farm_pk)
            assert {r["farm"] for r in filtered} == {farm_pk}
            assert sum(Decimal(r["total_weight_kg"]) for r in filtered) == expected_weight

        for farm_pk, farm_payments in _x_payments(market).items():
            expected_paid = sum((p.amount for p in farm_payments), Decimal("0"))
            assert paid_by_farm[farm_pk] == expected_paid
            filtered = _get(api, x, PAYMENTS, farm=farm_pk)
            assert {r["farm"] for r in filtered} == {farm_pk}
            assert sum(Decimal(r["amount"]) for r in filtered) == expected_paid

    def test_my_farms_lists_both_links_and_nothing_else(self, api, market):
        rows = _get(api, market["x"], MY_FARMS)
        assert {r["farm"] for r in rows} == {market["alpha"].farm.pk, market["bravo"].farm.pk}
        assert {r["partner"] for r in rows} == {market["x"].pk}


class TestBuyerOnOneFarm:
    def test_y_sees_only_alpha(self, api, market):
        y, bravo = market["y"], market["bravo"]
        assert [r["farm"] for r in _get(api, y, MY_FARMS)] == [market["alpha"].farm.pk]
        purchases = _get(api, y, PURCHASES)
        payments = _get(api, y, PAYMENTS)
        assert [str(r["id"]) for r in purchases] == [str(market["y_sale"].pk)]
        assert [r["id"] for r in payments] == [market["y_payment"].pk]
        assert {r["farm"] for r in purchases + payments} == {market["alpha"].farm.pk}
        assert all(r["farm_name"] != bravo.farm.name for r in purchases + payments)

    def test_asking_for_bravo_by_id_returns_nothing(self, api, market):
        y, bravo = market["y"], market["bravo"]
        assert _get(api, y, PURCHASES, farm=bravo.farm.pk) == []
        assert _get(api, y, PAYMENTS, farm=bravo.farm.pk) == []

    def test_y_cannot_reach_bravos_farm_endpoints_either(self, api, market):
        api.force_authenticate(user=market["y"])
        bravo = market["bravo"]
        for url in (
            reverse("production:batch-list", kwargs={"farm_pk": bravo.farm.pk}),
            reverse("production:sale-list", kwargs={"farm_pk": bravo.farm.pk, "batch_pk": bravo.batch.pk}),
            reverse("partners:farm-partners", kwargs={"farm_pk": bravo.farm.pk}),
            reverse("analytics:report-sales-history", kwargs={"farm_pk": bravo.farm.pk}),
        ):
            assert api.get(url).status_code == 403, url


class TestNothingTheBuyerShouldNotSee:
    @pytest.mark.parametrize("buyer_key", ["x", "y", "z"])
    @pytest.mark.parametrize("url", BUYER_ENDPOINTS)
    def test_no_price_revenue_or_balance_key(self, api, market, buyer_key, url):
        payload = _get(api, market[buyer_key], url)
        assert payload, "the buyer should have rows here; an empty list proves nothing"
        leaked = sorted({key for key, _ in _walk(payload)} & FORBIDDEN_KEYS)
        assert not leaked, leaked

    @pytest.mark.parametrize("buyer_key", ["x", "y", "z"])
    @pytest.mark.parametrize("url", BUYER_ENDPOINTS)
    def test_no_price_or_revenue_value(self, api, market, buyer_key, url):
        """The figures themselves, under any key name."""
        sales = SaleEvent.objects.all()
        figures = set()
        for sale in sales:
            figures.add(str(sale.price_per_kg))
            figures.add(str(sale.revenue))
        payload = _get(api, market[buyer_key], url)
        values = {str(v) for _, v in _walk(payload) if not isinstance(v, (dict, list))}
        assert not (values & figures), sorted(values & figures)

    @pytest.mark.parametrize("buyer_key", ["x", "y", "z"])
    @pytest.mark.parametrize("url", BUYER_ENDPOINTS)
    def test_no_other_buyers_name(self, api, market, buyer_key, url):
        me = market[buyer_key]
        others = [market[k] for k in ("x", "y", "z") if k != buyer_key]
        names = set()
        for other in others:
            names.add(other.full_name)
            names.update(
                FarmPartnerLink.objects.filter(partner=other).values_list("business_name", flat=True)
            )
        api.force_authenticate(user=me)
        text = api.get(url).content.decode()
        assert not [name for name in names if name and name in text]
