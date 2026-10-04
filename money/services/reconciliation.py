import logging
from calendar import monthrange
from collections import Counter
from contextlib import contextmanager
from datetime import date, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import connection, transaction as db_transaction
from django.utils import timezone

from finance.models import EmmaRawTransaction, FinancialAccount, ImportRun, Transaction
from money.services.emma_import import (
    ImportRowError,
    _account_for_row,
    _field_map,
    _resolve_import_user,
    _transaction_defaults,
    _value,
    hash_row,
    parse_transaction_date,
)
from money.services.google_sheets import fetch_worksheet_rows


logger = logging.getLogger(__name__)
LONDON = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")
LOCK_KEY = 0x4D4F4E4559
BATCH_SIZE = 1000
INTRADAY_UTC_HOURS = {6, 9, 12, 15, 18, 21, 22}
SCHEDULE_GRACE_SECONDS = 10 * 60
SOURCE_CONTROLLED_FIELDS = (
    "account",
    "transaction_date",
    "description",
    "merchant_name",
    "amount",
    "currency",
    "source_system",
    "source_content_hash",
    "source_category",
    "source_subcategory",
    "source_type",
    "source_tags",
    "source_counterparty",
    "source_custom_name",
    "source_merchant",
    "source_additional_details",
    "source_notes",
    "source_linked_transaction_id",
    "raw_data",
)


class ReconciliationError(RuntimeError):
    pass


def london_now(now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        return timezone.make_aware(now, LONDON)
    return timezone.localtime(now, LONDON)


def subtract_calendar_months(value, months):
    month_index = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def get_reconciliation_window(mode, today):
    if mode == ImportRun.JobType.INTRADAY:
        return today - timedelta(days=3), today, False
    if mode == ImportRun.JobType.DAILY:
        return subtract_calendar_months(today, 3), today, False
    if mode == ImportRun.JobType.FULL:
        return None, None, True
    raise ValueError("mode must be intraday, daily, or full.")


def _within_schedule_grace(now, target_hour):
    seconds_after_hour = now.minute * 60 + now.second
    return now.hour == target_hour and seconds_after_hour <= SCHEDULE_GRACE_SECONDS


def _scheduled_skip_reason(mode, now):
    if mode == ImportRun.JobType.INTRADAY:
        utc_now = now.astimezone(UTC)
        if any(_within_schedule_grace(utc_now, hour) for hour in INTRADAY_UTC_HOURS):
            return None
        return "Intraday reconciliation is only due at configured UTC candidate hours (with a ten-minute grace)."
    if mode == ImportRun.JobType.DAILY:
        if now.day == 3:
            return "Daily reconciliation is superseded by monthly full reconciliation on day 3."
        if _within_schedule_grace(now, 3):
            return None
        return "Not the scheduled daily reconciliation time in Europe/London."
    if mode == ImportRun.JobType.FULL:
        if now.day == 3 and _within_schedule_grace(now, 0):
            return None
        return "Not the scheduled monthly full reconciliation time in Europe/London."
    return "Scheduled reconciliation requires a standard mode."


@contextmanager
def _advisory_lock():
    if connection.vendor != "postgresql":
        raise ReconciliationError("Reconciliation requires PostgreSQL advisory locks.")
    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
            acquired = cursor.fetchone()[0]
        yield acquired
    finally:
        if acquired and connection.connection is not None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])


def _finish_run(run, status, error_summary="", started_clock=None):
    finished_at = timezone.now()
    run.status = status
    run.finished_at = finished_at
    run.error_summary = error_summary
    run.error_message = error_summary
    if started_clock is not None:
        run.duration_ms = max(0, int((monotonic() - started_clock) * 1000))
    run.save(
        update_fields=(
            "status",
            "finished_at",
            "error_summary",
            "error_message",
            "duration_ms",
        )
    )
    return run


def _create_run(mode, trigger_type, start_date, end_date, user=None):
    return ImportRun.objects.create(
        source=Transaction.SOURCE_EMMA,
        job_type=mode,
        trigger_type=trigger_type,
        user=user,
        status=ImportRun.Status.RUNNING,
        started_at=timezone.now(),
        requested_start_date=start_date,
        requested_end_date=end_date,
    )


