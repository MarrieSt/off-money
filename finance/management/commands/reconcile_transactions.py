from datetime import date

from django.core.management.base import BaseCommand, CommandError

from money.services.reconciliation import reconcile_transactions


class Command(BaseCommand):
    help = "Reconcile Emma transactions from a full source snapshot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=("intraday", "daily", "full"),
            help="Standard reconciliation window.",
        )
        parser.add_argument("--start", help="Custom inclusive start date (YYYY-MM-DD).")
        parser.add_argument("--end", help="Custom inclusive end date (YYYY-MM-DD).")
        parser.add_argument("--dry-run", action="store_true", help="Compare without persisting changes.")
        parser.add_argument("--scheduled", action="store_true", help="Apply Europe/London schedule guards.")
        parser.add_argument("--verbose", action="store_true", help="Print per-run details.")

    def handle(self, *args, **options):
        mode = options["mode"]
        start_value = options["start"]
        end_value = options["end"]
        if bool(start_value) != bool(end_value):
            raise CommandError("Provide both --start and --end for a custom date range.")
        if mode and start_value:
            raise CommandError("Use either --mode or a custom --start/--end range.")
        if not mode and not start_value:
            raise CommandError("Provide --mode or both --start and --end.")
        if options["scheduled"] and not mode:
            raise CommandError("--scheduled requires a standard --mode.")

        start_date = end_date = None
        if start_value:
            try:
                start_date = date.fromisoformat(start_value)
                end_date = date.fromisoformat(end_value)
            except ValueError as exc:
                raise CommandError("Dates must use YYYY-MM-DD format.") from exc
            if start_date > end_date:
                raise CommandError("--start must not be after --end.")

        try:
            run = reconcile_transactions(
                start_date=start_date,
                end_date=end_date,
                full=mode == "full",
                mode=mode,
                scheduled=options["scheduled"],
                dry_run=options["dry_run"],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        if run is None:
            self.stdout.write("Schedule guard: this reconciliation is not due now.")
            return

        style = self.style.WARNING if run.status in {"partial", "skipped"} else self.style.SUCCESS
        self.stdout.write(
            style(
                "Reconciliation {status}: {rows_read} rows read, {created} created, "
                "{updated} updated, {unchanged} unchanged, {missing} missing, "
                "{restored} restored, {failed} failed{dry_run}.".format(
                    status=run.status,
                    rows_read=run.rows_read,
                    created=run.rows_created,
                    updated=run.rows_updated,
                    unchanged=run.transactions_unchanged,
                    missing=run.transactions_missing,
                    restored=run.transactions_restored,
                    failed=run.rows_failed,
                    dry_run=" (dry run; no data changes persisted)" if options["dry_run"] else "",
                )
            )
        )
        if options["verbose"]:
            self.stdout.write(
                "Run {pk}; mode={mode}; window={start}..{end}; duration={duration}ms".format(
                    pk=run.pk,
                    mode=run.job_type,
                    start=run.requested_start_date or "all history",
                    end=run.requested_end_date or "all history",
                    duration=run.duration_ms or 0,
                )
            )
