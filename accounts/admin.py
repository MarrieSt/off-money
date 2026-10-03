from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import MoneyUser


@admin.register(MoneyUser)
class MoneyUserAdmin(UserAdmin):
    model = MoneyUser
