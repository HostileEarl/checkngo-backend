# production/management/commands/lock_a_record.py
"""
Backdate a daily record so it crosses the 24-hour edit lock.

Local demo helper only. The seeded records are all recent enough to still
be editable by the worker; the manager-correction flow needs one that is
past the lock. This pushes a record's created_at into the past with
queryset.update() -- created_at is auto_now_add, so an ordinary save()
would not touch it.

ASCII output only: this is run against a cp1252 console.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from production.models import Batch, DailyRecord

EDIT_WINDOW_HOURS = DailyRecord.EDIT_WINDOW_HOURS  # 24


class Command(BaseCommand):
    help = (
        "Backdate the most recent daily record of an ACTIVE batch so it "
        "becomes locked (older than the 24-hour edit window). Local demo only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=30,
            help=(
                "How many hours into the past to set created_at. "
                "Default 30 (past the 24-hour lock)."
            ),
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Set created_at back to now for EVERY daily record in an "
                "active batch (this command does not track which records it "
                "previously backdated)."
            ),
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to run with DEBUG=False. "
                "This command is for local demonstration only."
            )

        now = timezone.now()
        active_batches = Batch.objects.filter(status=Batch.Status.ACTIVE)

        if not active_batches.exists():
            self.stdout.write("No ACTIVE batch found. Nothing to do.")
            return

        active_records = DailyRecord.objects.filter(batch__in=active_batches)

        if not active_records.exists():
            self.stdout.write(
                "No daily records found in any ACTIVE batch. Nothing to do."
            )
            return

        if options["reset"]:
            self._reset(active_records, active_batches, now)
            return

        self._backdate(active_records, options["hours"], now)

    # ------------------------------------------------------------------

    def _backdate(self, active_records, hours, now):
        record = active_records.select_related("batch").order_by(
            "-record_date", "-created_at"
        ).first()

        target = now - timedelta(hours=hours)

        # update(), not save(): created_at is auto_now_add and save() ignores it.
        DailyRecord.objects.filter(pk=record.pk).update(created_at=target)

        locked = hours >= EDIT_WINDOW_HOURS
        status_line = (
            "LOCKED (older than the %d-hour edit window)" % EDIT_WINDOW_HOURS
            if locked
            else "still editable (%d hours is inside the %d-hour window)"
            % (hours, EDIT_WINDOW_HOURS)
        )

        self.stdout.write("Backdated 1 daily record.")
        self.stdout.write("  Batch code:  %s" % record.batch.batch_code)
        self.stdout.write("  Record date: %s" % record.record_date.isoformat())
        self.stdout.write(
            "  created_at:  set to %s (%d hours ago)"
            % (target.isoformat(timespec="seconds"), hours)
        )
        self.stdout.write("  Status:      %s" % status_line)
        if locked:
            self.stdout.write("")
            self.stdout.write(
                "Open 'Daily records' in the UI, pick batch %s, and find the "
                "%s row. It will show a 'Locked' indicator and a 'Correct' "
                "button." % (record.batch.batch_code, record.record_date.isoformat())
            )

    def _reset(self, active_records, active_batches, now):
        record_count = active_records.count()
        batch_count = active_batches.count()

        active_records.update(created_at=now)

        self.stdout.write(
            "Reset created_at to now for %d daily record(s) across %d active "
            "batch(es)." % (record_count, batch_count)
        )
        self.stdout.write(
            "Note: --reset does not track which records this command previously "
            "backdated. It reset EVERY daily record in an active batch. All of "
            "them are now inside the %d-hour edit window." % EDIT_WINDOW_HOURS
        )
