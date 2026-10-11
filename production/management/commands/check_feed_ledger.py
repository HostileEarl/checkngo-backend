# production/management/commands/check_feed_ledger.py
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from farms.models import Farm
from production.models import FeedDelivery, InventoryItem


class Command(BaseCommand):
    """
    Verify that every feed item's stock-in history is backed by deliveries.

    Read-only: never writes, never deletes, never corrects. A feed item's
    stock enters only through record_feed_delivery()'s dual write (see
    production/models.py), so Σ(stock-in count) should equal Σ(delivery
    count) for that item, and Σ(stock-in quantity) should equal Σ(delivery
    quantity, converted into the item's own unit via kg_per_unit). Count
    equality alone would miss a delivery whose dual-written stock-in
    quantity silently diverged from it — both are checked, and either one
    disagreeing is reported.

    A delivery against an item with no kg_per_unit set (e.g. CBM, seeded
    without a sack weight) can't be converted to a unit count at all; that
    is reported separately rather than folded into the pass/fail quantity
    check, since "uncomparable" and "wrong" are not the same finding.
    """

    help = (
        "Print the feed ledger invariant (stock-ins vs. deliveries, by count "
        "and by quantity) for one farm or all farms, and exit non-zero if any "
        "feed item's stock is not fully backed by deliveries."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--farm", type=int, help="Limit to one farm id. Default: every farm."
        )

    def handle(self, *args, **options):
        items = InventoryItem.objects.filter(is_feed=True).select_related("farm")
        if options["farm"]:
            try:
                Farm.objects.get(pk=options["farm"])
            except Farm.DoesNotExist:
                raise CommandError(f"No farm with id {options['farm']}.")
            items = items.filter(farm_id=options["farm"])

        header = (
            f"{'farm':25} {'item':30} {'stock_ins':>9} {'deliveries':>10} "
            f"{'stock qty':>10} {'deliv qty':>10} {'flag':>20}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        violations = 0
        for item in items.order_by("farm_id", "name"):
            stock_in_count = item.stock_ins.count()
            stock_in_qty = item.stocked_in

            deliveries = FeedDelivery.objects.filter(inventory_item=item)
            delivery_count = deliveries.count()

            if item.kg_per_unit:
                delivery_qty = sum(
                    (d.quantity_kg / item.kg_per_unit).quantize(Decimal("0.01"))
                    for d in deliveries
                ) or Decimal("0")
                qty_mismatch = stock_in_qty != delivery_qty
                delivery_qty_display = f"{delivery_qty:>10}"
            else:
                # No sack weight set — kg can't be converted to a unit
                # count, so the quantity side of this item is uncomparable,
                # not necessarily wrong.
                delivery_qty = None
                qty_mismatch = False
                delivery_qty_display = f"{'n/a':>10}"

            count_mismatch = stock_in_count != delivery_count

            flag = ""
            if count_mismatch and stock_in_count > 0 and delivery_count == 0:
                flag = "STOCK, NO DELIVERIES"
            elif count_mismatch:
                flag = "COUNT MISMATCH"
            elif qty_mismatch:
                flag = "QUANTITY MISMATCH"
            elif delivery_qty is None and deliveries.exists():
                flag = "uncomparable (no kg_per_unit)"

            if count_mismatch or qty_mismatch:
                violations += 1

            self.stdout.write(
                f"{item.farm.name[:25]:25} {item.name[:30]:30} "
                f"{stock_in_count:>9} {delivery_count:>10} "
                f"{stock_in_qty:>10}{delivery_qty_display} {flag:>20}"
            )

        self.stdout.write("")
        if violations:
            self.stderr.write(
                self.style.ERROR(
                    f"{violations} feed item(s) with stock not fully backed "
                    "by deliveries."
                )
            )
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Feed ledger is consistent."))
