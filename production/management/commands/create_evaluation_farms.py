# production/management/commands/create_evaluation_farms.py
"""
Create evaluation farms from the command line — the local twin of the
admin's System → Evaluation farms page (same service, same rules).

Unlike seed_demo_data this runs with DEBUG=False too: it only adds rows,
refuses taken session numbers, and never sends an SMS. The PINs are printed
once and are not stored anywhere in the clear.
"""
from django.core.management.base import BaseCommand, CommandError

from production.evaluation import (
    MAX_FARMS_PER_REQUEST,
    EvaluationFarmError,
    create_evaluation_farms,
)


class Command(BaseCommand):
    help = (
        "Create evaluation farms (Demo Farm – Session NN) on the next free "
        "session numbers, each with three weeks of sample records and four "
        "demo accounts. Prints the PINs once."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=1,
            help=f"How many farms to create (1-{MAX_FARMS_PER_REQUEST}). Default 1.",
        )

    def handle(self, *args, **options):
        try:
            results = create_evaluation_farms(options["count"])
        except EvaluationFarmError as exc:
            raise CommandError(str(exc)) from exc

        for result in results:
            self.stdout.write(self.style.SUCCESS(result.farm.name))
            for account in result.accounts:
                self.stdout.write(
                    f"  {account.role:<8} {account.full_name:<22} "
                    f"{account.phone_number}  PIN {account.pin}"
                )
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "Copy these accounts into the private session sheet now. "
                "The PINs are stored hashed and cannot be shown again."
            )
        )
