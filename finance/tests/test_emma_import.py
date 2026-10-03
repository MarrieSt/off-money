import json
from datetime import date
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from finance.models import EmmaRawTransaction, FinancialAccount, ImportRun, Transaction
from money.services.emma_import import (
    ImportConfigurationError,
    hash_row,
    normalize_row,
    parse_amount,
    parse_transaction_date,
    run_emma_import,
)
from money.services.google_sheets import (
    GoogleSheetsConfigurationError,
    READ_ONLY_SCOPE,
    fetch_worksheet_rows,
)


class EmmaRowNormalizationTests(SimpleTestCase):
    def test_normalize_trims_whitespace_and_normalizes_nulls_dates_and_amounts(self):
        normalized = normalize_row(
            {
                "Date": " 03/01/2026 ",
                "Amount": " £1,000.00 ",
                "Notes": "  lunch  ",
                "Tags": "   ",
            }
        )
        self.assertEqual(
            normalized,
            {"Date": "2026-01-03", "Amount": "1000", "Notes": "lunch", "Tags": None},
        )

    def test_hash_is_stable_for_equivalent_row_values_and_key_order(self):
        first = {"ID": " emma-1 ", "Date": "03/01/2026", "Amount": "£1,000.00"}
        second = {"Amount": "1000", "Date": "2026-01-03", "ID": "emma-1"}
        self.assertEqual(hash_row(first), hash_row(second))

    def test_amount_parser_uses_decimal_and_accepts_grouping(self):
        self.assertEqual(parse_amount("-£1,234.50"), Decimal("-1234.50"))

    def test_date_parser_accepts_iso_and_uk_dates(self):
        self.assertEqual(parse_transaction_date("2026-01-03"), date(2026, 1, 3))
        self.assertEqual(parse_transaction_date("03/01/2026"), date(2026, 1, 3))


@override_settings(
    GOOGLE_SHEETS_CREDENTIALS_JSON='{"type":"service_account"}',
    EMMA_SPREADSHEET_ID="sheet-id",
    EMMA_WORKSHEET_NAME="Primary",
)
class GoogleSheetsAdapterTests(SimpleTestCase):
    @patch("money.services.google_sheets.gspread.service_account_from_dict")
    def test_reads_dictionary_rows_with_only_read_only_scope(self, service_account):
        worksheet = Mock()
        worksheet.get_all_values.return_value = [
            ["ID", "Date", "Amount"],
            ["emma-1", "2026-01-03", "-12.34"],
            ["", "", ""],
        ]
        spreadsheet = Mock()
        spreadsheet.worksheet.return_value = worksheet
        client = Mock()
        client.open_by_key.return_value = spreadsheet
        service_account.return_value = client

        rows = fetch_worksheet_rows()

        service_account.assert_called_once_with(
            {"type": "service_account"}, scopes=[READ_ONLY_SCOPE]
        )
        self.assertEqual(
            rows,
            [
                {
                    "source_row_number": 2,
                    "raw_data": {"ID": "emma-1", "Date": "2026-01-03", "Amount": "-12.34"},
                }
            ],
        )

    @override_settings(GOOGLE_SHEETS_CREDENTIALS_JSON="")
    def test_missing_google_configuration_is_reported_without_authentication(self):
        with patch("money.services.google_sheets.gspread.service_account_from_dict") as auth:
            with self.assertRaises(GoogleSheetsConfigurationError):
                fetch_worksheet_rows()
        auth.assert_not_called()