def _fail_abandoned_runs(current_run):
    summary = "Previous reconciliation process exited before recording a final status."
    ImportRun.objects.filter(
        source=Transaction.SOURCE_EMMA,
        status=ImportRun.Status.RUNNING,
    ).exclude(pk=current_run.pk).update(
        status=ImportRun.Status.FAILED,
        finished_at=timezone.now(),
        error_summary=summary,
        error_message=summary,
    )


def _chunked(values, size=BATCH_SIZE):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _existing_transactions(user, source_ids):
    records = []
    for chunk in _chunked(source_ids):
        records.extend(
            Transaction.objects.filter(
                user=user,
                source_system=Transaction.SOURCE_EMMA,
                source_transaction_id__in=chunk,
            ).select_related("account")
        )
    return {record.source_transaction_id: record for record in records}


def _existing_raw_rows(user, source_ids):
    records = []
    for chunk in _chunked(source_ids):
        records.extend(
            EmmaRawTransaction.objects.filter(
                user=user,
                source_system=Transaction.SOURCE_EMMA,
                source_transaction_id__in=chunk,
            )
        )
    return {record.source_transaction_id: record for record in records}


def _account_for_fields(user, fields, dry_run):
    name = _value(fields, "Account")
    bank = _value(fields, "Bank") or ""
    if not name:
        raise ImportRowError("Account name is missing.")
    if not dry_run:
        return _account_for_row(user, fields)
    return FinancialAccount.objects.filter(
        user=user,
        source_system=Transaction.SOURCE_EMMA,
        source_name=name,
        institution_name=bank,
    ).first()


def _matches_source(existing, defaults):
    if existing is None:
        return False
    for field in SOURCE_CONTROLLED_FIELDS:
        expected = defaults[field]
        current = getattr(existing, f"{field}_id" if field == "account" else field)
        if field == "account":
            expected = expected.pk if expected is not None else None
        if current != expected:
            return False
    return True


def _prepare_rows(rows, start_date, end_date, full, local_window_ids):
    prepared = []
    source_ids = set()
    source_id_counts = Counter()
    for row in rows:
        fields = _field_map(row["raw_data"])
        source_id = _value(fields, "ID")
        if source_id:
            source_id = str(source_id)
            source_ids.add(source_id)
            source_id_counts[source_id] += 1
        try:
            transaction_date = parse_transaction_date(
                _value(fields, "Date"), source_system=Transaction.SOURCE_EMMA
            )
            date_error = None
        except ImportRowError as exc:
            transaction_date = None
            date_error = exc

        relevant = (
            full
            or source_id in local_window_ids
            or (transaction_date is not None and start_date <= transaction_date <= end_date)
            or date_error is not None
        )
        if relevant:
            prepared.append(
                {
                    "row": row,
                    "fields": fields,
                    "source_id": source_id,
                    "transaction_date": transaction_date,
                    "date_error": date_error,
                }
            )
    relevant_ids = {item["source_id"] for item in prepared if item["source_id"] is not None}
    duplicate_ids = {
        source_id
        for source_id in relevant_ids
        if source_id_counts[source_id] > 1
    }
    return prepared, source_ids, duplicate_ids


def _source_defaults(user, account, fields, raw_data, source_hash, now, existing):
    defaults = _transaction_defaults(user, account, fields, raw_data, source_hash, now, existing)
    defaults["source_state"] = Transaction.SourceState.ACTIVE
    return defaults


def _make_raw_record(user, run, source_id, source_row_number, source_hash, raw_data, now, existing):
    if existing is None:
        return EmmaRawTransaction(
            user=user,
            import_run=run,
            source_system=Transaction.SOURCE_EMMA,
            source_transaction_id=source_id,
            source_row_number=source_row_number,
            source_hash=source_hash,
            raw_data=raw_data,
            first_seen_at=now,
            last_seen_at=now,
        ), True
    existing.import_run = run
    existing.source_row_number = source_row_number
    existing.source_hash = source_hash
    existing.raw_data = raw_data
    existing.last_seen_at = now
    existing.updated_at = now
    return existing, False


