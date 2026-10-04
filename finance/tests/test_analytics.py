from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from finance.analytics import build_window
from finance.models import FinancialAccount, SpendRule, Transaction


class SpendingAnalyticsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="chart-user", password="password")
        self.other_user = get_user_model().objects.create_user(username="other-user", password="password")
        self.account = FinancialAccount.objects.create(
            user=self.user,
            source_name="Everyday account",
            display_name="Everyday",
            institution_name="Example Bank",
        )
        self.today = timezone.localdate()

    def add_transaction(
        self,
        *,
        user=None,
        account=None,
        date=None,
        amount="-10.00",
        currency="GBP",
        category="Food",
        transaction_type="Purchase",
        source_id=None,
        linked_id="",
    ):
        user = user or self.user
        account = account or self.account
        return Transaction.objects.create(
            user=user,
            account=account,
            source_system="emma",
            source_transaction_id=source_id,
            transaction_date=date or self.today,
            amount=Decimal(amount),
            currency=currency,
            source_category=category,
            source_type=transaction_type,
            source_merchant="Example merchant",
            source_linked_transaction_id=linked_id,
        )

    def get_payload(self, **params):
        self.client.force_login(self.user)
        response = self.client.get(reverse("finance:spending_api"), params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_unauthenticated_analytics_api_returns_json_401(self):
        response = self.client.get(reverse("finance:spending_api"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Authentication required.")

    def test_day_window_has_seven_buckets_and_keeps_zero_days(self):
        window = build_window("day", self.today, self.today)
        self.assertEqual(len(window["buckets"]), 7)
        self.assertEqual(window["buckets"][0]["start"], self.today - timedelta(days=6))
        self.assertEqual(window["buckets"][-1]["end"], self.today)

        payload = self.get_payload()

        self.assertEqual(len(payload["buckets"]), 7)
        self.assertEqual(payload["buckets"][0]["total"], "0.00")
        self.assertEqual(payload["buckets"][-1]["total"], "0.00")

    def test_only_gbp_negative_purchases_count_by_default(self):
        self.add_transaction(amount="-12.34", category="Food", source_id="purchase-1")
        self.add_transaction(amount="45.00", category="Income", transaction_type="Income")
        self.add_transaction(amount="-20.00", category="Transfer", transaction_type="Transfer")
        self.add_transaction(amount="-30.00", category="Travel", currency="USD")
        other_account = FinancialAccount.objects.create(
            user=self.user,
            source_name="Excluded account",
            institution_name="Example Bank",
            include_in_analytics=False,
        )
        self.add_transaction(account=other_account, amount="-50.00", category="Excluded")
        self.add_transaction(user=self.other_user, amount="-90.00", category="Private")

        payload = self.get_payload(stack_by="category")

        self.assertEqual(payload["currency"], "GBP")
        self.assertEqual(payload["total"], "12.34")
        self.assertEqual(
            [series["label"] for series in payload["series"]],
            ["Food"],
        )

    def test_missing_source_transactions_are_excluded_from_spend(self):
        record = self.add_transaction(amount="-27.50", source_id="missing-source-row")
        record.source_state = Transaction.SourceState.MISSING
        record.save(update_fields=["source_state"])

        payload = self.get_payload()

        self.assertEqual(payload["total"], "0.00")
        self.assertEqual(payload["series"], [])

    def test_active_rules_override_default_in_priority_order(self):
        self.add_transaction(amount="-10.00", category="Food", source_id="food")
        self.add_transaction(
            amount="25.00",
            category="Reimbursement",
            transaction_type="Income",
            source_id="reimbursement",
        )
        SpendRule.objects.create(
            user=self.user,
            name="Include reimbursement",
            match_field=SpendRule.MatchField.CATEGORY,
            match_operator=SpendRule.MatchOperator.EQUALS,
            match_value="Reimbursement",
            action=SpendRule.Action.INCLUDE_IN_SPEND,
            priority=10,
        )
        SpendRule.objects.create(
            user=self.user,
            name="Exclude food",
            match_field=SpendRule.MatchField.CATEGORY,
            match_operator=SpendRule.MatchOperator.EQUALS,
            match_value="Food",
            action=SpendRule.Action.EXCLUDE_FROM_SPEND,
            priority=20,
        )
        SpendRule.objects.create(
            user=self.user,
            name="Later exclusion",
            match_field=SpendRule.MatchField.CATEGORY,
            match_operator=SpendRule.MatchOperator.EQUALS,
            match_value="Reimbursement",
            action=SpendRule.Action.EXCLUDE_FROM_SPEND,
            priority=30,
        )

        payload = self.get_payload()

        self.assertEqual(payload["total"], "25.00")
        self.assertEqual([series["label"] for series in payload["series"]], ["Reimbursement"])

    def test_refund_is_on_refund_date_but_uses_original_stack(self):
        purchase = self.add_transaction(
            date=self.today - timedelta(days=2),
            amount="-40.00",
            category="Electronics",
            source_id="original-purchase",
        )
        self.add_transaction(
            date=self.today,
            amount="15.00",
            category="Miscellaneous",
            transaction_type="Refund",
            source_id="refund-1",
            linked_id=purchase.source_transaction_id,
        )

        payload = self.get_payload(stack_by="category")

        self.assertEqual(payload["total"], "25.00")
        self.assertEqual([series["label"] for series in payload["series"]], ["Electronics"])
        today_bucket = payload["buckets"][-1]
        self.assertEqual(today_bucket["total"], "-15.00")
        self.assertEqual(today_bucket["segments"][0]["label"], "Electronics")
        self.assertEqual(today_bucket["segments"][0]["value"], "-15.00")

    def test_refund_drilldown_uses_original_category_and_account(self):
        purchase = self.add_transaction(
            date=self.today - timedelta(days=2),
            amount="-40.00",
            category="Electronics",
            source_id="original-purchase",
        )
        refund_account = FinancialAccount.objects.create(
            user=self.user,
            source_name="Refund card",
            display_name="Refund card",
            institution_name="Example Bank",
        )
        refund = self.add_transaction(
            account=refund_account,
            amount="15.00",
            category="Miscellaneous",
            transaction_type="Refund",
            source_id="refund-1",
            linked_id=purchase.source_transaction_id,
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("finance:spending_transactions_api"),
            {
                "start_date": self.today.isoformat(),
                "end_date": self.today.isoformat(),
                "stack_by": "category",
                "series_key": "Electronics",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        refund_row = response.json()["rows"][0]
        self.assertEqual(refund_row["id"], refund.pk)
        self.assertEqual(refund_row["contribution"], "-15.00")
        self.assertTrue(refund_row["is_refund"])
        self.assertEqual(refund_row["category"], "Electronics")
        self.assertEqual(refund_row["account"], "Everyday")

    def test_top_six_and_other_are_ranked_over_window(self):
        for index in range(7):
            self.add_transaction(
                amount=f"-{index + 1}.00",
                category=f"Category {index}",
                source_id=f"category-{index}",
            )

        payload = self.get_payload()

        self.assertEqual(len(payload["series"]), 7)
        self.assertEqual(payload["series"][0]["label"], "Category 6")
        self.assertEqual(payload["series"][-1]["label"], "Other")
        self.assertEqual(payload["series"][-1]["members"], ["Category 0"])

    def test_month_and_week_windows_are_seven_calendar_periods(self):
        week = build_window("week", self.today, self.today)
        month = build_window("month", self.today, self.today)
        self.assertEqual(len(week["buckets"]), 7)
        self.assertEqual(len(month["buckets"]), 7)
        self.assertEqual(week["buckets"][-1]["start"].weekday(), 0)
        self.assertEqual(week["buckets"][-1]["end"], self.today)
        self.assertEqual(month["buckets"][-1]["start"].day, 1)
        self.assertEqual(month["buckets"][-1]["end"], self.today)

    def test_future_date_is_rejected(self):
        future_date = self.today + timedelta(days=1)
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("finance:spending_api"), {"end_date": future_date.isoformat()}
        )
        self.assertEqual(response.status_code, 400)

    def test_transaction_drilldown_is_user_scoped_and_period_scoped(self):
        own = self.add_transaction(source_id="own", category="Food")
        self.add_transaction(user=self.other_user, source_id="other", category="Food")
        response = self.client.get(reverse("finance:spending_transactions_api"))
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("finance:spending_transactions_api"),
            {
                "start_date": self.today.isoformat(),
                "end_date": self.today.isoformat(),
                "stack_by": "category",
                "series_key": "Food",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row["id"] for row in response.json()["rows"]], [own.pk])
