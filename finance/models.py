from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class FinancialAccount(TimeStampedModel):
    SOURCE_EMMA = "emma"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="financial_accounts"
    )
    source_system = models.CharField(max_length=50, default=SOURCE_EMMA)
    source_name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True)
    institution_name = models.CharField(max_length=255, blank=True)
    account_type = models.CharField(max_length=100, blank=True)
    currency = models.CharField(max_length=3, default="GBP")
    is_active = models.BooleanField(default=True)
    include_in_analytics = models.BooleanField(default=True)
    include_in_no_spend_tracking = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "display_name", "source_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "source_system", "source_name", "institution_name"],
                name="uniq_account_user_source_name_institution",
            )
        ]
        indexes = [models.Index(fields=["user", "is_active"])]

    def __str__(self):
        return self.display_name or self.source_name


class Transaction(TimeStampedModel):
    SOURCE_EMMA = "emma"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="transactions"
    )
    account = models.ForeignKey(
        FinancialAccount, on_delete=models.PROTECT, related_name="transactions"
    )
    source_system = models.CharField(max_length=50, default=SOURCE_EMMA)
    source_transaction_id = models.CharField(max_length=255, null=True, blank=True)
    transaction_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="GBP")

    source_category = models.CharField(max_length=255, blank=True)
    source_subcategory = models.CharField(max_length=255, blank=True)
    source_type = models.CharField(max_length=100, blank=True)
    source_tags = models.TextField(blank=True)
    source_counterparty = models.CharField(max_length=255, blank=True)
    source_custom_name = models.CharField(max_length=255, blank=True)
    source_merchant = models.CharField(max_length=255, blank=True)
    source_additional_details = models.TextField(blank=True)
    source_notes = models.TextField(blank=True)
    source_linked_transaction_id = models.CharField(max_length=255, blank=True)

    raw_data = models.JSONField(default=dict, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    imported_at = models.DateTimeField(null=True, blank=True)
    last_source_sync_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-transaction_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "source_system", "source_transaction_id"],
                condition=Q(source_transaction_id__isnull=False)
                & ~Q(source_transaction_id=""),
                name="uniq_transaction_user_source_external_id",
            )
        ]
        indexes = [
            models.Index(fields=["user", "transaction_date"]),
            models.Index(fields=["account", "transaction_date"]),
            models.Index(fields=["user", "source_category", "transaction_date"]),
        ]

    def clean(self):
        super().clean()
        if self.account_id and self.user_id and self.account.user_id != self.user_id:
            raise ValidationError({"account": "The account must belong to the transaction user."})

    def __str__(self):
        description = self.source_merchant or self.source_counterparty or self.account
        return f"{self.transaction_date}: {description} ({self.amount} {self.currency})"


class SpendRule(TimeStampedModel):
    class Scope(models.TextChoices):
        SPEND = "spend", "Spend"
        NO_SPEND = "no_spend", "No-spend day"
        BOTH = "both", "Both"

    class MatchField(models.TextChoices):
        ACCOUNT = "account", "Account"
        CATEGORY = "category", "Category"
        SUBCATEGORY = "subcategory", "Subcategory"
        TYPE = "type", "Type"
        MERCHANT = "merchant", "Merchant"
        COUNTERPARTY = "counterparty", "Counterparty"

    class MatchOperator(models.TextChoices):
        EQUALS = "equals", "Equals"
        CONTAINS = "contains", "Contains"
        STARTS_WITH = "starts_with", "Starts with"

    class Action(models.TextChoices):
        INCLUDE_IN_SPEND = "include_spend", "Include in spend"
        EXCLUDE_FROM_SPEND = "exclude_spend", "Exclude from spend"
        INCLUDE_IN_NO_SPEND = "include_no_spend", "Include against no-spend day"
        EXCLUDE_FROM_NO_SPEND = "exclude_no_spend", "Exclude from no-spend day"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="spend_rules"
    )
    name = models.CharField(max_length=150)
    rule_scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.BOTH)
    match_field = models.CharField(max_length=20, choices=MatchField.choices)
    match_operator = models.CharField(
        max_length=20, choices=MatchOperator.choices, default=MatchOperator.EQUALS
    )
    match_value = models.CharField(max_length=255)
    action = models.CharField(max_length=30, choices=Action.choices)
    priority = models.PositiveIntegerField(default=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "name"]
        indexes = [models.Index(fields=["user", "is_active", "priority"])]

    def __str__(self):
        return self.name


class DailyStatus(TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="daily_statuses"
    )
    date = models.DateField()
    total_outflow = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    qualifying_spend = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    qualifying_transaction_count = models.PositiveIntegerField(null=True, blank=True)
    is_no_spend_day = models.BooleanField(null=True, blank=True)
    calculation_version = models.CharField(max_length=50, blank=True)
    calculated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["user", "date"], name="uniq_daily_status_user_date")
        ]

    def __str__(self):
        return f"{self.user} - {self.date}"


class RewardDefinition(TimeStampedModel):
    class RewardType(models.TextChoices):
        STREAK = "streak", "No-spend streak"
        MONTHLY_COUNT = "monthly_count", "Monthly no-spend days"
        PERSONAL_RECORD = "personal_record", "Personal record"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reward_definitions"
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    reward_type = models.CharField(max_length=30, choices=RewardType.choices)
    threshold = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class EarnedReward(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="earned_rewards"
    )
    reward = models.ForeignKey(
        RewardDefinition, on_delete=models.PROTECT, related_name="earned_rewards"
    )
    earned_on = models.DateField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-earned_on", "-created_at"]

    def clean(self):
        super().clean()
        if self.reward_id and self.user_id and self.reward.user_id != self.user_id:
            raise ValidationError({"reward": "The reward definition must belong to this user."})

    def __str__(self):
        return f"{self.reward} - {self.earned_on}"
