from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("", views.home, name="home"),
    path("transactions/", views.transactions, name="transactions"),
    path("accounts/", views.accounts, name="accounts"),
    path("rules/", views.rules, name="rules"),
    path("api/analytics/spending", views.spending_api, name="spending_api"),
    path(
        "api/analytics/spending/transactions",
        views.spending_transactions_api,
        name="spending_transactions_api",
    ),
]
