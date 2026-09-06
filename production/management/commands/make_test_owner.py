# production/management/commands/make_test_owner.py
"""
Creates a bare OWNER account with no farm and no membership, for manually
testing the onboarding wizard (FarmSetup.tsx / the /setup route).

There is no public sign-up in this system - a platform administrator
creates every account by hand. This command stands in for that
administrator during local testing only.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from accounts.services import issue_owner_account


class Command(BaseCommand):
    help = "Create a test OWNER with zero farms, to exercise the onboarding wizard."

    def add_arguments(self, parser):
        parser.add_argument(
            "--phone",
            default="+639170000001",
            help="E.164 phone number for the test owner. Default: +639170000001",
        )
        parser.add_argument(
            "--pin",
            default="123456",
            help="Initial PIN. Default: 123456",
        )
        parser.add_argument(
            "--name",
            default="Test Owner",
            help='Full name. Default: "Test Owner"',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to create a test account with DEBUG=False. "
                "This command is for local testing only."
            )

        phone = options["phone"]
        pin = options["pin"]
        name = options["name"]

        existing = User.objects.filter(phone_number=phone).first()
        if existing is not None:
            self.stdout.write(
                f"A user with {phone} already exists (id={existing.pk}). "
                "Deleting it so this command can be re-run cleanly."
            )
            existing.delete()

        # Same code path as the admin's "Create Farm Owner" flow and a real
        # invitation acceptance. --pin is honoured here only because this is a
        # DEBUG-gated local helper; production issuance is always random.
        user, pin = issue_owner_account(
            full_name=name,
            phone_number=phone,
            pin=pin,
        )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("-" * 60))
        self.stdout.write(self.style.SUCCESS("  TEST OWNER CREATED"))
        self.stdout.write(self.style.SUCCESS("-" * 60))
        self.stdout.write(f"  Name         : {user.full_name}")
        self.stdout.write(f"  Phone        : {phone}")
        self.stdout.write(f"  PIN          : {pin}")
        self.stdout.write(f"  Role         : {user.role}")
        self.stdout.write("  Farms        : none")
        self.stdout.write("  Memberships  : none")
        self.stdout.write(self.style.SUCCESS("-" * 60))
        self.stdout.write("")
        self.stdout.write(
            "Log in with the phone and PIN above. You will hit the PIN "
            "change gate first (must_change_credential=True); after "
            "rotating it, you will land on the /setup onboarding wizard, "
            "since this account has zero farm memberships."
        )
