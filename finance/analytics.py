from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    DecimalField,
    F,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import (
    Abs,
    Cast,
    Coalesce,
    NullIf,
    TruncDate,
    TruncMonth,
    TruncWeek,
)

from .models import SpendRule, Transaction


CURRENCY = "GBP"
OTHER_KEY = "__other__"
UNCATEGORISED_KEY = "__uncategorised__"
MONEY_FIELD = DecimalField(max_digits=16, decimal_places=2)


def parse_iso_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("Dates must use YYYY-MM-DD format.") from None


def _month_start(value):
    return value.replace(day=1)


def _bucket_start(value, granularity):
    if granularity == "day":
        return value
    if granularity == "week":
        return value - timedelta(days=value.weekday())
    return _month_start(value)


def _shift_month(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _shift_bucket(value, granularity, count):
    if granularity == "day":
        return value + timedelta(days=count)
    if granularity == "week":
        return value + timedelta(days=7 * count)
    return _shift_month(value, count)


def build_window(granularity, anchor, today):
    if granularity not in {"day", "week", "month"}:
        raise ValueError("granularity must be day, week, or month.")
    if anchor > today:
        raise ValueError("Future dates are not available.")

    final_start = _bucket_start(anchor, granularity)
    current_start = _bucket_start(today, granularity)
    starts = [_shift_bucket(final_start, granularity, offset) for offset in range(-6, 1)]
    buckets = []
    for bucket_start in starts:
        next_start = _shift_bucket(bucket_start, granularity, 1)
        bucket_end = next_start - timedelta(days=1)
        if bucket_start == current_start:
            bucket_end = min(bucket_end, today)
        buckets.append({"start": bucket_start, "end": bucket_end})

    return {
        "buckets": buckets,
        "start": buckets[0]["start"],
        "end": buckets[-1]["end"],
        "anchor": final_start,
        "current": final_start == current_start,
        "can_navigate_forward": final_start < current_start,
    }


def _rule_condition(rule):
    field_paths = {
        SpendRule.MatchField.CATEGORY: ("source_category",),
        SpendRule.MatchField.SUBCATEGORY: ("source_subcategory",),
        SpendRule.MatchField.TYPE: ("source_type",),
        SpendRule.MatchField.MERCHANT: ("source_merchant", "merchant_name"),
        SpendRule.MatchField.COUNTERPARTY: ("source_counterparty",),
    }
    if rule.match_field == SpendRule.MatchField.ACCOUNT:
        field_paths[SpendRule.MatchField.ACCOUNT] = (
            "account__source_name",
            "account__display_name",
        )
    paths = field_paths.get(rule.match_field, ())
    lookups = {
        SpendRule.MatchOperator.EQUALS: "iexact",
        SpendRule.MatchOperator.CONTAINS: "icontains",
        SpendRule.MatchOperator.STARTS_WITH: "istartswith",
    }
    lookup = lookups.get(rule.match_operator)
    if lookup is None:
        return Q(pk__in=[])
    condition = Q()
    for path in paths:
        condition |= Q(**{f"{path}__{lookup}": rule.match_value})
    return condition


def _eligibility_expression(user):
    rules = SpendRule.objects.filter(
        user=user,
        is_active=True,
        action__in=(
            SpendRule.Action.INCLUDE_IN_SPEND,
            SpendRule.Action.EXCLUDE_FROM_SPEND,
        ),
    ).order_by("priority", "pk")
    rule_cases = [
        When(
            _rule_condition(rule),
            then=Value(rule.action == SpendRule.Action.INCLUDE_IN_SPEND),
        )
        for rule in rules
    ]
    baseline = Q(account__include_in_analytics=True, amount__lt=0, source_type__iexact="purchase")
    default = Case(
        When(baseline, then=Value(True)),
        default=Value(False),
        output_field=BooleanField(),
    )
    return Case(*rule_cases, default=default, output_field=BooleanField())


def _eligible_transactions(user):
    return Transaction.objects.filter(
        user=user,
        currency=CURRENCY,
        source_state=Transaction.SourceState.ACTIVE,
    ).exclude(
        Q(amount__gt=0)
        & (Q(source_type__iexact="refund") | Q(source_linked_transaction_id__gt=""))
    ).annotate(spend_eligible=_eligibility_expression(user)).filter(spend_eligible=True)


def _trunc_expression(granularity):
    if granularity == "day":
        return TruncDate("transaction_date")
    if granularity == "week":
        return TruncWeek("transaction_date")
    return TruncMonth("transaction_date")


def _category_expressions(queryset):
    category = F("source_category")
    key = Coalesce(
        NullIf(category, Value("")),
        Value(UNCATEGORISED_KEY),
        output_field=CharField(),
    )
    queryset = queryset.annotate(series_key=key)
    return queryset.annotate(
        series_label=Case(
            When(series_key=UNCATEGORISED_KEY, then=Value("Uncategorised")),
            default=F("series_key"),
            output_field=CharField(),
        )
    )


def _account_expressions(queryset):
    account_id = F("account_id")
    account_display = F("account__display_name")
    account_source = F("account__source_name")
    key = Cast(account_id, output_field=CharField())
    label = Coalesce(NullIf(account_display, Value("")), account_source, output_field=CharField())
    return queryset.annotate(series_key=key, series_label=label)


def _series_expressions(queryset, stack_by):
    if stack_by == "category":
        return _category_expressions(queryset)
    return _account_expressions(queryset)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def _contribution_groups(user, window, granularity, stack_by):
    trunc = _trunc_expression(granularity)
    base = _eligible_transactions(user)
    period_filter = {"transaction_date__range": (window["start"], window["end"])}
    eligible = base.filter(**period_filter)
    eligible = _series_expressions(eligible, stack_by).annotate(
        period=trunc,
        contribution=Abs(F("amount")),
    )
    return list(
        eligible.values("period", "series_key", "series_label")
        .annotate(total=Sum("contribution"))
        .order_by()
    )


def _money_string(value):
    return format(value.quantize(Decimal("0.01")), "f")


def behaviour_band(total):
    total = Decimal(total)
    if total == 0:
        return "zero", "No-spend day"
    if total <= Decimal("50.00"):
        return "low", "Low-spend day"
    if total <= Decimal("150.00"):
        return "medium", "Higher-spend day"
    return "high", "High-spend day"


def build_behaviour_calendar_payload(user, month_anchor, today):
    month_start = month_anchor.replace(day=1)
    current_month = today.replace(day=1)
    if month_start > current_month:
        raise ValueError("Future months are not available.")

    month_end = date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])
    data_end = min(month_end, today)
    daily_rows = (
        _eligible_transactions(user)
        .filter(transaction_date__range=(month_start, data_end))
        .annotate(calendar_day=TruncDate("transaction_date"))
        .values("calendar_day")
        .annotate(total=Sum(Abs(F("amount"))), transaction_count=Count("pk"))
        .order_by("calendar_day")
    )
    daily = {
        row["calendar_day"]: {
            "total": row["total"] or Decimal("0"),
            "transaction_count": row["transaction_count"],
        }
        for row in daily_rows
    }

    transactions = (
        _eligible_transactions(user)
        .filter(transaction_date__range=(month_start, data_end))
        .select_related("account")
        .order_by("transaction_date", "pk")
    )
    mini_bars = {}
    for item in transactions:
        mini_bars.setdefault(item.transaction_date, []).append(
            {
                "amount": abs(item.amount),
                "category": item.source_category or "Uncategorised",
                "description": item.description or item.source_merchant or item.source_counterparty or "Transaction",
            }
        )

    days = []
    for day_number in range(1, month_end.day + 1):
        current_date = date(month_start.year, month_start.month, day_number)
        is_future = current_date > today
        totals = daily.get(current_date)
        total = totals["total"] if totals else Decimal("0")
        count = totals["transaction_count"] if totals else 0
        if is_future:
            band, behavior = "future", "Future day"
            day_total = None
        else:
            band, behavior = behaviour_band(total)
            day_total = _money_string(total)

        entries = mini_bars.get(current_date, [])
        entries.sort(key=lambda item: item["amount"], reverse=True)
        if len(entries) > 8:
            retained = entries[:7]
            remainder = entries[7:]
            retained.append(
                {
                    "amount": sum((entry["amount"] for entry in remainder), Decimal("0")),
                    "category": "Other",
                    "description": f"{len(remainder)} other transactions",
                    "aggregate_count": len(remainder),
                }
            )
            entries = retained
        max_bar = max((entry["amount"] for entry in entries), default=Decimal("0"))
        bars = [
            {
                "amount": _money_string(entry["amount"]),
                "category": entry["category"],
                "description": entry["description"],
                "height": max(12, int(entry["amount"] / max_bar * 100)) if max_bar else 0,
                "aggregate_count": entry.get("aggregate_count", 1),
            }
            for entry in entries
        ]
        days.append(
            {
                "date": current_date.isoformat(),
                "day_number": day_number,
                "in_month": True,
                "is_future": is_future,
                "is_today": current_date == today,
                "total_spend": day_total,
                "transaction_count": 0 if is_future else count,
                "band": band,
                "behavior": behavior,
                "bars": [] if is_future else bars,
            }
        )

    return {
        "month": month_start.strftime("%Y-%m"),
        "month_label": month_start.strftime("%B %Y"),
        "today": today.isoformat(),
        "is_current_month": month_start == current_month,
        "can_navigate_forward": month_start < current_month,
        "currency": CURRENCY,
        "days": days,
    }


