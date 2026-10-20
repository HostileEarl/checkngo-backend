# accounts/management/commands/expire_invitations.py
from django.core.management.base import BaseCommand

from accounts.models import Invitation


class Command(BaseCommand):
    """
    Mark every PENDING invitation past its expires_at as EXPIRED.

    Nothing else writes EXPIRED on a schedule: creating a new invitation
    expires only the overdue ones for that phone and farm, and accepting
    expires only the row being accepted. This sweep keeps the admin's
    stored status honest for the rest. In deployment it runs as a daily
    Render cron job (render.yaml); during development, by hand:

        python manage.py expire_invitations
    """

    help = "Mark overdue PENDING invitations as EXPIRED."

    def handle(self, *args, **options):
        count = Invitation.objects.expire_overdue()
        self.stdout.write(self.style.SUCCESS(f"Expired {count} invitation(s)."))
