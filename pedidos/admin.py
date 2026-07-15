from django.contrib import admin
from django.core.cache import cache

from .models import (
    CONFIGURACION_CACHE_KEY,
    Configuracion,
    ItemPedido,
    LogRecordatorio,
    Pedido,
    Precio,
    Producto,
    SucursalCliente,
)


@admin.register(SucursalCliente)
class SucursalClienteAdmin(admin.ModelAdmin):
    list_display = ("nombre", "tipo", "activa", "usuario", "email")
    list_filter = ("tipo", "activa")
    search_fields = ("nombre", "usuario__username", "email")


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "nombre_ticket", "orden", "activo")
    list_editable = ("nombre_ticket", "orden", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre", "nombre_ticket")


@admin.register(Precio)
class PrecioAdmin(admin.ModelAdmin):
    list_display = ("producto", "sucursal_cliente", "precio_unitario", "fecha_vigencia")
    list_filter = ("sucursal_cliente", "producto")
    search_fields = ("producto__nombre", "sucursal_cliente__nombre")


class ItemPedidoInline(admin.TabularInline):
    model = ItemPedido
    extra = 0
    readonly_fields = ("subtotal",)


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = ("id", "sucursal_cliente", "fecha_creacion", "estado", "total", "eliminado")
    list_filter = ("estado", "eliminado", "sucursal_cliente")
    search_fields = ("id", "sucursal_cliente__nombre", "usuario_nombre")
    readonly_fields = ("fecha_creacion", "fecha_confirmacion", "total")
    inlines = [ItemPedidoInline]


@admin.register(Configuracion)
class ConfiguracionAdmin(admin.ModelAdmin):
    """Vista técnica/depuración de la configuración. El uso de negocio vive en
    /admin/configuracion/ (ver pedidos/views.py::admin_configuracion)."""

    fieldsets = (
        (
            "Horario de pedidos",
            {"fields": ("hora_inicio_pedidos", "hora_fin_pedidos")},
        ),
        (
            "Recordatorios por correo",
            {
                "fields": (
                    "hora_envio_recordatorio",
                    "dias_recordatorio",
                    "recordatorios_habilitados",
                    "email_remitente",
                )
            },
        ),
        (
            "Auditoría",
            {"fields": ("actualizado_en", "actualizado_por"), "classes": ("collapse",)},
        ),
    )
    readonly_fields = ("actualizado_en", "actualizado_por")

    def save_model(self, request, obj, form, change):
        obj.actualizado_por = request.user.username
        super().save_model(request, obj, form, change)
        cache.delete(CONFIGURACION_CACHE_KEY)

    def has_add_permission(self, request):
        # Solo puede existir una configuración (patrón singleton).
        return not Configuracion.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LogRecordatorio)
class LogRecordatorioAdmin(admin.ModelAdmin):
    list_display = ("sucursal_cliente", "fecha_envio", "estado", "mensaje_error")
    list_filter = ("estado", "sucursal_cliente")
    search_fields = ("sucursal_cliente__nombre",)
    readonly_fields = ("sucursal_cliente", "fecha_envio", "estado", "mensaje_error")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
