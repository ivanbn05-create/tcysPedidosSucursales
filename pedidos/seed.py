from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify

from .models import Configuracion, Precio, Producto, SucursalCliente


PRODUCTOS_DEMO = [
    ("Barbacoa", "BARBACOA", "Carne de barbacoa lista para venta."),
    ("Tortilla", "TORTILLA", "Tortilla para servicio."),
    ("Cebolla Guisada", "CEBOLLA GUISADA", "Cebolla preparada."),
    ("Chile Toreado", "CHILE TOREADO", "Chile toreado preparado."),
    ("Salsa Verde", "SALSA VERDE", "Salsa verde de la casa."),
    ("Salsa Roja", "SALSA ROJA", "Salsa roja de la casa."),
]

CLIENTES_DEMO = [
    ("Aguilas", SucursalCliente.Tipo.SUCURSAL, Decimal("1.00")),
    ("Fortin", SucursalCliente.Tipo.SUCURSAL, Decimal("1.00")),
    ("Estancia", SucursalCliente.Tipo.SUCURSAL, Decimal("1.00")),
    ("Brot Nueva Galicia", SucursalCliente.Tipo.CLIENTE_MAYORISTA, Decimal("2.00")),
    ("Brot CAT", SucursalCliente.Tipo.CLIENTE_MAYORISTA, Decimal("2.00")),
    ("Rakebela", SucursalCliente.Tipo.CLIENTE_MAYORISTA, Decimal("2.00")),
]


def username_for_name(nombre):
    return slugify(nombre).replace("-", "_")


def seed_demo_data():
    """Crea usuarios, productos y precios idempotentes para la demo."""

    User = get_user_model()

    admin, _ = User.objects.get_or_create(username="admin")
    admin.is_staff = True
    admin.is_superuser = True
    admin.first_name = "Admin"
    admin.set_password("admin123")
    admin.save()

    productos = []
    for orden, (nombre, nombre_ticket, descripcion) in enumerate(PRODUCTOS_DEMO, start=1):
        producto, _ = Producto.objects.update_or_create(
            nombre=nombre,
            defaults={
                "nombre_ticket": nombre_ticket,
                "descripcion": descripcion,
                "orden": orden,
                "activo": True,
            },
        )
        productos.append(producto)

    hoy = timezone.localdate()
    for nombre, tipo, precio_unitario in CLIENTES_DEMO:
        username = username_for_name(nombre)
        user, _ = User.objects.get_or_create(username=username)
        user.first_name = nombre
        user.is_staff = False
        user.is_superuser = False
        user.set_password(nombre)
        user.save()

        sucursal, _ = SucursalCliente.objects.update_or_create(
            nombre=nombre,
            defaults={"tipo": tipo, "activa": True, "usuario": user},
        )

        for producto in productos:
            Precio.objects.update_or_create(
                producto=producto,
                sucursal_cliente=sucursal,
                fecha_vigencia=hoy,
                defaults={"precio_unitario": precio_unitario},
            )

    # Asegura que exista el registro único de Configuracion (horarios de pedidos
    # y recordatorios). No se tocan correos de sucursales/clientes aquí: quedan
    # en blanco hasta que un admin real los capture, para no arriesgar el envío
    # de recordatorios a direcciones de prueba en un ambiente de demo/staging.
    Configuracion.get_solo()

    return {
        "usuarios": User.objects.count(),
        "sucursales_clientes": SucursalCliente.objects.count(),
        "productos": Producto.objects.count(),
        "precios": Precio.objects.count(),
    }
