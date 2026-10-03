import hashlib
import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from finance.models import EmmaRawTransaction, FinancialAccount, ImportRun, Transaction
from money.services.google_sheets import fetch_worksheet_rows


logger = logging.getLogger(__name__)


class ImportConfigurationError(RuntimeError):
    pass


class ImportRowError(ValueError):
    pass


SOURCE_DATE_FORMATS = {
    Transaction.SOURCE_EMMA: ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"),
}


def _field_map(row):
    return {str(key).strip().casefold(): value for key, value in row.items()}


def _value(fields, name):
    value = fields.get(name.casefold())
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def parse_transaction_date(value, source_system=Transaction.SOURCE_EMMA):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        raise ImportRowError("Transaction date is missing.")

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    for date_format in SOURCE_DATE_FORMATS.get(source_system, ()):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise ImportRowError("Transaction date has an unsupported format.")


def parse_amount(value):
    if isinstance(value, Decimal):
        amount = value
    else:
        text = str(value or "").strip().replace(",", "").replace("£", "")
        if not text:
            raise ImportRowError("Transaction amount is missing.")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ImportRowError("Transaction amount is invalid.") from exc
    if not amount.is_finite():
        raise ImportRowError("Transaction amount is invalid.")
    if amount.as_tuple().exponent < -2:
        raise ImportRowError("Transaction amount has more than two decimal places.")
    return amount


def normalize_row(raw_data, source_system=Transaction.SOURCE_EMMA):
    normalized = {}
    for key, original_value in raw_data.items():
        header = str(key).strip()
        value = original_value.strip() if isinstance(original_value, str) else original_value
        if value == "":
            value = None

        normalized_header = header.casefold()
        if normalized_header == "date" and value is not None:
            try:
                value = parse_transaction_date(value, source_system=source_system).isoformat()
            except ImportRowError:
                pass
        elif normalized_header == "amount" and value is not None:
            try:
                value = format(parse_amount(value).normalize(), "f")
            except ImportRowError:
                pass
        elif isinstance(value, Decimal):
            value = format(value.normalize(), "f")
        elif isinstance(value, (date, datetime)):
            value = value.isoformat()
        normalized[header] = value
    return normalized


