from datetime import date
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


class ReconcileTransactionsCommandTests(SimpleTestCase):
    @patch("finance.management.commands.reconcile_transactions.reconcile_transactions")
    def test_custom_range_passes_dates_and_dry_run_to_shared_service(self, reconcile):
        reconcile.return_value = SimpleNamespace(
            status="success",
            rows_read=10,
            rows_created=2,
            rows_updated=1,
            transactions_unchanged=7,
            transactions_missing=0,
            transactions_restored=0,
            rows_failed=0,
            duration_ms=50,
            pk=1,
            job_type="manual",
            requested_start_date=date(2026, 9, 1),
            requested_end_date=date(2026, 9, 30),
        )
        output = StringIO()

        call_command(
            "reconcile_transactions",
            "--start=2026-09-01",
            "--end=2026-09-30",
            "--dry-run",
            stdout=output,
        )

        reconcile.assert_called_once_with(
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
            full=False,
            mode=None,
            scheduled=False,
            dry_run=True,
        )
        self.assertIn("2 created", output.getvalue())
        self.assertIn("dry run", output.getvalue())

    @patch("finance.management.commands.reconcile_transactions.reconcile_transactions")
    def test_scheduled_mode_passes_schedule_flag(self, reconcile):
        reconcile.return_value = None
        output = StringIO()

        call_command(
            "reconcile_transactions",
            "--mode=daily",
            "--scheduled",
            stdout=output,
        )

        reconcile.assert_called_once_with(
            start_date=None,
            end_date=None,
            full=False,
            mode="daily",
            scheduled=True,
            dry_run=False,
        )
        self.assertIn("not due now", output.getvalue())

    def test_date_range_requires_both_endpoints(self):
        with self.assertRaises(CommandError):
            call_command("reconcile_transactions", "--start=2026-09-01", stdout=StringIO())

    def test_rejects_mode_combined_with_custom_range(self):
        with self.assertRaises(CommandError):
            call_command(
                "reconcile_transactions",
                "--mode=full",
                "--start=2026-09-01",
                "--end=2026-09-30",
                stdout=StringIO(),
            )
