# production/management/commands/seed_demo_data.py
"""
Generates SYNTHETIC demonstration data for CheckN Go.

This is not real farm data. Values are drawn from published Ross 308
broiler performance objectives (~4-6% cycle mortality, FCR 1.5-1.8,
~2.4kg live weight at 42 days) with randomised variation so the charts
show meaningful spread between well-run and poorly-run batches.

Every seeded batch is prefixed DEMO- so it can be identified and removed.
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from farms.models import Farm, FarmMembership
from partners.models import FarmPartnerLink
from production.models import (
    Batch,
    DailyRecord,
    FeedDelivery,
    Harvest,
    House,
    WeightSample,
)

DEMO_PREFIX = "DEMO-"


class Command(BaseCommand):
    help = "Populate the database with synthetic demo data for the analytics dashboard."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing DEMO- data before seeding.",
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=42,
            help="RNG seed. Fixed by default so demo runs are reproducible.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to seed synthetic data with DEBUG=False. "
                "This command is for local demonstration only."
            )

        random.seed(options["seed"])

        self.stdout.write(self.style.WARNING("=" * 68))
        self.stdout.write(self.style.WARNING("  SYNTHETIC DEMONSTRATION DATA"))
        self.stdout.write(self.style.WARNING("  Not real farm records. Generated from published"))
        self.stdout.write(self.style.WARNING("  Ross 308 broiler performance benchmarks."))
        self.stdout.write(self.style.WARNING("=" * 68))

        if options["reset"]:
            self._reset()

        with transaction.atomic():
            owner, manager, worker = self._create_staff()
            farm = self._create_farm(owner, manager, worker)
            supplier, buyer = self._create_partners(farm, owner)
            houses = self._create_houses(farm)
            self._create_feed_deliveries(farm, supplier, owner)
            self._create_batches(farm, houses, owner, worker, buyer)

        self._report()

    # ── teardown ────────────────────────────────────────────

    def _reset(self):
        self.stdout.write("Removing existing DEMO- data...")
        batches = Batch.objects.filter(batch_code__startswith=DEMO_PREFIX)
        count = batches.count()
        batches.delete()  # cascades to daily records, weights, harvests
        FeedDelivery.objects.filter(invoice_ref__startswith=DEMO_PREFIX).delete()
        self.stdout.write(f"  removed {count} demo batches")

    # ── people ──────────────────────────────────────────────

    def _create_staff(self):
        owner, created = User.objects.get_or_create(
            phone_number="+639171112222",
            defaults={
                "full_name": "Maria Santos",
                "role": User.Role.OWNER,
                "must_change_credential": False,
            },
        )
        if created:
            owner.set_password("111111")
            owner.save()

        manager, created = User.objects.get_or_create(
            phone_number="+639172223333",
            defaults={
                "full_name": "Jose Cruz",
                "role": User.Role.MANAGER,
                "must_change_credential": False,
            },
        )
        if created:
            manager.set_password("222222")
            manager.save()

        worker, created = User.objects.get_or_create(
            phone_number="+639173334444",
            defaults={
                "full_name": "Ana Reyes",
                "role": User.Role.WORKER,
                "must_change_credential": False,
            },
        )
        if created:
            worker.set_password("333333")
            worker.save()

        self.stdout.write(self.style.SUCCESS("✓ staff accounts"))
        return owner, manager, worker

    def _create_farm(self, owner, manager, worker):
        farm, _ = Farm.objects.get_or_create(
            owner=owner,
            name="Santos Broiler Farm",
            defaults={
                "municipality": "San Pablo",
                "province": "Laguna",
                "address": "Barangay San Antonio",
            },
        )
        # The post_save signal already granted the owner their membership.
        FarmMembership.objects.get_or_create(
            farm=farm, user=manager,
            defaults={"role": FarmMembership.Role.MANAGER, "invited_by": owner},
        )
        FarmMembership.objects.get_or_create(
            farm=farm, user=worker,
            defaults={"role": FarmMembership.Role.WORKER, "invited_by": manager},
        )
        self.stdout.write(self.style.SUCCESS(f"✓ farm: {farm.name}"))
        return farm

    def _create_partners(self, farm, owner):
        sup_user, created = User.objects.get_or_create(
            phone_number="+639175556666",
            defaults={
                "full_name": "Ricardo Lim",
                "role": User.Role.SUPPLIER,
                "must_change_credential": False,
            },
        )
        if created:
            sup_user.set_password("555555")
            sup_user.save()

        buy_user, created = User.objects.get_or_create(
            phone_number="+639176667777",
            defaults={
                "full_name": "Elena Bautista",
                "role": User.Role.CONSUMER,
                "must_change_credential": False,
            },
        )
        if created:
            buy_user.set_password("666666")
            buy_user.save()

        supplier, _ = FarmPartnerLink.objects.get_or_create(
            farm=farm, partner=sup_user,
            link_type=FarmPartnerLink.LinkType.SUPPLIER,
            defaults={"business_name": "Lim Feeds Trading", "linked_by": owner},
        )
        buyer, _ = FarmPartnerLink.objects.get_or_create(
            farm=farm, partner=buy_user,
            link_type=FarmPartnerLink.LinkType.CONSUMER,
            defaults={"business_name": "Bautista Poultry Dealers", "linked_by": owner},
        )
        self.stdout.write(self.style.SUCCESS("✓ supplier and buyer links"))
        return supplier, buyer

    def _create_houses(self, farm):
        specs = [("House 1", 5000), ("House 2", 5000), ("House 3", 3500)]
        houses = []
        for name, capacity in specs:
            house, _ = House.objects.get_or_create(
                farm=farm, name=name, defaults={"capacity": capacity}
            )
            houses.append(house)
        self.stdout.write(self.style.SUCCESS(f"✓ {len(houses)} houses"))
        return houses

    # ── feed ────────────────────────────────────────────────

    def _create_feed_deliveries(self, farm, supplier, owner):
        """
        Deliveries spread over eight months, with feed prices drifting upward
        — so cost-per-kg is not a flat line and the profitability chart has
        something to show.
        """
        today = timezone.localdate()
        types = [
            (FeedDelivery.FeedType.STARTER, Decimal("32.50")),
            (FeedDelivery.FeedType.GROWER, Decimal("30.00")),
            (FeedDelivery.FeedType.FINISHER, Decimal("28.75")),
        ]

        created = 0
        for month_back in range(8, 0, -1):
            delivery_date = today - timedelta(days=month_back * 30)
            drift = Decimal("1") + (Decimal(8 - month_back) * Decimal("0.015"))

            for feed_type, base_price in types:
                qty = Decimal(random.randrange(1500, 3500, 50))
                unit = (base_price * drift).quantize(Decimal("0.01"))

                FeedDelivery.objects.create(
                    farm=farm,
                    supplier_link=supplier,
                    delivery_date=delivery_date,
                    feed_type=feed_type,
                    quantity_kg=qty,
                    unit_cost=unit,
                    invoice_ref=f"{DEMO_PREFIX}INV-{delivery_date:%Y%m}-{feed_type[:3]}",
                    recorded_by=owner,
                    recorded_at=timezone.now(),
                )
                created += 1

        self.stdout.write(self.style.SUCCESS(f"✓ {created} feed deliveries"))

    # ── batches ─────────────────────────────────────────────

    def _create_batches(self, farm, houses, owner, worker, buyer):
        """
        Five harvested batches with deliberately varied performance, plus two
        still running. The spread is the point — a chart where every bar is
        identical demonstrates nothing.
        """
        today = timezone.localdate()

        # (label, days_ago_started, birds, quality) — quality drives the curves
        completed = [
            ("2025-09", 300, 4800, "good"),
            ("2025-11", 240, 4800, "average"),
            ("2026-01", 180, 3400, "poor"),      # heat event
            ("2026-03", 120, 4900, "excellent"),
            ("2026-05", 60, 4700, "average"),
        ]

        for i, (label, days_ago, birds, quality) in enumerate(completed):
            house = houses[i % len(houses)]
            start = today - timedelta(days=days_ago)
            batch = Batch.objects.create(
                house=house,
                batch_code=f"{DEMO_PREFIX}{label}",
                breed="Ross 308",
                initial_bird_count=birds,
                start_date=start,
                expected_harvest_date=start + timedelta(days=42),
                created_by=owner,
            )
            cycle = random.randint(40, 45)
            self._fill_daily_records(batch, cycle, quality, worker)
            self._fill_weight_samples(batch, cycle, quality, worker)
            self._harvest(batch, cycle, quality, buyer, owner)

        # Two active batches, mid-cycle, in the remaining free houses.
        occupied = {b.house_id for b in Batch.objects.filter(status=Batch.Status.ACTIVE)}
        free = [h for h in houses if h.pk not in occupied]

        for i, house in enumerate(free[:2]):
            age = [26, 14][i]
            start = today - timedelta(days=age)
            batch = Batch.objects.create(
                house=house,
                batch_code=f"{DEMO_PREFIX}ACTIVE-{i + 1}",
                breed="Ross 308",
                initial_bird_count=4600 - (i * 300),
                start_date=start,
                expected_harvest_date=start + timedelta(days=42),
                created_by=owner,
            )
            self._fill_daily_records(batch, age, "average", worker)
            self._fill_weight_samples(batch, age, "average", worker)

        self.stdout.write(self.style.SUCCESS("✓ 5 harvested + 2 active batches"))

    # ── curve generation ────────────────────────────────────

    def _fill_daily_records(self, batch, days, quality, worker):
        """
        Mortality follows the real broiler shape: a spike in week 1 (chick
        mortality), a low plateau, then a slight rise near harvest as birds
        get heavy. Feed intake climbs steadily with age.
        """
        profiles = {
            "excellent": {"target_pct": 3.2, "heat_event": False},
            "good": {"target_pct": 4.0, "heat_event": False},
            "average": {"target_pct": 5.0, "heat_event": False},
            "poor": {"target_pct": 9.5, "heat_event": True},
        }
        profile = profiles[quality]
        total_target = int(batch.initial_bird_count * profile["target_pct"] / 100)

        # Weight the daily distribution into the realistic U-shape.
        weights = []
        for day in range(1, days + 1):
            if day <= 7:
                w = 3.5 - (day * 0.3)       # early chick losses
            elif day <= 28:
                w = 0.5                      # healthy plateau
            else:
                w = 0.8 + ((day - 28) * 0.05)  # heavier birds, more culls
            weights.append(max(w, 0.2))

        total_weight = sum(weights)
        alive = batch.initial_bird_count
        records = []

        for day in range(1, days + 1):
            share = weights[day - 1] / total_weight
            base = total_target * share
            daily = max(0, int(random.gauss(base, base * 0.4)))

            # A heat event concentrates losses in a three-day window.
            if profile["heat_event"] and 22 <= day <= 24:
                daily += random.randint(60, 140)

            daily = min(daily, alive - 10)
            alive -= daily

            # Split by cause, shaped by age and event.
            if profile["heat_event"] and 22 <= day <= 24:
                heat = int(daily * 0.75)
                disease = int(daily * 0.10)
                culled = int(daily * 0.05)
            elif day <= 7:
                heat = 0
                disease = int(daily * 0.55)
                culled = int(daily * 0.25)
            else:
                heat = int(daily * 0.15)
                disease = int(daily * 0.35)
                culled = int(daily * 0.30)
            unknown = max(0, daily - heat - disease - culled)

            # Feed intake per bird, grams/day, roughly Ross 308 shape.
            g_per_bird = min(20 + (day * 4.2), 185)
            feed_kg = Decimal(alive * g_per_bird / 1000).quantize(Decimal("0.01"))

            record_date = batch.start_date + timedelta(days=day)
            recorded_at = timezone.make_aware(
                timezone.datetime.combine(
                    record_date, timezone.datetime.min.time()
                )
            ) + timedelta(hours=random.randint(6, 8))

            records.append(
                DailyRecord(
                    batch=batch,
                    record_date=record_date,
                    mortality_disease=disease,
                    mortality_heat=heat,
                    mortality_culled=culled,
                    mortality_unknown=unknown,
                    feed_kg=feed_kg,
                    recorded_by=worker,
                    recorded_at=recorded_at,
                )
            )

        DailyRecord.objects.bulk_create(records)
        # bulk_create skips signals, so refresh the denormalized totals.
        batch.recalculate_totals()

    def _fill_weight_samples(self, batch, days, quality, worker):
        """Weekly weighing. Ross 308 reaches ~2.4kg at day 42 when well managed."""
        multipliers = {
            "excellent": 1.06,
            "good": 1.02,
            "average": 1.0,
            "poor": 0.90,
        }
        m = multipliers[quality]

        for week_day in range(7, days + 1, 7):
            # Ross 308 approximate live weight by age.
            grams = (0.045 * (week_day ** 2.35) + 42) * m
            grams *= random.uniform(0.97, 1.03)

            WeightSample.objects.create(
                batch=batch,
                sample_date=batch.start_date + timedelta(days=week_day),
                birds_weighed=random.choice([25, 30, 40, 50]),
                average_grams=Decimal(grams).quantize(Decimal("0.01")),
                recorded_by=worker,
                recorded_at=timezone.now(),
            )

    def _harvest(self, batch, cycle_days, quality, buyer, owner):
        batch.refresh_from_db()
        alive = batch.current_bird_count

        final = batch.weight_samples.order_by("-sample_date").first()
        avg_kg = Decimal(final.average_grams) / 1000 if final else Decimal("2.3")

        # A small residual loss during catching and transport.
        harvested = int(alive * random.uniform(0.985, 0.998))
        total_kg = (Decimal(harvested) * avg_kg).quantize(Decimal("0.01"))

        # Live-weight farmgate price, PHP/kg, with market variation.
        price = Decimal(random.uniform(115, 138)).quantize(Decimal("0.01"))
        revenue = (total_kg * price).quantize(Decimal("0.01"))

        Harvest.objects.create(
            batch=batch,
            harvest_date=batch.start_date + timedelta(days=cycle_days),
            birds_harvested=harvested,
            total_weight_kg=total_kg,
            revenue=revenue,
            buyer_link=buyer,
            notes=f"Synthetic demo harvest ({quality} performance profile).",
            recorded_by=owner,
        )
        batch.status = Batch.Status.HARVESTED
        batch.save(update_fields=["status"])

    # ── report ──────────────────────────────────────────────

    def _report(self):
        from analytics.services import fcr_by_batch, profitability_by_batch

        farm = Farm.objects.get(name="Santos Broiler Farm")
        fcr = fcr_by_batch(farm)
        profit = profitability_by_batch(farm)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("─" * 68))
        self.stdout.write(self.style.SUCCESS("  SEEDED. Analytics preview:"))
        self.stdout.write(self.style.SUCCESS("─" * 68))

        for row in fcr["rows"]:
            self.stdout.write(
                f"  {row['batch_code']:<18} "
                f"FCR {row['fcr']:<7} "
                f"survival {row['survival_rate_pct']}%  "
                f"{row['total_weight_kg']}kg"
            )

        s = fcr["summary"]
        self.stdout.write("")
        self.stdout.write(f"  Average FCR : {s.get('average_fcr')}")
        self.stdout.write(f"  Best        : {s.get('best_batch')} @ {s.get('best_fcr')}")
        self.stdout.write(f"  Worst       : {s.get('worst_batch')} @ {s.get('worst_fcr')}")
        self.stdout.write("")
        self.stdout.write(f"  Revenue     : ₱{profit['summary']['total_revenue']}")
        self.stdout.write(f"  Feed cost   : ₱{profit['summary']['total_allocated_feed_cost']}")
        self.stdout.write(f"  Feed margin : ₱{profit['summary']['total_feed_margin']}")
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING("  Reminder: synthetic data. Say so if asked.")
        )
        self.stdout.write(self.style.SUCCESS("─" * 68))