def _write_in_batches(
    new_transactions,
    changed_transactions,
    unchanged_transactions,
    new_raw,
    changed_raw,
    missing_transactions,
):
    with db_transaction.atomic():
        for chunk in _chunked(new_transactions):
            Transaction.objects.bulk_create(chunk, batch_size=BATCH_SIZE)
        update_fields = list(SOURCE_CONTROLLED_FIELDS) + [
            "source_state",
            "last_source_sync_at",
            "updated_at",
        ]
        for chunk in _chunked(changed_transactions):
            Transaction.objects.bulk_update(chunk, update_fields, batch_size=BATCH_SIZE)
        for chunk in _chunked(unchanged_transactions):
            Transaction.objects.bulk_update(chunk, ["last_source_sync_at"], batch_size=BATCH_SIZE)
        for chunk in _chunked(new_raw):
            EmmaRawTransaction.objects.bulk_create(chunk, batch_size=BATCH_SIZE)
        for chunk in _chunked(changed_raw):
            EmmaRawTransaction.objects.bulk_update(
                chunk,
                [
                    "import_run",
                    "source_row_number",
                    "source_hash",
                    "raw_data",
                    "last_seen_at",
                    "updated_at",
                ],
                batch_size=BATCH_SIZE,
            )
        for chunk in _chunked(missing_transactions):
            Transaction.objects.bulk_update(chunk, ["source_state", "updated_at"], batch_size=BATCH_SIZE)