def get_behaviour_day_details(user, selected_date):
    query = (
        _eligible_transactions(user)
        .filter(transaction_date=selected_date)
        .select_related("account")
        .order_by("amount", "pk")
    )
    transactions = [
        {
            "id": item.pk,
            "date": item.transaction_date.isoformat(),
            "description": item.description or item.source_merchant or item.source_counterparty or "Transaction",
            "merchant": item.merchant_name or item.source_merchant or item.source_counterparty,
            "category": item.source_category or "Uncategorised",
            "account": item.account.display_name or item.account.source_name,
            "amount": _money_string(abs(item.amount)),
            "currency": item.currency,
        }
        for item in query
    ]
    total = sum((Decimal(item["amount"]) for item in transactions), Decimal("0"))
    band, behavior = behaviour_band(total)
    return {
        "date": selected_date.isoformat(),
        "total_spend": _money_string(total),
        "currency": CURRENCY,
        "transaction_count": len(transactions),
        "band": band,
        "behavior": behavior,
        "transactions": transactions,
    }


def _bucket_key(value):
    return _as_date(value).isoformat()


def build_spending_payload(user, granularity, stack_by, anchor, today):
    if stack_by not in {"category", "account"}:
        raise ValueError("stack_by must be category or account.")
    window = build_window(granularity, anchor, today)
    bucket_map = {bucket["start"].isoformat(): bucket for bucket in window["buckets"]}
    values_by_bucket = {key: {} for key in bucket_map}
    labels = {}

    for row in _contribution_groups(user, window, granularity, stack_by):
        bucket = _bucket_key(row["period"])
        if bucket not in values_by_bucket:
            continue
        series_key = str(row["series_key"])
        labels[series_key] = row["series_label"] or "Uncategorised"
        values_by_bucket[bucket][series_key] = (
            values_by_bucket[bucket].get(series_key, Decimal("0")) + row["total"]
        )

    totals = {}
    for bucket_values in values_by_bucket.values():
        for series_key, value in bucket_values.items():
            totals[series_key] = totals.get(series_key, Decimal("0")) + value
    ranked_keys = sorted(totals, key=lambda key: (-totals[key], labels[key].casefold()))
    top_keys = ranked_keys[:6]
    other_keys = ranked_keys[6:]
    series = [
        {"key": key, "label": labels[key], "total": _money_string(totals[key]), "members": [key]}
        for key in top_keys
    ]
    if other_keys:
        series.append(
            {
                "key": OTHER_KEY,
                "label": "Other",
                "total": _money_string(sum((totals[key] for key in other_keys), Decimal("0"))),
                "members": other_keys,
                "excluded_keys": top_keys,
            }
        )

    buckets = []
    overall_total = Decimal("0")
    for bucket in window["buckets"]:
        bucket_key = bucket["start"].isoformat()
        bucket_values = values_by_bucket[bucket_key]
        segment_values = {key: bucket_values.get(key, Decimal("0")) for key in top_keys}
        if other_keys:
            segment_values[OTHER_KEY] = sum(
                (bucket_values.get(key, Decimal("0")) for key in other_keys), Decimal("0")
            )
        bucket_total = sum(segment_values.values(), Decimal("0"))
        overall_total += bucket_total
        segments = [
            {"key": item["key"], "label": item["label"], "value": _money_string(segment_values[item["key"]])}
            for item in series
        ]
        breakdown = [
            {"key": key, "label": labels[key], "value": _money_string(value)}
            for key, value in sorted(
                bucket_values.items(), key=lambda item: (-item[1], labels[item[0]].casefold())
            )
        ]
        buckets.append(
            {
                "key": bucket_key,
                "start": bucket["start"].isoformat(),
                "end": bucket["end"].isoformat(),
                "total": _money_string(bucket_total),
                "segments": segments,
                "breakdown": breakdown,
            }
        )

    return {
        "granularity": granularity,
        "stack_by": stack_by,
        "currency": CURRENCY,
        "total": _money_string(overall_total),
        "range_start": window["start"].isoformat(),
        "range_end": window["end"].isoformat(),
        "window_anchor": window["anchor"].isoformat(),
        "is_current": window["current"],
        "can_navigate_forward": window["can_navigate_forward"],
        "series": series,
        "buckets": buckets,
    }


def get_spending_transactions(user, start_date, end_date, stack_by, series_key=None, excluded_keys=()):
    base = _eligible_transactions(user)
    date_filter = {"transaction_date__range": (start_date, end_date)}
    eligible = _series_expressions(base.filter(**date_filter), stack_by).select_related("account")

    def matches_series(transaction_record):
        key = transaction_record.series_key
        if series_key == OTHER_KEY:
            return key not in excluded_keys
        return series_key is None or key == series_key

    events = []
    for item in eligible:
        if not matches_series(item):
            continue
        events.append(
            {
                "id": item.pk,
                "date": item.transaction_date.isoformat(),
                "description": item.description or item.source_merchant or item.source_counterparty or "Transaction",
                "merchant": item.merchant_name or item.source_merchant or item.source_counterparty,
                "account": item.account.display_name or item.account.source_name,
                "category": item.source_category or "Uncategorised",
                "contribution": abs(item.amount),
                "is_refund": False,
            }
        )
    events.sort(key=lambda item: (item["date"], item["id"]), reverse=True)
    return events[:200], len(events) > 200
