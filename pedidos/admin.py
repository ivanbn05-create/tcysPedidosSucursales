from django.contrib import admin
from django.core.cache import cache
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import (
    CONFIGURACION_CACHE_KEY,
    Configuracion,
    EventoCliente,
    ItemPedido,
    LogRecordatorio,
    MacroPedido,
    Pedido,
    Precio,
    Producto,
    SucursalCliente,
    SesionActiva,
)


# El admin del plugin permite crear/cambiar semillas y marcar dispositivos como
# confirmados. La recuperación/enrolamiento debe pasar por las vistas MFA.
if admin.site.is_registered(TOTPDevice):
    admin.site.unregister(TOTPDevice)


class AuditoriaSuperusuarioAdmin(admin.ModelAdmin):
    """Datos técnicos de seguridad: sólo un superusuario puede verlos."""

    def has_module_permission(self, request):
        return bool(request.user.is_active and request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return bool(
            request.user.is_active
            and request.user.is_superuser
            and super().has_view_permission(request, obj)
        )


@admin.register(SucursalCliente)
class SucursalClienteAdmin(admin.ModelAdmin):
    list_display = ("nombre", "tipo", "activa", "usuario", "email")
    list_filter = ("tipo", "activa")
    search_fields = ("nombre", "usuario__username", "email")


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = (
        "nombre",
        "nombre_ticket",
        "unidad_abreviatura",
        "cantidad_por_precio",
        "orden",
        "activo",
    )
    list_editable = (
        "nombre_ticket",
        "unidad_abreviatura",
        "cantidad_por_precio",
        "orden",
        "activo",
    )
    list_filter = ("activo", "unidad_abreviatura")
    search_fields = ("nombre", "nombre_ticket")


@admin.register(Precio)
class PrecioAdmin(admin.ModelAdmin):
    list_display = (
        "producto",
        "sucursal_cliente",
        "precio_unitario",
        "nombre_ticket",
        "fecha_vigencia",
    )
    list_filter = ("sucursal_cliente", "producto")
    search_fields = ("producto__nombre", "nombre_ticket", "sucursal_cliente__nombre")


class ItemPedidoInline(admin.TabularInline):
    model = ItemPedido
    extra = 0
    readonly_fields = ("subtotal",)


@admin.register(MacroPedido)
class MacroPedidoAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "sucursal_cliente",
        "fecha_pedido",
        "ultima_confirmacion",
        "estado",
        "total",
        "eliminado",
    )
    list_filter = ("estado", "eliminado", "sucursal_cliente", "fecha_pedido")
    search_fields = ("sucursal_cliente__nombre", "codigo_publico")
    readonly_fields = (
        "codigo_publico",
        "fecha_creacion",
        "fecha_actualizacion",
        "ultima_confirmacion",
        "total",
    )


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "macropedido",
        "sucursal_cliente",
        "fecha_creacion",
        "estado",
        "total",
        "eliminado",
    )
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
class LogRecordatorioAdmin(AuditoriaSuperusuarioAdmin):
    list_display = ("sucursal_cliente_id", "fecha_envio", "estado")
    list_filter = ("estado",)
    fields = ("sucursal_cliente", "fecha_envio", "estado")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SesionActiva)
class SesionActivaAdmin(AuditoriaSuperusuarioAdmin):
    list_display = ("usuario", "iniciada_en", "ultima_actividad")
    fields = ("usuario", "iniciada_en", "ultima_actividad")
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EventoCliente)
class EventoClienteAdmin(AuditoriaSuperusuarioAdmin):
    list_display = (
        "recibido_en",
        "evento",
        "sucursal_cliente",
    )
    list_filter = ("evento",)
    fields = (
        "evento_id",
        "sucursal_cliente",
        "evento",
        "recibido_en",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
