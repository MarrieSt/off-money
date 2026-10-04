from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from finance.analytics import behaviour_band, build_behaviour_calendar_payload, build_spending_payload
from finance.models import FinancialAccount, Transaction


class BehaviourCalendarLogicTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="calendar-user", password="password")
        self.account = FinancialAccount.objects.create(
            user=self.user,
            source_name="Everyday account",
            display_name="Everyday",
            institution_name="Example Bank",
        )

    def add_transaction(self, *, day, amount, category="Food", source_id=None, transaction_type="Purchase", linked_id=""):
        return Transaction.objects.create(
            user=self.user,
            account=self.account,
            source_system="emma",
            source_transaction_id=source_id,
            transaction_date=day,
            amount=Decimal(amount),
            currency="GBP",
            source_category=category,
            source_type=transaction_type,
            source_merchant="Cafe Example",
            source_linked_transaction_id=linked_id,
        )

    def test_behavior_band_boundaries(self):
        cases = (
            ("0.00", "zero"),
            ("0.01", "low"),
            ("50.00", "low"),
            ("50.01", "medium"),
            ("150.00", "medium"),
            ("150.01", "high"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(behaviour_band(Decimal(value))[0], expected)

    def test_calendar_current_month_has_every_day_future_neutral_and_monday_first_padding(self):
        today = date(2026, 10, 4)
        payload = build_behaviour_calendar_payload(self.user, date(2026, 10, 1), today)
        first_weekday, days_in_month = monthrange(2026, 10)
        self.assertEqual(first_weekday, 3)
        self.assertEqual(len(payload["days"]), days_in_month)
        self.assertEqual(payload["days"][0]["date"], "2026-10-01")
        self.assertTrue(payload["days"][3]["is_today"])
        self.assertTrue(all(day["is_future"] for day in payload["days"][4:]))
        self.assertTrue(all(day["total_spend"] is None for day in payload["days"][4:]))
        self.assertTrue(all(day["band"] == "future" for day in payload["days"][4:]))

    def test_calendar_handles_february_and_leap_year(self):
        normal = build_behaviour_calendar_payload(self.user, date(2025, 2, 1), date(2025, 3, 1))
        leap = build_behaviour_calendar_payload(self.user, date(2024, 2, 1), date(2024, 3, 1))
        self.assertEqual(len(normal["days"]), 28)
        self.assertEqual(len(leap["days"]), 29)

    def test_month_navigation_metadata_blocks_future_and_allows_history(self):
        today = date(2026, 10, 4)
        current = build_behaviour_calendar_payload(self.user, date(2026, 10, 1), today)
        historical = build_behaviour_calendar_payload(self.user, date(2026, 9, 1), today)
        self.assertTrue(current["is_current_month"])
        self.assertFalse(current["can_navigate_forward"])
        self.assertFalse(historical["is_current_month"])
        self.assertTrue(historical["can_navigate_forward"])
        with self.assertRaises(ValueError):
            build_behaviour_calendar_payload(self.user, date(2026, 11, 1), today)

    def test_calendar_daily_totals_match_chart_and_details_while_refunds_are_ignored(self):
        month = date(2026, 10, 1)
        today = date(2026, 10, 4)
        target_day = date(2026, 10, 2)
        self.add_transaction(day=target_day, amount="-22.50", category="Dining", source_id="calendar-1")
        self.add_transaction(day=target_day, amount="-18.00", category="Dining", source_id="calendar-2")
        self.add_transaction(day=today, amount="-7.25", category="Coffee", source_id="calendar-3")
        self.add_transaction(
            day=today,
            amount="12.00",
            category="Refund",
            source_id="calendar-refund",
            transaction_type="Refund",
            linked_id="calendar-1",
        )

        calendar_payload = build_behaviour_calendar_payload(self.user, month, today)
        chart_payload = build_spending_payload(self.user, "day", "category", target_day, today)
        detail = self.client.get(
            reverse("finance:behaviour_day_api"), {"date": target_day.isoformat()},
            HTTP_HOST="127.0.0.1",
        )

        calendar_day = calendar_payload["days"][target_day.day - 1]
        self.assertEqual(calendar_day["total_spend"], chart_payload["buckets"][-1]["total"])
        self.assertEqual(calendar_day["total_spend"], "40.50")
        self.assertEqual(len(calendar_day["bars"]), 2)
        self.assertEqual(sum(Decimal(bar["amount"]) for bar in calendar_day["bars"]), Decimal("40.50"))

        today_calendar_day = calendar_payload["days"][today.day - 1]
        today_chart_payload = build_spending_payload(self.user, "day", "category", today, today)
        self.assertEqual(today_calendar_day["total_spend"], "7.25")
        self.assertEqual(today_calendar_day["total_spend"], today_chart_payload["buckets"][-1]["total"])
        self.assertEqual(len(today_calendar_day["bars"]), 1)

        self.client.force_login(self.user)
        detail = self.client.get(reverse("finance:behaviour_day_api"), {"date": target_day.isoformat()})
        self.assertEqual(detail.status_code, 200)
        payload = detail.json()
        self.assertEqual(payload["total_spend"], "40.50")
        self.assertEqual(sum(Decimal(row["amount"]) for row in payload["transactions"]), Decimal(payload["total_spend"]))

        today_detail = self.client.get(reverse("finance:behaviour_day_api"), {"date": today.isoformat()})
        self.assertEqual(today_detail.status_code, 200)
        self.assertEqual(today_detail.json()["total_spend"], "7.25")
        self.assertEqual(len(today_detail.json()["transactions"]), 1)

    def test_zero_spend_day_is_green_empty_and_future_not_a_zero_day(self):
        today = timezone.localdate()
        payload = build_behaviour_calendar_payload(self.user, today.replace(day=1), today)
        today_item = payload["days"][today.day - 1]
        self.assertEqual(today_item["total_spend"], "0.00")
        self.assertEqual(today_item["band"], "zero")
        self.assertEqual(today_item["bars"], [])
        if today.day < len(payload["days"]):
            self.assertIsNone(payload["days"][today.day]["total_spend"])


class BehaviourCalendarEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="calendar-api", password="password")

    def test_calendar_requires_login_and_validates_month(self):
        url = reverse("finance:behaviour_calendar_api")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.user)
        bad_month = self.client.get(url, {"month": "2026-13"})
        self.assertEqual(bad_month.status_code, 400)

    def test_month_endpoint_aggregates_only_the_signed_in_users_transactions(self):
        today = timezone.localdate()
        own_account = FinancialAccount.objects.create(
            user=self.user, source_name="My account", institution_name="My bank"
        )
        Transaction.objects.create(
            user=self.user,
            account=own_account,
            transaction_date=today,
            amount=Decimal("-12.50"),
            currency="GBP",
            source_category="Food",
            source_type="Purchase",
        )
        other_user = get_user_model().objects.create_user(username="calendar-other", password="password")
        other_account = FinancialAccount.objects.create(
            user=other_user, source_name="Other account", institution_name="Other bank"
        )
        Transaction.objects.create(
            user=other_user,
            account=other_account,
            transaction_date=today,
            amount=Decimal("-99.00"),
            currency="GBP",
            source_category="Private",
            source_type="Purchase",
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("finance:behaviour_calendar_api"), {"month": today.strftime("%Y-%m")}
        )

        self.assertEqual(response.status_code, 200)
        day = response.json()["days"][today.day - 1]
        self.assertEqual(day["total_spend"], "12.50")
        self.assertEqual(day["transaction_count"], 1)
        self.assertEqual(day["bars"][0]["category"], "Food")

    def test_future_day_detail_is_rejected(self):
        self.client.force_login(self.user)
        future_day = timezone.localdate() + timedelta(days=1)
        response = self.client.get(reverse("finance:behaviour_day_api"), {"date": future_day.isoformat()})
        self.assertEqual(response.status_code, 400)
