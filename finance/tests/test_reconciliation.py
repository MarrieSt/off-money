from contextlib import contextmanager
from datetime import date, datetime, timezone as datetime_timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from finance.models import EmmaRawTransaction, FinancialAccount, ImportRun, Transaction
from money.services.reconciliation import (
    get_reconciliation_window,
    london_now,
    reconcile_transactions,
    subtract_calendar_months,
)


@override_settings(MONEY_IMPORT_USERNAME="reconcile-owner", MONEY_DEFAULT_CURRENCY="GBP")
class ReconciliationServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="reconcile-owner", password="unused-password"
        )

    def source_row(self, source_id="emma-1", transaction_date="10/2/2026", **overrides):
        raw_data = {
            "ID": source_id,
            "Date": transaction_date,
            "Amount": "-12.34",
            "Account": "Everyday account",
            "Bank": "Example Bank",
            "Currency": "GBP",
            "Category": "Food",
            "Subcategory": "Cafe",
            "Type": "Purchase",
            "Tags": "",
            "Counterparty": "Cafe Example",
            "Custom Name": "Lunch",
            "Merchant": "Cafe Example",
            "Additional details": "Card transaction",
            "Notes": "",
            "Linked transaction ID": "",
        }
        raw_data.update(overrides)
        return {"source_row_number": 2, "raw_data": raw_data}

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def run_full(self, fetch_rows, rows):
        fetch_rows.return_value = rows
        return reconcile_transactions(full=True)

    def test_windows_use_calendar_day_and_month_arithmetic(self):
        today = date(2026, 10, 4)
        self.assertEqual(
            get_reconciliation_window(ImportRun.JobType.INTRADAY, today),
            (date(2026, 10, 1), today, False),
        )
        self.assertEqual(
            get_reconciliation_window(ImportRun.JobType.DAILY, today),
            (date(2026, 7, 4), today, False),
        )
        self.assertEqual(subtract_calendar_months(date(2026, 5, 31), 3), date(2026, 2, 28))
        self.assertEqual(get_reconciliation_window(ImportRun.JobType.FULL, today), (None, None, True))

    def test_london_clock_converts_utc_across_gmt_and_bst(self):
        winter = datetime(2026, 1, 1, 6, 0, tzinfo=datetime_timezone.utc)
        summer = datetime(2026, 7, 1, 5, 0, tzinfo=datetime_timezone.utc)
        self.assertEqual(london_now(winter).hour, 6)
        self.assertEqual(london_now(summer).hour, 6)

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_custom_window_reads_full_snapshot_but_writes_only_window_rows(self, fetch_rows):
        fetch_rows.return_value = [
            self.source_row("in-window", "10/2/2026"),
            self.source_row("outside-window", "9/30/2026"),
        ]

        run = reconcile_transactions(start_date=date(2026, 10, 1), end_date=date(2026, 10, 3))

        self.assertEqual(run.status, ImportRun.Status.SUCCESS)
        self.assertEqual(run.rows_read, 2)
        self.assertEqual(run.rows_created, 1)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(Transaction.objects.get().source_transaction_id, "in-window")

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_repeat_full_reconciliation_is_idempotent(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]
        first = reconcile_transactions(full=True)
        second = reconcile_transactions(full=True)

        self.assertEqual(first.rows_created, 1)
        self.assertEqual(second.rows_created, 0)
        self.assertEqual(second.rows_updated, 0)
        self.assertEqual(second.transactions_unchanged, 1)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(EmmaRawTransaction.objects.count(), 1)

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_reconciliation_preserves_application_owned_posted_date(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]
        reconcile_transactions(full=True)
        record = Transaction.objects.get()
        record.posted_date = date(2026, 10, 3)
        record.save(update_fields=["posted_date"])

        run = reconcile_transactions(full=True)

        record.refresh_from_db()
        self.assertEqual(run.transactions_unchanged, 1)
        self.assertEqual(record.posted_date, date(2026, 10, 3))

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_source_date_moved_outside_window_updates_existing_by_emma_id(self, fetch_rows):
        fetch_rows.return_value = [self.source_row(transaction_date="10/2/2026")]
        reconcile_transactions(start_date=date(2026, 10, 1), end_date=date(2026, 10, 3))
        fetch_rows.return_value = [self.source_row(transaction_date="10/5/2026")]

        run = reconcile_transactions(start_date=date(2026, 10, 1), end_date=date(2026, 10, 3))

        record = Transaction.objects.get()
        self.assertEqual(run.rows_updated, 1)
        self.assertEqual(record.transaction_date, date(2026, 10, 5))
        self.assertEqual(record.source_state, Transaction.SourceState.ACTIVE)
        self.assertEqual(Transaction.objects.count(), 1)

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_absent_transaction_is_marked_missing_and_later_restored(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]
        reconcile_transactions(full=True)
        fetch_rows.return_value = []

        missing_run = reconcile_transactions(full=True)

        record = Transaction.objects.get()
        self.assertEqual(missing_run.transactions_missing, 1)
        self.assertEqual(record.source_state, Transaction.SourceState.MISSING)

        fetch_rows.return_value = [self.source_row()]
        restored_run = reconcile_transactions(full=True)
        record.refresh_from_db()
        self.assertEqual(restored_run.transactions_restored, 1)
        self.assertEqual(record.source_state, Transaction.SourceState.ACTIVE)
        self.assertEqual(Transaction.objects.count(), 1)

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_bad_row_suppresses_missing_detection_but_preserves_raw_row(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]
        reconcile_transactions(full=True)
        bad_row = self.source_row(Amount="not-a-decimal")
        fetch_rows.return_value = [bad_row]

        run = reconcile_transactions(full=True)

        self.assertEqual(run.status, ImportRun.Status.PARTIAL)
        self.assertEqual(run.rows_failed, 1)
        self.assertIn("ImportRowError=1", run.error_summary)
        self.assertEqual(run.transactions_missing, 0)
        self.assertEqual(Transaction.objects.get().source_state, Transaction.SourceState.ACTIVE)
        self.assertEqual(EmmaRawTransaction.objects.get().raw_data["Amount"], "not-a-decimal")

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_duplicate_emma_ids_fail_rows_without_breaking_raw_uniqueness(self, fetch_rows):
        fetch_rows.return_value = [
            self.source_row("duplicate-id", "10/2/2026"),
            self.source_row("duplicate-id", "10/2/2026", Amount="-15.00"),
        ]

        run = reconcile_transactions(full=True)

        self.assertEqual(run.status, ImportRun.Status.PARTIAL)
        self.assertEqual(run.rows_failed, 2)
        self.assertEqual(run.rows_created, 0)
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(EmmaRawTransaction.objects.count(), 0)

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_dry_run_reports_changes_without_persisting_accounts_transactions_or_raw_rows(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]

        run = reconcile_transactions(full=True, dry_run=True)

        self.assertEqual(run.rows_created, 1)
        self.assertEqual(run.status, ImportRun.Status.SUCCESS)
        self.assertEqual(FinancialAccount.objects.count(), 0)
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(EmmaRawTransaction.objects.count(), 0)

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_source_read_failure_fails_run_without_marking_existing_rows_missing(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]
        reconcile_transactions(full=True)
        fetch_rows.side_effect = RuntimeError("Google API unavailable")

        with self.assertRaises(RuntimeError):
            reconcile_transactions(full=True)

        self.assertEqual(Transaction.objects.get().source_state, Transaction.SourceState.ACTIVE)
        self.assertEqual(ImportRun.objects.order_by("-started_at").first().status, ImportRun.Status.FAILED)

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_scheduled_daily_skips_on_london_day_three_but_manual_daily_runs(self, fetch_rows):
        now = datetime(2026, 10, 3, 3, 4, tzinfo=ZoneInfo("Europe/London"))
        fetch_rows.return_value = []

        skipped = reconcile_transactions(mode="daily", scheduled=True, now=now)

        self.assertEqual(skipped.status, ImportRun.Status.SKIPPED)
        fetch_rows.assert_not_called()

        manual = reconcile_transactions(mode="daily", scheduled=False, now=now)

        self.assertEqual(manual.status, ImportRun.Status.SUCCESS)
        fetch_rows.assert_called_once()
        self.assertEqual(manual.requested_start_date, date(2026, 7, 3))
        self.assertEqual(manual.requested_end_date, date(2026, 10, 3))

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_scheduled_intraday_accepts_railway_start_four_minutes_after_target(self, fetch_rows):
        now = datetime(2026, 10, 4, 15, 4, 11, tzinfo=ZoneInfo("Europe/London"))
        fetch_rows.return_value = []

        run = reconcile_transactions(mode="intraday", scheduled=True, now=now)

        self.assertEqual(run.status, ImportRun.Status.SUCCESS)
        fetch_rows.assert_called_once()

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_scheduled_intraday_skips_after_ten_minute_grace(self, fetch_rows):
        now = datetime(2026, 10, 4, 15, 11, tzinfo=ZoneInfo("Europe/London"))

        run = reconcile_transactions(mode="intraday", scheduled=True, now=now)

        self.assertIsNone(run)
        fetch_rows.assert_not_called()
        self.assertEqual(ImportRun.objects.count(), 0)

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_monthly_full_accepts_utc_candidate_at_0009_london(self, fetch_rows):
        now = datetime(2026, 10, 3, 0, 9, tzinfo=ZoneInfo("Europe/London"))
        fetch_rows.return_value = []

        run = reconcile_transactions(mode="full", scheduled=True, now=now)

        self.assertEqual(run.status, ImportRun.Status.SUCCESS)
        fetch_rows.assert_called_once()

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_locked_reconciliation_is_audited_as_skipped(self, fetch_rows):
        @contextmanager
        def lock_not_acquired():
            yield False

        fetch_rows.return_value = []
        with patch("money.services.reconciliation._advisory_lock", lock_not_acquired):
            run = reconcile_transactions(full=True)

        self.assertEqual(run.status, ImportRun.Status.SKIPPED)
        self.assertIn("already running", run.error_summary)
        fetch_rows.assert_not_called()

    @patch("money.services.reconciliation.fetch_worksheet_rows")
    def test_next_lock_owner_marks_abandoned_running_runs_failed(self, fetch_rows):
        abandoned = ImportRun.objects.create(
            source="emma",
            job_type=ImportRun.JobType.FULL,
            trigger_type=ImportRun.TriggerType.MANUAL_CLI,
            status=ImportRun.Status.RUNNING,
            started_at=timezone.now(),
        )
        fetch_rows.return_value = []

        reconcile_transactions(full=True)

        abandoned.refresh_from_db()
        self.assertEqual(abandoned.status, ImportRun.Status.FAILED)
        self.assertIn("process exited", abandoned.error_summary)