def hash_row(raw_data, source_system=Transaction.SOURCE_EMMA):
    stable_json = json.dumps(
        normalize_row(raw_data, source_system=source_system),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(stable_json.encode("utf-8")).hexdigest()


def _as_source_text(value):
    return "" if value is None else str(value).strip()


def _resolve_import_user():
    username = settings.MONEY_IMPORT_USERNAME.strip()
    if not username:
        raise ImportConfigurationError("MONEY_IMPORT_USERNAME is not configured.")
    try:
        return get_user_model().objects.get(username=username)
    except get_user_model().DoesNotExist as exc:
        raise ImportConfigurationError(
            "MONEY_IMPORT_USERNAME does not match an existing Money user."
        ) from exc


def _account_for_row(user, fields):
    account_name = _value(fields, "Account")
    institution_name = _value(fields, "Bank")
    if not account_name:
        raise ImportRowError("Account name is missing.")

    account, created = FinancialAccount.objects.get_or_create(
        user=user,
        source_system=Transaction.SOURCE_EMMA,
        source_name=account_name,
        institution_name=institution_name or "",
        defaults={"currency": settings.MONEY_DEFAULT_CURRENCY},
    )
    if created:
        logger.info("Created Emma financial account from source row")
    return account


def _transaction_defaults(user, account, fields, raw_data, source_hash, now, existing):
    transaction_date = parse_transaction_date(
        _value(fields, "Date"), source_system=Transaction.SOURCE_EMMA
    )
    amount = parse_amount(_value(fields, "Amount"))
    merchant = _as_source_text(_value(fields, "Merchant"))
    counterparty = _as_source_text(_value(fields, "Counterparty"))
    custom_name = _as_source_text(_value(fields, "Custom Name"))
    description = custom_name or merchant or counterparty
    currency = _as_source_text(_value(fields, "Currency")).upper()
    currency = currency or settings.MONEY_DEFAULT_CURRENCY

    return {
        "account": account,
        "transaction_date": transaction_date,
        "posted_date": None,
        "description": description,
        "merchant_name": merchant,
        "amount": amount,
        "currency": currency,
        "source_system": Transaction.SOURCE_EMMA,
        "source_content_hash": source_hash,
        "source_category": _as_source_text(_value(fields, "Category")),
        "source_subcategory": _as_source_text(_value(fields, "Subcategory")),
        "source_type": _as_source_text(_value(fields, "Type")),
        "source_tags": _as_source_text(_value(fields, "Tags")),
        "source_counterparty": counterparty,
        "source_custom_name": custom_name,
        "source_merchant": merchant,
        "source_additional_details": _as_source_text(_value(fields, "Additional details")),
        "source_notes": _as_source_text(_value(fields, "Notes")),
        "source_linked_transaction_id": _as_source_text(_value(fields, "Linked transaction ID")),
        "raw_data": raw_data,
        "imported_at": (existing.imported_at if existing and existing.imported_at else now),
        "last_source_sync_at": now,
    }


def _process_row(run, user, row):
    row_number = row["source_row_number"]
    raw_data = row["raw_data"]
    fields = _field_map(raw_data)
    source_id = _value(fields, "ID")
    if not source_id:
        raise ImportRowError("Emma transaction ID is missing.")

    now = timezone.now()
    source_hash = hash_row(raw_data, source_system=Transaction.SOURCE_EMMA)
    raw_record, created = EmmaRawTransaction.objects.get_or_create(
        user=user,
        source_system=Transaction.SOURCE_EMMA,
        source_transaction_id=str(source_id),
        defaults={
            "import_run": run,
            "source_row_number": row_number,
            "source_hash": source_hash,
            "raw_data": raw_data,
            "first_seen_at": now,
            "last_seen_at": now,
        },
    )
    if not created:
        raw_record.import_run = run
        raw_record.source_row_number = row_number
        raw_record.source_hash = source_hash
        raw_record.raw_data = raw_data
        raw_record.last_seen_at = now
        raw_record.save(
            update_fields=(
                "import_run",
                "source_row_number",
                "source_hash",
                "raw_data",
                "last_seen_at",
                "updated_at",
            )
        )

    existing = Transaction.objects.filter(
        user=user,
        source_system=Transaction.SOURCE_EMMA,
        source_transaction_id=str(source_id),
    ).first()
    if existing and existing.source_content_hash == source_hash:
        return "skipped"

    account = _account_for_row(user, fields)
    defaults = _transaction_defaults(user, account, fields, raw_data, source_hash, now, existing)
    with transaction.atomic():
        _, was_created = Transaction.objects.update_or_create(
            user=user,
            source_system=Transaction.SOURCE_EMMA,
            source_transaction_id=str(source_id),
            defaults=defaults,
        )
    return "created" if was_created else "updated"


def _finish_run(run, status, error_message=""):
    run.status = status
    run.finished_at = timezone.now()
    run.error_message = error_message
    run.save(update_fields=("status", "finished_at", "error_message"))


def run_emma_import():
    """Fetch Emma rows and upsert raw and canonical records for the configured user."""
    run = ImportRun.objects.create(started_at=timezone.now())
    logger.info("Emma import %s started", run.pk)

    try:
        user = _resolve_import_user()
        run.user = user
        run.save(update_fields=("user",))
        rows = fetch_worksheet_rows()
        run.rows_read = len(rows)
        run.save(update_fields=("rows_read",))

        for row in rows:
            try:
                result = _process_row(run, user, row)
            except Exception as exc:
                run.rows_failed += 1
                logger.warning(
                    "Emma import %s failed at source row %s (%s)",
                    run.pk,
                    row.get("source_row_number", "unknown"),
                    type(exc).__name__,
                )
            else:
                setattr(run, f"rows_{result}", getattr(run, f"rows_{result}") + 1)
                if result == "created":
                    logger.info("Emma import %s created a transaction", run.pk)
                elif result == "updated":
                    logger.info("Emma import %s updated a transaction", run.pk)
                else:
                    logger.info("Emma import %s skipped an unchanged row", run.pk)
            run.save(
                update_fields=("rows_created", "rows_updated", "rows_skipped", "rows_failed")
            )

        status = ImportRun.Status.PARTIAL if run.rows_failed else ImportRun.Status.SUCCESS
        _finish_run(run, status)
        logger.info(
            "Emma import %s completed: read=%s created=%s updated=%s skipped=%s failed=%s",
            run.pk,
            run.rows_read,
            run.rows_created,
            run.rows_updated,
            run.rows_skipped,
            run.rows_failed,
        )
        return run
    except Exception as exc:
        _finish_run(run, ImportRun.Status.FAILED, "Import failed; see application logs.")
        logger.error("Emma import %s failed (%s)", run.pk, type(exc).__name__)
        raise
