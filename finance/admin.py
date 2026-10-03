from django.contrib import admin

from .models import (
    DailyStatus,
    EarnedReward,
    FinancialAccount,
    RewardDefinition,
    SpendRule,
    Transaction,
)


@admin.register(FinancialAccount)
class FinancialAccountAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "source_name",
        "institution_name",
        "currency",
        "is_active",
        "include_in_analytics",
        "include_in_no_spend_tracking",
        "user",
    )
    list_filter = ("institution_name", "is_active", "include_in_analytics", "user")
    search_fields = ("source_name", "display_name", "institution_name")
    ordering = ("user", "sort_order", "display_name", "source_name")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_date",
        "amount",
        "currency",
        "account",
        "source_merchant",
        "source_counterparty",
        "source_category",
        "source_type",
        "user",
    )
    list_filter = ("account", "source_category", "source_type", "currency", "user")
    search_fields = (
        "source_merchant",
        "source_counterparty",
        "source_transaction_id",
        "source_notes",
    )
    date_hierarchy = "transaction_date"
    ordering = ("-transaction_date", "-id")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("user", "account", "transaction_date", "amount", "currency")}),
        (
            "Source identity",
            {"fields": ("source_system", "source_transaction_id", "source_created_at")},
        ),
        (
            "Source details",
            {
                "fields": (
                    "source_category",
                    "source_subcategory",
                    "source_type",
                    "source_tags",
                    "source_counterparty",
                    "source_custom_name",
                    "source_merchant",
                    "source_additional_details",
                    "source_notes",
                    "source_linked_transaction_id",
                )
            },
        ),
        ("Import metadata", {"fields": ("raw_data", "imported_at", "last_source_sync_at")}),
        ("Record metadata", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(SpendRule)
class SpendRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "match_field",
        "match_operator",
        "match_value",
        "action",
        "priority",
        "is_active",
        "user",
    )
    list_filter = ("match_field", "action", "is_active", "user")
    search_fields = ("name", "match_value")
    ordering = ("user", "priority", "name")


@admin.register(DailyStatus)
class DailyStatusAdmin(admin.ModelAdmin):
    list_display = (
        "date",
        "total_outflow",
        "qualifying_spend",
        "qualifying_transaction_count",
        "is_no_spend_day",
        "user",
    )
    list_filter = ("is_no_spend_day", "user")
    date_hierarchy = "date"
    ordering = ("-date",)


@admin.register(RewardDefinition)
class RewardDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "reward_type", "threshold", "is_active", "user")
    list_filter = ("reward_type", "is_active", "user")
    search_fields = ("name", "description")
    ordering = ("user", "name")


@admin.register(EarnedReward)
class EarnedRewardAdmin(admin.ModelAdmin):
    list_display = ("reward", "earned_on", "user")
    list_filter = ("earned_on", "user", "reward")
    date_hierarchy = "earned_on"
    ordering = ("-earned_on", "-created_at")
