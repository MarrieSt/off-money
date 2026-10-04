from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import (
    BooleanField,
    Case,
    CharField,
    DecimalField,
    Exists,
    F,
    OuterRef,
    Q,
    Subquery,
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
    baseline = (
        Q(account__include_in_analytics=True)
        & (
            Q(amount__lt=0, source_type__iexact="purchase")
            | Q(amount__gt=0, source_linked_transaction_id__gt="")
        )
    )
    default = Case(
        When(baseline, then=Value(True)),
        default=Value(False),
        output_field=BooleanField(),
    )
    return Case(*rule_cases, default=default, output_field=BooleanField())


def _eligible_transactions(user):
    return Transaction.objects.filter(user=user, currency=CURRENCY).annotate(
        spend_eligible=_eligibility_expression(user)
    ).filter(spend_eligible=True)


def _trunc_expression(granularity):
    if granularity == "day":
        return TruncDate("transaction_date")
    if granularity == "week":
        return TruncWeek("transaction_date")
    return TruncMonth("transaction_date")


def _category_expressions(queryset, original=None):
    if original is None:
        category = F("source_category")
    else:
        category = Subquery(original.values("source_category")[:1], output_field=CharField())
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


def _account_expressions(queryset, original=None):
    if original is None:
        account_id = F("account_id")
        account_display = F("account__display_name")
        account_source = F("account__source_name")
    else:
        account_id = Subquery(original.values("account_id")[:1])
        account_display = Subquery(original.values("account__display_name")[:1], output_field=CharField())
        account_source = Subquery(original.values("account__source_name")[:1], output_field=CharField())
    key = Cast(account_id, output_field=CharField())
    label = Coalesce(NullIf(account_display, Value("")), account_source, output_field=CharField())
    return queryset.annotate(series_key=key, series_label=label)


def _series_expressions(queryset, stack_by, original=None):
    if stack_by == "category":
        return _category_expressions(queryset, original=original)
    return _account_expressions(queryset, original=original)


def _refund_origin_queryset(user, base_queryset):
    return base_queryset.filter(
        source_system=OuterRef("source_system"),
        source_transaction_id=OuterRef("source_linked_transaction_id"),
        amount__lt=0,
        source_type__iexact="purchase",
        spend_eligible=True,
    )


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def _contribution_groups(user, window, granularity, stack_by):
    trunc = _trunc_expression(granularity)
    base = _eligible_transactions(user)
    period_filter = {"transaction_date__range": (window["start"], window["end"])}
    regular = base.filter(**period_filter).exclude(
        Q(amount__gt=0) & Q(source_linked_transaction_id__gt="")
    )
    regular = _series_expressions(regular, stack_by).annotate(
        period=trunc,
        contribution=Abs(F("amount")),
    )
    grouped = list(
        regular.values("period", "series_key", "series_label")
        .annotate(total=Sum("contribution"))
        .order_by()
    )

    origins = _refund_origin_queryset(user, base)
    refunds = base.filter(
        **period_filter,
        amount__gt=0,
        source_linked_transaction_id__gt="",
    ).annotate(
        linked_purchase=Exists(origins),
        original_category=Subquery(origins.values("source_category")[:1], output_field=CharField()),
        original_account_id=Subquery(origins.values("account_id")[:1]),
        original_account_display=Subquery(origins.values("account__display_name")[:1], output_field=CharField()),
        original_account_source=Subquery(origins.values("account__source_name")[:1], output_field=CharField()),
    ).filter(linked_purchase=True)
    refunds = _series_expressions(refunds, stack_by, original=origins).annotate(
        period=trunc,
        contribution=-F("amount"),
    )
    grouped.extend(
        refunds.values("period", "series_key", "series_label")
        .annotate(total=Sum("contribution"))
        .order_by()
    )
    return grouped


def _money_string(value):
    return format(value.quantize(Decimal("0.01")), "f")


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
    regular = base.filter(**date_filter).exclude(
        Q(amount__gt=0) & Q(source_linked_transaction_id__gt="")
    )
    regular = _series_expressions(regular, stack_by).select_related("account")
    origins = _refund_origin_queryset(user, base)
    refunds = base.filter(
        **date_filter,
        amount__gt=0,
        source_linked_transaction_id__gt="",
    ).annotate(
        linked_purchase=Exists(origins),
        original_category=Subquery(origins.values("source_category")[:1], output_field=CharField()),
        original_account_id=Subquery(origins.values("account_id")[:1]),
        original_account_display=Subquery(origins.values("account__display_name")[:1], output_field=CharField()),
        original_account_source=Subquery(origins.values("account__source_name")[:1], output_field=CharField()),
    ).filter(linked_purchase=True)
    refunds = _series_expressions(refunds, stack_by, original=origins).select_related("account")

    def matches_series(transaction_record):
        key = transaction_record.series_key
        if series_key == OTHER_KEY:
            return key not in excluded_keys
        return series_key is None or key == series_key

    events = []
    for item in regular:
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
    for item in refunds:
        if not matches_series(item):
            continue
        if stack_by == "account":
            account_label = item.series_label
            category_label = item.original_category or "Uncategorised"
        else:
            account_label = item.original_account_display or item.original_account_source
            category_label = item.series_label
        events.append(
            {
                "id": item.pk,
                "date": item.transaction_date.isoformat(),
                "description": item.description or item.source_merchant or item.source_counterparty or "Refund",
                "merchant": item.merchant_name or item.source_merchant or item.source_counterparty,
                "account": account_label,
                "category": category_label,
                "contribution": -item.amount,
                "is_refund": True,
            }
        )
    events.sort(key=lambda item: (item["date"], item["id"]), reverse=True)
    return events[:200], len(events) > 200
