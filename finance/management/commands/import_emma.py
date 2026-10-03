from django.core.management.base import BaseCommand, CommandError

from money.services.emma_import import run_emma_import


class Command(BaseCommand):
    help = "Import Emma transactions from the configured Google Sheet."

    def handle(self, *args, **options):
        try:
            run = run_emma_import()
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Emma import {status}: {rows_read} read, {rows_created} created, "
                "{rows_updated} updated, {rows_skipped} skipped, {rows_failed} failed.".format(
                    status=run.status,
                    rows_read=run.rows_read,
                    rows_created=run.rows_created,
                    rows_updated=run.rows_updated,
                    rows_skipped=run.rows_skipped,
                    rows_failed=run.rows_failed,
                )
            )
        )
