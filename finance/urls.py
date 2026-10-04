from django.urls import path

from . import views

app_name = "finance"

urlpatterns = [
    path("", views.home, name="home"),
    path("transactions/", views.transactions, name="transactions"),
    path("transactions/<int:pk>/", views.transaction_detail, name="transaction_detail"),
    path("accounts/", views.accounts, name="accounts"),
    path("rules/", views.rules, name="rules"),
    path("api/analytics/spending", views.spending_api, name="spending_api"),
    path(
        "api/analytics/behaviour-calendar",
        views.behaviour_calendar_api,
        name="behaviour_calendar_api",
    ),
    path(
        "api/analytics/behaviour-calendar/day",
        views.behaviour_day_api,
        name="behaviour_day_api",
    ),
    path(
        "api/analytics/spending/transactions",
        views.spending_transactions_api,
        name="spending_transactions_api",
    ),
]
