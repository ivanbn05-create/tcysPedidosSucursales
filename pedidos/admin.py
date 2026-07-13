from django.contrib import admin

from .models import ItemPedido, Pedido, Precio, Producto, SucursalCliente


@admin.register(SucursalCliente)
class SucursalClienteAdmin(admin.ModelAdmin):
    list_display = ("nombre", "tipo", "activa", "usuario")
    list_filter = ("tipo", "activa")
    search_fields = ("nombre", "usuario__username")


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
