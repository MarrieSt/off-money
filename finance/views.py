from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from .models import FinancialAccount, SpendRule, Transaction


@login_required
def home(request):
    has_transactions = Transaction.objects.filter(user=request.user).exists()
    return render(request, "finance/home.html", {"has_transactions": has_transactions})


@login_required
def transactions(request):
    transaction_list = (
        Transaction.objects.filter(user=request.user)
        .select_related("account")
        .order_by("-transaction_date", "-id")
    )
    page = Paginator(transaction_list, 25).get_page(request.GET.get("page"))
    return render(request, "finance/transactions.html", {"page": page})


@login_required
def accounts(request):
    user_accounts = FinancialAccount.objects.filter(user=request.user)
    return render(request, "finance/accounts.html", {"accounts": user_accounts})


@login_required
def rules(request):
    user_rules = SpendRule.objects.filter(user=request.user)
    return render(request, "finance/rules.html", {"rules": user_rules})
