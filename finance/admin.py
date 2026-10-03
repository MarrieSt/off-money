from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect
from django.urls import path, reverse

from .models import (
    DailyStatus,
    EmmaRawTransaction,
    EarnedReward,
    FinancialAccount,
    ImportRun,
    RewardDefinition,
    SpendRule,
    Transaction,
)
from money.services.emma_import import run_emma_import


admin.site.site_header = "Money administration"
admin.site.site_title = "Money Admin"
admin.site.index_title = "Data and configuration"


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


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    change_list_template = "admin/finance/importrun/change_list.html"
    list_display = (
        "started_at",
        "source",
        "user",
        "status",
        "rows_read",
        "rows_created",
        "rows_updated",
        "rows_skipped",
        "rows_failed",
        "finished_at",
    )
    list_filter = ("source", "status", "user")
    search_fields = ("error_message", "user__username")
    readonly_fields = (
        "source",
        "user",
        "status",
        "started_at",
        "finished_at",
        "rows_read",
        "rows_created",
        "rows_updated",
        "rows_skipped",
        "rows_failed",
        "error_message",
    )
    ordering = ("-started_at",)

    def has_add_permission(self, request):
        return False

    def get_urls(self):
        custom_urls = [
            path(
                "run-emma-import/",
                self.admin_site.admin_view(self.run_import_view),
                name="finance_importrun_run",
            )
        ]
        return custom_urls + super().get_urls()

    def run_import_view(self, request):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_change_permission(request):
            raise PermissionDenied
        try:
            run = run_emma_import()
        except Exception:
            self.message_user(
                request,
                "Emma import failed. Check the configured credentials and application logs.",
                level=messages.ERROR,
            )
        else:
            self.message_user(
                request,
                (
                    f"Emma import {run.status}: {run.rows_created} created, "
                    f"{run.rows_updated} updated, {run.rows_skipped} skipped, "
                    f"{run.rows_failed} failed."
                ),
                level=messages.WARNING if run.rows_failed else messages.SUCCESS,
            )
        return redirect(reverse("admin:finance_importrun_changelist"))


@admin.register(EmmaRawTransaction)
class EmmaRawTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "source_transaction_id",
        "source_system",
        "user",
        "source_row_number",
        "source_hash",
        "first_seen_at",
        "last_seen_at",
        "import_run",
    )
    list_filter = ("source_system", "user", "import_run")
    search_fields = ("source_transaction_id", "source_hash", "user__username")
    readonly_fields = (
        "user",
        "import_run",
        "source_system",
        "source_transaction_id",
        "source_row_number",
        "source_hash",
        "raw_data",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )
    ordering = ("-last_seen_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