def _process_snapshot(run, user, rows, start_date, end_date, full, dry_run):
    local_window = Transaction.objects.filter(
        user=user,
        source_system=Transaction.SOURCE_EMMA,
    )
    if not full:
        local_window = local_window.filter(transaction_date__range=(start_date, end_date))
    local_window_records = list(local_window)
    local_window_by_id = {
        record.source_transaction_id: record
        for record in local_window_records
        if record.source_transaction_id
    }
    local_window_ids = set(local_window_by_id)

    prepared, snapshot_ids, duplicate_ids = _prepare_rows(
        rows, start_date, end_date, full, local_window_ids
    )
    relevant_ids = {
        item["source_id"]
        for item in prepared
        if item["source_id"] is not None and item["source_id"] not in duplicate_ids
    }
    existing_by_id = _existing_transactions(user, relevant_ids)
    existing_by_id.update(local_window_by_id)
    raw_by_id = _existing_raw_rows(user, relevant_ids)

    new_transactions = []
    changed_transactions = []
    unchanged_transactions = []
    new_raw = []
    changed_raw = []
    failed_count = 0
    created_count = 0
    updated_count = 0
    unchanged_count = 0
    restored_count = 0
    failure_kinds = Counter()
    created_accounts = {}
    now = timezone.now()

    for item in prepared:
        row = item["row"]
        source_id = item["source_id"]
        fields = item["fields"]
        if source_id is None:
            failed_count += 1
            failure_kinds["missing_id"] += 1
            logger.warning(
                "Reconciliation %s failed at source row %s (missing transaction ID)",
                run.pk,
                row.get("source_row_number", "unknown"),
            )
            continue
        if source_id in duplicate_ids:
            failed_count += 1
            failure_kinds["duplicate_id"] += 1
            logger.warning(
                "Reconciliation %s failed at source row %s (duplicate transaction ID)",
                run.pk,
                row.get("source_row_number", "unknown"),
            )
            continue

        try:
            source_hash = hash_row(row["raw_data"], source_system=Transaction.SOURCE_EMMA)
            raw_record, raw_created = _make_raw_record(
                user,
                run,
                source_id,
                row["source_row_number"],
                source_hash,
                row["raw_data"],
                now,
                raw_by_id.get(source_id),
            )
            if raw_created:
                new_raw.append(raw_record)
            else:
                changed_raw.append(raw_record)
        except Exception as exc:
            failed_count += 1
            failure_kinds[type(exc).__name__] += 1
            logger.warning(
                "Reconciliation %s failed at source row %s (%s)",
                run.pk,
                row.get("source_row_number", "unknown"),
                type(exc).__name__,
            )
            continue

        if item["date_error"] is not None:
            failed_count += 1
            failure_kinds[type(item["date_error"]).__name__] += 1
            logger.warning(
                "Reconciliation %s failed at source row %s (%s)",
                run.pk,
                row.get("source_row_number", "unknown"),
                type(item["date_error"]).__name__,
            )
            continue

        try:
            existing = existing_by_id.get(source_id)
            account_key = (_value(fields, "Account"), _value(fields, "Bank") or "")
            account = (
                _account_for_fields(user, fields, dry_run=True)
                if dry_run
                else None
            )
            defaults = _source_defaults(
                user, account, fields, row["raw_data"], source_hash, now, existing
            )
            probe = Transaction(
                user=user,
                source_transaction_id=source_id,
                **defaults,
            )
            probe.full_clean(
                exclude=["account"],
                validate_unique=False,
                validate_constraints=False,
            )
            if not dry_run and account_key not in created_accounts:
                created_accounts[account_key] = _account_for_fields(user, fields, dry_run=False)
            if not dry_run:
                account = created_accounts[account_key]
                defaults["account"] = account
        except Exception as exc:
            failed_count += 1
            failure_kinds[type(exc).__name__] += 1
            logger.warning(
                "Reconciliation %s failed at source row %s (%s)",
                run.pk,
                row.get("source_row_number", "unknown"),
                type(exc).__name__,
            )
            continue

        if existing is None:
            created_count += 1
            if not dry_run:
                new_transactions.append(
                    Transaction(
                        user=user,
                        source_transaction_id=source_id,
                        **defaults,
                    )
                )
            continue

        was_missing = existing.source_state == Transaction.SourceState.MISSING
        source_matches = _matches_source(existing, defaults)
        existing.last_source_sync_at = now
        if was_missing:
            restored_count += 1
            existing.source_state = Transaction.SourceState.ACTIVE
            existing.updated_at = now
            if not dry_run:
                for field in SOURCE_CONTROLLED_FIELDS:
                    setattr(existing, field, defaults[field])
                changed_transactions.append(existing)
        elif source_matches:
            unchanged_count += 1
            if not dry_run:
                unchanged_transactions.append(existing)
        else:
            updated_count += 1
            existing.source_state = Transaction.SourceState.ACTIVE
            existing.updated_at = now
            if not dry_run:
                for field in SOURCE_CONTROLLED_FIELDS:
                    setattr(existing, field, defaults[field])
                changed_transactions.append(existing)

    missing_transactions = []
    missing_count = 0
    missing_detection_safe = failed_count == 0
    if missing_detection_safe:
        for record in local_window_records:
            if record.source_transaction_id not in snapshot_ids and record.source_state != Transaction.SourceState.MISSING:
                missing_count += 1
                if not dry_run:
                    record.source_state = Transaction.SourceState.MISSING
                    record.updated_at = now
                    missing_transactions.append(record)

    if not dry_run:
        _write_in_batches(
            new_transactions,
            changed_transactions,
            unchanged_transactions,
            new_raw,
            changed_raw,
            missing_transactions,
        )

    return {
        "rows_failed": failed_count,
        "transactions_created": created_count,
        "transactions_updated": updated_count,
        "transactions_unchanged": unchanged_count,
        "transactions_missing": missing_count,
        "transactions_restored": restored_count,
        "error_summary": "Row failures: " + ", ".join(
            f"{kind}={count}" for kind, count in sorted(failure_kinds.items())
        ) if failure_kinds else "",
    }