@override_settings(MONEY_IMPORT_USERNAME="emma-owner", MONEY_DEFAULT_CURRENCY="GBP")
class EmmaImportServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="emma-owner", password="unused-password"
        )
        self.account = FinancialAccount.objects.create(
            user=self.user,
            source_system="emma",
            source_name="Current account",
            institution_name="Example Bank",
        )

    def source_row(self, **overrides):
        raw_data = {
            "ID": "emma-transaction-1",
            "Date": "03/01/2026",
            "Amount": "-12.34",
            "Account": "Current account",
            "Bank": "Example Bank",
            "Currency": "GBP",
            "Category": "Eating out",
            "Subcategory": "Restaurant",
            "Type": "Card payment",
            "Tags": "personal, lunch",
            "Counterparty": "Cafe Example",
            "Custom Name": "Lunch",
            "Merchant": "Cafe Example",
            "Additional details": "Card purchase",
            "Notes": "Team lunch",
            "Linked transaction ID": "",
        }
        raw_data.update(overrides)
        return {"source_row_number": 2, "raw_data": raw_data}

    @patch("money.services.emma_import.fetch_worksheet_rows")
    def test_import_creates_raw_and_canonical_transaction(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]

        run = run_emma_import()

        transaction_record = Transaction.objects.get()
        raw_record = EmmaRawTransaction.objects.get()
        self.assertEqual(run.status, ImportRun.Status.SUCCESS)
        self.assertEqual((run.rows_read, run.rows_created), (1, 1))
        self.assertEqual(transaction_record.source_transaction_id, "emma-transaction-1")
        self.assertEqual(transaction_record.amount, Decimal("-12.34"))
        self.assertEqual(transaction_record.transaction_date, date(2026, 1, 3))
        self.assertEqual(transaction_record.account, self.account)
        self.assertEqual(transaction_record.description, "Lunch")
        self.assertEqual(transaction_record.merchant_name, "Cafe Example")
        self.assertEqual(transaction_record.source_category, "Eating out")
        self.assertEqual(transaction_record.raw_data, raw_record.raw_data)
        self.assertEqual(raw_record.import_run, run)
        self.assertEqual(len(raw_record.source_hash), 64)

    @patch("money.services.emma_import.fetch_worksheet_rows")
    def test_repeated_import_is_idempotent_and_updates_last_seen(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]
        first_run = run_emma_import()
        previous_last_seen = EmmaRawTransaction.objects.get().last_seen_at

        second_run = run_emma_import()

        self.assertEqual(first_run.rows_created, 1)
        self.assertEqual(second_run.status, ImportRun.Status.SUCCESS)
        self.assertEqual(second_run.rows_skipped, 1)
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(EmmaRawTransaction.objects.count(), 1)
        raw_record = EmmaRawTransaction.objects.get()
        self.assertEqual(raw_record.import_run, second_run)
        self.assertGreaterEqual(raw_record.last_seen_at, previous_last_seen)

    @patch("money.services.emma_import.fetch_worksheet_rows")
    def test_changed_source_row_updates_existing_transaction(self, fetch_rows):
        fetch_rows.return_value = [self.source_row()]
        run_emma_import()
        fetch_rows.return_value = [
            self.source_row(**{"Amount": "-18.50", "Merchant": "Cafe Example Ltd"})
        ]

        run = run_emma_import()

        self.assertEqual(run.rows_updated, 1)
        self.assertEqual(Transaction.objects.count(), 1)
        transaction_record = Transaction.objects.get()
        self.assertEqual(transaction_record.amount, Decimal("-18.50"))
        self.assertEqual(transaction_record.merchant_name, "Cafe Example Ltd")
        self.assertEqual(EmmaRawTransaction.objects.get().raw_data["Amount"], "-18.50")

    @patch("money.services.emma_import.fetch_worksheet_rows")
    def test_import_discovers_accounts_and_bad_rows_do_not_stop_the_run(self, fetch_rows):
        missing_id = self.source_row()
        missing_id["raw_data"]["ID"] = "  "
        unmapped_account = self.source_row(ID="emma-unknown-account", Account="Unknown")
        unmapped_account["raw_data"]["Bank"] = "New Bank"
        unmapped_account["raw_data"]["Currency"] = "EUR"
        malformed_amount = self.source_row(ID="emma-bad-amount", Amount="not-money")
        fetch_rows.return_value = [self.source_row(), missing_id, unmapped_account, malformed_amount]

        run = run_emma_import()

        self.assertEqual(run.status, ImportRun.Status.PARTIAL)
        self.assertEqual(run.rows_read, 4)
        self.assertEqual(run.rows_created, 2)
        self.assertEqual(run.rows_failed, 2)
        self.assertEqual(Transaction.objects.count(), 2)
        self.assertEqual(EmmaRawTransaction.objects.count(), 3)
        discovered_account = FinancialAccount.objects.get(source_name="Unknown")
        self.assertEqual(discovered_account.institution_name, "New Bank")
        self.assertEqual(discovered_account.currency, "GBP")
        self.assertEqual(
            Transaction.objects.get(source_transaction_id="emma-unknown-account").currency,
            "EUR",
        )

    @patch("money.services.emma_import.fetch_worksheet_rows")
    def test_importer_retries_canonical_write_after_a_previous_row_failure(self, fetch_rows):
        invalid = self.source_row(**{"Amount": "not-money"})
        fetch_rows.return_value = [invalid]
        failed_run = run_emma_import()
        self.assertEqual(failed_run.rows_failed, 1)
        self.assertEqual(EmmaRawTransaction.objects.count(), 1)
        self.assertEqual(Transaction.objects.count(), 0)

        fetch_rows.return_value = [self.source_row()]
        retry_run = run_emma_import()

        self.assertEqual(retry_run.rows_created, 1)
        self.assertEqual(Transaction.objects.count(), 1)

    @override_settings(MONEY_IMPORT_USERNAME="")
    @patch("money.services.emma_import.fetch_worksheet_rows")
    def test_missing_import_user_fails_and_records_import_run(self, fetch_rows):
        with self.assertRaises(ImportConfigurationError):
            run_emma_import()
        fetch_rows.assert_not_called()
        run = ImportRun.objects.get()
        self.assertEqual(run.status, ImportRun.Status.FAILED)
        self.assertIsNotNone(run.finished_at)


class EmmaImportOperationsTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="admin", password="admin-password", email="admin@example.com"
        )

    @patch("finance.admin.run_emma_import")
    def test_admin_button_calls_the_shared_import_service(self, run_import):
        run_import.return_value = SimpleNamespace(
            status="success",
            rows_created=2,
            rows_updated=1,
            rows_skipped=3,
            rows_failed=0,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse("admin:finance_importrun_run"))

        self.assertRedirects(
            response,
            reverse("admin:finance_importrun_changelist"),
            fetch_redirect_response=False,
        )
        run_import.assert_called_once_with()

    @patch("finance.admin.run_emma_import")
    def test_admin_import_action_rejects_get_requests(self, run_import):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("admin:finance_importrun_run"))
        self.assertEqual(response.status_code, 405)
        run_import.assert_not_called()

    @patch("finance.management.commands.import_emma.run_emma_import")
    def test_management_command_prints_import_summary(self, run_import):
        run_import.return_value = SimpleNamespace(
            status="partial",
            rows_read=4,
            rows_created=1,
            rows_updated=1,
            rows_skipped=1,
            rows_failed=1,
        )
        output = StringIO()

        call_command("import_emma", stdout=output)

        self.assertIn("4 read, 1 created, 1 updated, 1 skipped, 1 failed", output.getvalue())

    @patch("finance.management.commands.import_emma.run_emma_import")
    def test_management_command_returns_error_for_fatal_import(self, run_import):
        run_import.side_effect = RuntimeError("Import configuration unavailable")
        with self.assertRaises(CommandError):
            call_command("import_emma", stdout=StringIO(), stderr=StringIO())
