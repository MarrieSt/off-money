from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.urls import reverse

from finance.models import (
    DailyStatus,
    FinancialAccount,
    SpendRule,
    Transaction,
)


User = get_user_model()


class ApplicationAccessTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="maria", password="test-password")
        self.other_user = User.objects.create_user(username="alex", password="test-password")

    def test_finance_pages_require_authentication(self):
        for name in (
            "finance:home",
            "finance:transactions",
            "finance:accounts",
            "finance:rules",
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertRedirects(
                    response,
                    f"{reverse('login')}?next={reverse(name)}",
                    fetch_redirect_response=False,
                )

    def test_authenticated_user_can_view_dashboard_shell(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("finance:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Overview")
        self.assertContains(response, "No transaction data has been imported yet")
        self.assertContains(response, "—")

    def test_health_endpoint_returns_success_without_database_queries(self):
        with self.assertNumQueries(0):
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "app": "money"})

    def test_login_page_uses_money_template(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in to Money")

    def test_user_can_log_in_and_log_out(self):
        login_response = self.client.post(
            reverse("login"), {"username": "maria", "password": "test-password"}
        )
        self.assertRedirects(login_response, reverse("finance:home"), fetch_redirect_response=False)

        logout_response = self.client.post(reverse("logout"))
        self.assertRedirects(logout_response, reverse("login"), fetch_redirect_response=False)

    def test_default_database_is_postgresql(self):
        self.assertEqual(connection.vendor, "postgresql")


class UserIsolationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="maria", password="test-password")
        other_user = User.objects.create_user(username="alex", password="test-password")
        account = FinancialAccount.objects.create(
            user=other_user,
            source_name="Hidden source account",
            display_name="Hidden account label",
            institution_name="Hidden institution",
        )
        Transaction.objects.create(
            user=other_user,
            account=account,
            transaction_date="2026-01-02",
            amount=Decimal("-12.34"),
            source_merchant="Private Merchant Name",
            source_category="Private Category",
        )
        SpendRule.objects.create(
            user=other_user,
            name="Private Rule Name",
            match_field=SpendRule.MatchField.MERCHANT,
            match_value="Private Merchant Name",
            action=SpendRule.Action.EXCLUDE_FROM_SPEND,
        )

    def test_user_cannot_see_another_users_accounts(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("finance:accounts"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Hidden account label")
        self.assertContains(response, "No accounts added")

    def test_user_cannot_see_another_users_transactions(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("finance:transactions"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Private Merchant Name")
        self.assertContains(response, "No transactions yet")

    def test_user_cannot_see_another_users_spend_rules(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("finance:rules"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Private Rule Name")
        self.assertContains(response, "No spend rules yet")


class FinanceModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="maria", password="test-password")
        self.account = FinancialAccount.objects.create(
            user=self.user,
            source_name="Current account",
            institution_name="Example Bank",
        )

    def test_external_transaction_id_is_unique_per_user_and_source(self):
        Transaction.objects.create(
            user=self.user,
            account=self.account,
            source_system="emma",
            source_transaction_id="external-123",
            transaction_date="2026-01-02",
            amount=Decimal("-12.34"),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Transaction.objects.create(
                    user=self.user,
                    account=self.account,
                    source_system="emma",
                    source_transaction_id="external-123",
                    transaction_date="2026-01-03",
                    amount=Decimal("-1.00"),
                )

        Transaction.objects.create(
            user=self.user,
            account=self.account,
            source_system="other-source",
            source_transaction_id="external-123",
            transaction_date="2026-01-03",
            amount=Decimal("-1.00"),
        )

    def test_external_transaction_id_may_be_missing_or_blank(self):
        for external_id in (None, ""):
            Transaction.objects.create(
                user=self.user,
                account=self.account,
                source_transaction_id=external_id,
                transaction_date="2026-01-02",
                amount=Decimal("1.00"),
            )
            Transaction.objects.create(
                user=self.user,
                account=self.account,
                source_transaction_id=external_id,
                transaction_date="2026-01-03",
                amount=Decimal("2.00"),
            )

    def test_daily_status_is_unique_per_user_and_date(self):
        DailyStatus.objects.create(user=self.user, date="2026-01-02")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DailyStatus.objects.create(user=self.user, date="2026-01-02")

    def test_transaction_preserves_decimal_precision(self):
        transaction_record = Transaction.objects.create(
            user=self.user,
            account=self.account,
            transaction_date="2026-01-02",
            amount=Decimal("1234.56"),
        )
        transaction_record.refresh_from_db()
        self.assertEqual(transaction_record.amount, Decimal("1234.56"))

    def test_transaction_account_must_belong_to_the_same_user(self):
        other_user = User.objects.create_user(username="alex", password="test-password")
        other_account = FinancialAccount.objects.create(
            user=other_user, source_name="Other current account"
        )
        record = Transaction(
            user=self.user,
            account=other_account,
            transaction_date="2026-01-02",
            amount=Decimal("1.00"),
        )
        with self.assertRaisesMessage(
            ValidationError, "The account must belong to the transaction user."
        ):
            record.full_clean()