def reconcile_transactions(
    start_date=None,
    end_date=None,
    full=False,
    *,
    mode=None,
    scheduled=False,
    dry_run=False,
    now=None,
):
    """Reconcile Emma's complete sheet snapshot against one requested date window."""
    local_now = london_now(now)
    today = local_now.date()
    trigger_type = ImportRun.TriggerType.SCHEDULED if scheduled else ImportRun.TriggerType.MANUAL_CLI

    if mode is not None:
        if mode not in {
            ImportRun.JobType.INTRADAY,
            ImportRun.JobType.DAILY,
            ImportRun.JobType.FULL,
        }:
            raise ValueError("mode must be intraday, daily, or full.")
        if start_date is not None or end_date is not None or full:
            raise ValueError("Use either a standard mode or an explicit date range.")
        if scheduled:
            skip_reason = _scheduled_skip_reason(mode, local_now)
            if skip_reason:
                if mode == ImportRun.JobType.DAILY and local_now.day == 3:
                    skipped_run = _create_run(mode, trigger_type, None, None)
                    return _finish_run(skipped_run, ImportRun.Status.SKIPPED, skip_reason)
                logger.info("reconciliation_schedule_not_due mode=%s london_time=%s", mode, local_now.isoformat())
                return None
        start_date, end_date, full = get_reconciliation_window(mode, today)
    elif full:
        mode = ImportRun.JobType.FULL
        start_date = end_date = None
    else:
        if start_date is None or end_date is None:
            raise ValueError("Provide both start_date and end_date, or use a standard mode.")
        mode = ImportRun.JobType.MANUAL
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date.")

    if not full and (start_date is None or end_date is None):
        raise ValueError("A non-full reconciliation requires a start and end date.")

    run = _create_run(mode, trigger_type, start_date, end_date)
    started_clock = monotonic()
    logger.info(
        "reconciliation_started mode=%s start=%s end=%s full=%s dry_run=%s",
        mode,
        start_date,
        end_date,
        full,
        dry_run,
    )

    try:
        with _advisory_lock() as acquired:
            if not acquired:
                logger.info("reconciliation_skipped_locked mode=%s", mode)
                return _finish_run(
                    run,
                    ImportRun.Status.SKIPPED,
                    "Another reconciliation is already running.",
                    started_clock,
                )
            _fail_abandoned_runs(run)
            user = _resolve_import_user()
            run.user = user
            run.save(update_fields=("user",))
            rows = fetch_worksheet_rows()
            run.rows_read = len(rows)
            run.save(update_fields=("rows_read",))
            result = _process_snapshot(rows=rows, run=run, user=user, start_date=start_date, end_date=end_date, full=full, dry_run=dry_run)
            run.rows_created = result["transactions_created"]
            run.rows_updated = result["transactions_updated"]
            run.rows_skipped = result["transactions_unchanged"]
            run.transactions_created = result["transactions_created"]
            run.transactions_updated = result["transactions_updated"]
            run.transactions_unchanged = result["transactions_unchanged"]
            run.transactions_missing = result["transactions_missing"]
            run.transactions_restored = result["transactions_restored"]
            run.rows_failed = result["rows_failed"]
            run.error_summary = result["error_summary"]
            run.save(
                update_fields=(
                    "rows_created",
                    "rows_updated",
                    "rows_skipped",
                    "transactions_created",
                    "transactions_updated",
                    "transactions_unchanged",
                    "transactions_missing",
                    "transactions_restored",
                    "rows_failed",
                    "error_summary",
                )
            )
            status = ImportRun.Status.PARTIAL if run.rows_failed else ImportRun.Status.SUCCESS
            _finish_run(run, status, result["error_summary"], started_clock)
            logger.info(
                "reconciliation_finished mode=%s status=%s rows_read=%s created=%s updated=%s unchanged=%s missing=%s restored=%s failed=%s duration_ms=%s",
                mode,
                run.status,
                run.rows_read,
                run.rows_created,
                run.rows_updated,
                run.transactions_unchanged,
                run.transactions_missing,
                run.transactions_restored,
                run.rows_failed,
                run.duration_ms,
            )
            return run
    except Exception as exc:
        _finish_run(
            run,
            ImportRun.Status.FAILED,
            f"Reconciliation failed ({type(exc).__name__}).",
            started_clock,
        )
        logger.error("reconciliation_failed mode=%s exception=%s", mode, type(exc).__name__)
        raise
