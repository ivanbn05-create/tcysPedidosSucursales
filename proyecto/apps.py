"""Registro del AdminSite MFA sin alterar los ModelAdmin existentes."""

from django.contrib.admin.apps import AdminConfig


class AdminSeguroConfig(AdminConfig):
    default_site = "proyecto.admin_site.AdminSeguro"
