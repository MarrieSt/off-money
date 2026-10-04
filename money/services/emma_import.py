import hashlib
import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model

from finance.models import FinancialAccount, Transaction


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


def run_emma_import():
    """Backward-compatible alias for a full Emma reconciliation."""
    from money.services.reconciliation import reconcile_transactions

    return reconcile_transactions(full=True)
