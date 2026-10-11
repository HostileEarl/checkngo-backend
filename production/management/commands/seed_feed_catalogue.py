# production/management/commands/seed_feed_catalogue.py
from django.core.management.base import BaseCommand, CommandError

from farms.models import Farm
from production.catalogue import STARTER_FEED_CATALOGUE, seed_starter_feed_catalogue


class Command(BaseCommand):
    help = (
        "Seed the LIMCOMA starter feed catalogue onto one farm (--farm) or "
        "every farm (--all). Opt-in and idempotent — a farm that already "
        "has one of these items by name is left alone, and this never runs "
        "automatically on farm creation (see production/catalogue.py for why)."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--farm", type=int, help="Farm id to seed.")
        group.add_argument(
            "--all", action="store_true", help="Seed every farm."
        )

    def handle(self, *args, **options):
        if options["all"]:
            farms = list(Farm.objects.all())
        else:
            try:
                farms = [Farm.objects.get(pk=options["farm"])]
            except Farm.DoesNotExist:
                raise CommandError(f"No farm with id {options['farm']}.")

        for farm in farms:
            created = seed_starter_feed_catalogue(farm)
            if created:
                names = ", ".join(i.name for i in created)
                self.stdout.write(
                    self.style.SUCCESS(f"{farm.name}: added {names}")
                )
            else:
                self.stdout.write(f"{farm.name}: already has the full catalogue")

        self.stdout.write(
            self.style.SUCCESS(
                f"Catalogue: {', '.join(i['name'] for i in STARTER_FEED_CATALOGUE)}"
            )
        )
