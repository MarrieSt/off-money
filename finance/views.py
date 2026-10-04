import json
from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .analytics import (
    build_behaviour_calendar_payload,
    build_spending_payload,
    get_behaviour_day_details,
    get_spending_transactions,
    parse_iso_date,
)
from .models import FinancialAccount, SpendRule, Transaction


@login_required
def home(request):
    has_transactions = Transaction.objects.filter(user=request.user).exists()
    return render(request, "finance/home.html", {"has_transactions": has_transactions})


@login_required
def transactions(request):
    transaction_list = Transaction.objects.filter(user=request.user)
    selected_date = request.GET.get("date")
    if selected_date:
        try:
            transaction_list = transaction_list.filter(transaction_date=parse_iso_date(selected_date))
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
    transaction_list = (
        transaction_list
        .select_related("account")
        .order_by("-transaction_date", "-id")
    )
    page = Paginator(transaction_list, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "finance/transactions.html",
        {"page": page, "selected_date": selected_date},
    )


@login_required
def transaction_detail(request, pk):
    transaction_record = get_object_or_404(
        Transaction.objects.select_related("account").filter(user=request.user),
        pk=pk,
    )
    raw_data_json = json.dumps(
        transaction_record.raw_data,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return render(
        request,
        "finance/transaction_detail.html",
        {"transaction": transaction_record, "raw_data_json": raw_data_json},
    )


@login_required
def accounts(request):
    user_accounts = FinancialAccount.objects.filter(user=request.user)
    return render(request, "finance/accounts.html", {"accounts": user_accounts})


@login_required
def rules(request):
    user_rules = SpendRule.objects.filter(user=request.user)
    return render(request, "finance/rules.html", {"rules": user_rules})


@require_GET
def spending_api(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)
    granularity = request.GET.get("granularity", "day")
    stack_by = request.GET.get("stack_by", "category")
    try:
        anchor = parse_iso_date(request.GET["end_date"]) if "end_date" in request.GET else timezone.localdate()
        payload = build_spending_payload(
            request.user,
            granularity=granularity,
            stack_by=stack_by,
            anchor=anchor,
            today=timezone.localdate(),
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(payload)


@require_GET
def spending_transactions_api(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)
    try:
        start_date = parse_iso_date(request.GET.get("start_date"))
        end_date = parse_iso_date(request.GET.get("end_date"))
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date.")
        if (end_date - start_date).days > 366:
            raise ValueError("The requested period is too large.")
        if end_date > timezone.localdate():
            raise ValueError("Future dates are not available.")
        stack_by = request.GET.get("stack_by", "category")
        if stack_by not in {"category", "account"}:
            raise ValueError("stack_by must be category or account.")
        series_key = request.GET.get("series_key") or None
        excluded_keys = request.GET.getlist("excluded_keys")
        if len(excluded_keys) > 6:
            raise ValueError("At most six series may be excluded.")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    rows, has_more = get_spending_transactions(
        request.user,
        start_date=start_date,
        end_date=end_date,
        stack_by=stack_by,
        series_key=series_key,
        excluded_keys=excluded_keys,
    )
    return JsonResponse(
        {
            "rows": [
                {
                    **row,
                    "contribution": format(row["contribution"], ".2f"),
                }
                for row in rows
            ],
            "has_more": has_more,
        }
    )


@require_GET
def behaviour_calendar_api(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)
    month_value = request.GET.get("month")
    try:
        if month_value:
            month_start = date.fromisoformat(f"{month_value}-01")
        else:
            month_start = timezone.localdate().replace(day=1)
        payload = build_behaviour_calendar_payload(
            request.user,
            month_anchor=month_start,
            today=timezone.localdate(),
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(payload)


@require_GET
def behaviour_day_api(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)
    try:
        selected_date = parse_iso_date(request.GET.get("date"))
        if selected_date > timezone.localdate():
            raise ValueError("Future dates are not available.")
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(get_behaviour_day_details(request.user, selected_date))
