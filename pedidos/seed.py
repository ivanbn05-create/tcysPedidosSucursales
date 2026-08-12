from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone
from django.utils.text import slugify

from .models import Configuracion, Precio, Producto, SucursalCliente


PRINT_GROUP_NAME = "Operador de impresion"
DEBUG_USER_USERNAME = "ivanprueba"
DEBUG_USER_PASSWORD = "prueba8989"
DEFAULT_REMINDER_FROM = "Los Tocayos <tocayos.tacos@gmail.com>"


PRODUCTOS_CATALOGO = [
    ("LITRO DE BARBACOA", "BARBACOA", "KILOGRAMO (KG)", "KG", "1.000"),
    ("TORTILLA ESPECIAL", "TORTILLA", "KILOGRAMO (KG)", "KG", "1.000"),
    ("BOLILLO", "BOLILLO", "PIEZA (PZA)", "PZA", "1.000"),
    ("QUESO", "QUESO", "KILOGRAMO (KG)", "KG", "1.000"),
    ("CEBOLLA BLANCA", "C. PICADA", "KILOGRAMO (KG)", "KG", "1.000"),
    ("CEBOLLA GUISADA", "C. GUISADA", "KILOGRAMO (KG)", "KG", "1.000"),
    ("CHILE GüERO", "CHILE", "PIEZA (PZA)", "PZA", "30.000"),
    ("SALSA DE TOMATE", "S. ROJA", "LITRO (LT)", "LT", "1.000"),
    ("SALSA DE AGUACATE", "S. AGUACATE", "LITRO (LT)", "LT", "1.000"),
    ("SALSA DE CHIPOTLE", "S. CHIPOTLE", "LITRO (LT)", "LT", "1.000"),
    ("SALSA DE SERRANO", "S. SERRANO", "LITRO (LT)", "LT", "1.000"),
    ("SALSA VERDE SIN CHILE", "S. VERDE S/CH", "LITRO (LT)", "LT", "1.000"),
    ("SALSA MEXICANA", "S. MEXICANA", "KILOGRAMO (KG)", "KG", "1.000"),
    ("SALSA HABANERO TATEMADO", "S. HABANERO", "LITRO (LT)", "LT", "1.000"),
    ("SALSA DE CACAHUATE", "S. CACAHUATE", "LITRO (LT)", "LT", "1.000"),
    ("CEBOLLA MORADA RAYADA", "C. MORADA RYD", "KILOGRAMO (KG)", "KG", "1.000"),
    ("PREPARADO CEBOLLA MORADA", "PREP. C. MORADA", "LITRO (LT)", "LT", "1.000"),
    ("PEPINO", "PEPINO", "KILOGRAMO (KG)", "KG", "1.000"),
    ("RÁBANO", "RÁBANO", "KILOGRAMO (KG)", "KG", "1.000"),
    ("LIMÓN", "LIMÓN", "KILOGRAMO (KG)", "KG", "1.000"),
    ("CILANTRO", "CILANTRO", "KILOGRAMO (KG)", "KG", "1.000"),
    ("GRASA", "GRASA", "LITRO (LT)", "LT", "1.000"),
    ("CONSOMÉ", "CONSOMÉ", "LITRO (LT)", "LT", "1.000"),
    ("BISTEK", "BISTEK", "KILOGRAMO (KG)", "KG", "1.000"),
    ("ARRACHERA", "ARRACHERA", "KILOGRAMO (KG)", "KG", "1.000"),
    ("CHORIZO", "CHORIZO", "KILOGRAMO (KG)", "KG", "1.000"),
    ("AGUA HORCHATA BLANCA 1/2", "HB 1/2", "PIEZA (PZA)", "PZA", "1.000"),
    ("AGUA HORCHATA BLANCA LT", "HB LT", "PIEZA (PZA)", "PZA", "1.000"),
    ("AGUA HORCHATA ROSA 1/2", "HR 1/2", "PIEZA (PZA)", "PZA", "1.000"),
    ("AGUA HORCHATA ROSA LT", "HR LT", "PIEZA (PZA)", "PZA", "1.000"),
    ("AGUA JAMAICA 1/2", "JAM 1/2", "PIEZA (PZA)", "PZA", "1.000"),
    ("AGUA JAMAICA LT", "JAM LT", "PIEZA (PZA)", "PZA", "1.000"),
    ("SERVILLETAS", "SERVILLETA", "PIEZA (PZA)", "PZA", "1.000"),
    ("VASO 8 TÉRMICO DART", "VASO 8 oz", "PIEZA (PZA)", "PZA", "1.000"),
    ("CUCHARA CHICA ECONÓMICA", "CUCHARA", "PIEZA (PZA)", "PZA", "1.000"),
    ("6x6 NEVADO 125 PZAS", "6x6", "PIEZA (PZA)", "PZA", "1.000"),
    ("7x7 LISO NEVADO 100 PZAS", "7x7", "PIEZA (PZA)", "PZA", "1.000"),
    ("HOAGIE REYMA", "HOAGIE", "PIEZA (PZA)", "PZA", "1.000"),
]

PRECIOS_POR_GRUPO = {
    "sucursal_general": {
        "LITRO DE BARBACOA": ("193", "BARBACOA"),
        "TORTILLA ESPECIAL": ("25.5", "TORTILLA"),
        "BOLILLO": ("9", "BOLILLO"),
        "QUESO": ("160", "QUESO"),
        "CEBOLLA BLANCA": ("52", "C. PICADA"),
        "CEBOLLA GUISADA": ("60", "C. GUISADA"),
        "CHILE GüERO": ("64", "CHILE"),
        "SALSA DE TOMATE": ("60", "S. ROJA"),
        "SALSA DE AGUACATE": ("60", "S. AGUACATE"),
        "SALSA DE CHIPOTLE": ("56", "S. CHIPOTLE"),
        "SALSA DE SERRANO": ("56", "S. SERRANO"),
        "SALSA VERDE SIN CHILE": ("55", "S. VERDE S/CH"),
        "SALSA MEXICANA": ("45", "S. MEXICANA"),
        "SALSA HABANERO TATEMADO": ("55", "S. HABANERO"),
        "SALSA DE CACAHUATE": ("70", "S. CACAHUATE"),
        "CEBOLLA MORADA RAYADA": ("43", "C. MORADA RYD"),
        "PREPARADO CEBOLLA MORADA": ("27", "PREP. C. MORADA"),
        "PEPINO": ("30", "PEPINO"),
        "RÁBANO": ("30", "RÁBANO"),
        "LIMÓN": ("13", "LIMÓN"),
        "CILANTRO": ("200", "CILANTRO"),
        "GRASA": ("20", "GRASA"),
        "CONSOMÉ": ("0", "CONSOMÉ"),
        "BISTEK": ("220", "BISTEK"),
        "ARRACHERA": ("220", "ARRACHERA"),
        "CHORIZO": ("125", "CHORIZO"),
        "AGUA HORCHATA ROSA 1/2": ("19", "HR 1/2"),
        "AGUA HORCHATA ROSA LT": ("32", "HR LT"),
        "AGUA HORCHATA BLANCA 1/2": ("19", "HB 1/2"),
        "AGUA HORCHATA BLANCA LT": ("32", "HB LT"),
        "AGUA JAMAICA 1/2": ("19", "JAM 1/2"),
        "AGUA JAMAICA LT": ("32", "JAM LT"),
        "SERVILLETAS": ("39", "SERVILLETA"),
        "VASO 8 TÉRMICO DART": ("16.5", "VASO 8 oz"),
        "CUCHARA CHICA ECONÓMICA": ("9.5", "CUCHARA"),
        "6x6 NEVADO 125 PZAS": ("108", "6x6"),
        "7x7 LISO NEVADO 100 PZAS": ("170", "7x7"),
        "HOAGIE REYMA": ("151", "HOAGIE"),
    },
    "aguilas": {
        "LITRO DE BARBACOA": ("193", "BARBACOA"),
        "TORTILLA ESPECIAL": ("25.5", "TORTILLA"),
        "BOLILLO": ("9", "BOLILLO"),
        "QUESO": ("160", "QUESO"),
        "CEBOLLA BLANCA": ("52", "C. PICADA"),
        "CEBOLLA GUISADA": ("60", "C. GUISADA"),
        "CHILE GüERO": ("64", "CHILE"),
        "SALSA DE TOMATE": ("60", "S. ROJA"),
        "SALSA DE AGUACATE": ("60", "S. AGUACATE"),
        "SALSA DE CHIPOTLE": ("56", "S. CHIPOTLE"),
        "SALSA DE SERRANO": ("56", "S. SERRANO"),
        "SALSA VERDE SIN CHILE": ("55", "S. VERDE S/CH"),
        "SALSA MEXICANA": ("45", "S. MEXICANA"),
        "SALSA HABANERO TATEMADO": ("55", "S. HABANERO"),
        "SALSA DE CACAHUATE": ("70", "S. CACAHUATE"),
        "CEBOLLA MORADA RAYADA": ("43", "C. MORADA RYD"),
        "PREPARADO CEBOLLA MORADA": ("27", "PREP. C. MORADA"),
        "PEPINO": ("30", "PEPINO"),
        "RÁBANO": ("30", "RÁBANO"),
        "LIMÓN": ("13", "LIMÓN"),
        "CILANTRO": ("200", "CILANTRO"),
        "GRASA": ("20", "GRASA"),
        "CONSOMÉ": ("0", "CONSOMÉ"),
        "BISTEK": ("200", "BISTEK"),
        "ARRACHERA": ("220", "ARRACHERA"),
        "CHORIZO": ("125", "CHORIZO"),
        "AGUA HORCHATA ROSA 1/2": ("19", "HR 1/2"),
        "AGUA HORCHATA ROSA LT": ("32", "HR LT"),
        "AGUA HORCHATA BLANCA 1/2": ("19", "HB 1/2"),
        "AGUA HORCHATA BLANCA LT": ("32", "HB LT"),
        "AGUA JAMAICA 1/2": ("19", "JAM 1/2"),
        "AGUA JAMAICA LT": ("32", "JAM LT"),
        "SERVILLETAS": ("39", "SERVILLETA"),
        "VASO 8 TÉRMICO DART": ("16.5", "VASO 8 oz"),
        "CUCHARA CHICA ECONÓMICA": ("9.5", "CUCHARA"),
        "6x6 NEVADO 125 PZAS": ("108", "6x6"),
        "7x7 LISO NEVADO 100 PZAS": ("170", "7x7"),
        "HOAGIE REYMA": ("151", "HOAGIE"),
    },
    "mayoreo": {
        "LITRO DE BARBACOA": ("203", "BARBACOA .M"),
        "TORTILLA ESPECIAL": ("26.5", "TORTILLA .M"),
        "BOLILLO": ("9", "BOLILLO"),
        "QUESO": ("160", "QUESO"),
        "CEBOLLA BLANCA": ("52", "C. PICADA"),
        "CEBOLLA GUISADA": ("60", "C. GUISADA"),
        "CHILE GüERO": ("64", "CHILE"),
        "SALSA DE TOMATE": ("60", "S. ROJA"),
        "SALSA DE AGUACATE": ("60", "S. AGUACATE"),
        "SALSA DE CHIPOTLE": ("56", "S. CHIPOTLE"),
        "SALSA DE SERRANO": ("56", "S. SERRANO"),
        "SALSA VERDE SIN CHILE": ("55", "S. VERDE S/CH"),
        "SALSA MEXICANA": ("45", "S. MEXICANA"),
        "SALSA HABANERO TATEMADO": ("55", "S. HABANERO"),
        "SALSA DE CACAHUATE": ("70", "S. CACAHUATE"),
        "CEBOLLA MORADA RAYADA": ("43", "C. MORADA RYD"),
        "PREPARADO CEBOLLA MORADA": ("27", "PREP. C. MORADA"),
        "PEPINO": ("30", "PEPINO"),
        "RÁBANO": ("30", "RÁBANO"),
        "LIMÓN": ("13", "LIMÓN"),
        "CILANTRO": ("200", "CILANTRO"),
        "GRASA": ("30", "GRASA .M"),
        "CONSOMÉ": ("15", "CONSOMÉ .M"),
        "BISTEK": ("220", "BISTEK"),
        "AGUA HORCHATA ROSA 1/2": ("19", "HR 1/2 .M"),
        "AGUA HORCHATA ROSA LT": ("32", "HR LT .M"),
        "AGUA HORCHATA BLANCA 1/2": ("19", "HB 1/2 .M"),
        "AGUA HORCHATA BLANCA LT": ("32", "HB LT .M"),
        "AGUA JAMAICA 1/2": ("19", "JAM 1/2 .M"),
        "AGUA JAMAICA LT": ("32", "JAM LT .M"),
        "SERVILLETAS": ("39", "SERVILLETA"),
        "VASO 8 TÉRMICO DART": ("16.5", "VASO 8 oz"),
        "CUCHARA CHICA ECONÓMICA": ("9.5", "CUCHARA"),
        "6x6 NEVADO 125 PZAS": ("108", "6x6"),
        "7x7 LISO NEVADO 100 PZAS": ("170", "7x7"),
        "HOAGIE REYMA": ("151", "HOAGIE"),
    },
}

CLIENTES_DEMO = [
    ("Aguilas", SucursalCliente.Tipo.SUCURSAL, "aguilas"),
    ("Fortin", SucursalCliente.Tipo.SUCURSAL, "sucursal_general"),
    ("Estancia", SucursalCliente.Tipo.SUCURSAL, "sucursal_general"),
    ("Eventos MO", SucursalCliente.Tipo.SUCURSAL, "sucursal_general"),
    ("Plaza del Sol", SucursalCliente.Tipo.SUCURSAL, "sucursal_general"),
    ("Santa Anita", SucursalCliente.Tipo.SUCURSAL, "sucursal_general"),
    ("Brot Nueva Galicia", SucursalCliente.Tipo.CLIENTE_MAYORISTA, "mayoreo"),
    ("Brot CAT", SucursalCliente.Tipo.CLIENTE_MAYORISTA, "mayoreo"),
    ("Rakebela", SucursalCliente.Tipo.CLIENTE_MAYORISTA, "mayoreo"),
    ("Eventos Edgar", SucursalCliente.Tipo.CLIENTE_MAYORISTA, "mayoreo"),
]

CLIENTE_PASSWORD_SUFFIXES = {
    "Aguilas": "8445",
    "Fortin": "9481",
    "Estancia": "7608",
    "Eventos MO": "6924",
    "Plaza del Sol": "3186",
    "Santa Anita": "5702",
    "Brot Nueva Galicia": "0846",
    "Brot CAT": "7721",
    "Rakebela": "4349",
    "Eventos Edgar": "4437",
}


def username_for_name(nombre):
    return slugify(nombre).replace("-", "_")


def password_for_cliente(nombre):
    return f"{nombre}{CLIENTE_PASSWORD_SUFFIXES[nombre]}"


def ensure_password(user, password):
    if not user.has_usable_password():
        user.set_password(password)


def ensure_demo_price(producto, sucursal, precio_unitario, nombre_ticket, fecha_vigencia):
    """Crea historial de precios solo cuando el valor vigente cambia."""

    precio_unitario = Decimal(precio_unitario).quantize(Decimal("0.01"))
    nombre_ticket = nombre_ticket[:24]
    precio_vigente = (
        Precio.objects.filter(
            producto=producto,
            sucursal_cliente=sucursal,
            fecha_vigencia__lte=fecha_vigencia,
        )
        .order_by("-fecha_vigencia")
        .first()
    )
    if (
        precio_vigente is not None
        and precio_vigente.precio_unitario == precio_unitario
        and precio_vigente.nombre_ticket == nombre_ticket
    ):
        return precio_vigente

    precio_hoy = Precio.objects.filter(
        producto=producto,
        sucursal_cliente=sucursal,
        fecha_vigencia=fecha_vigencia,
    ).first()
    if precio_hoy is not None:
        precio_hoy.precio_unitario = precio_unitario
        precio_hoy.nombre_ticket = nombre_ticket
        precio_hoy.save(update_fields=["precio_unitario", "nombre_ticket"])
        return precio_hoy

    return Precio.objects.create(
        producto=producto,
        sucursal_cliente=sucursal,
        fecha_vigencia=fecha_vigencia,
        precio_unitario=precio_unitario,
        nombre_ticket=nombre_ticket,
    )


def seed_demo_data():
    """Crea datos demo de Los Tocayos de forma idempotente."""

    User = get_user_model()

    admin = User.objects.filter(username="juancarlos").first()
    legacy_admin = User.objects.filter(username="admin", is_staff=True).first()
    if admin is None:
        admin = legacy_admin or User(username="juancarlos")
    admin.username = "juancarlos"
    admin.is_staff = True
    admin.is_superuser = True
    admin.is_active = True
    admin.first_name = "Juan Carlos"
    admin.set_password("TocayosMO2026")
    admin.save()
    User.objects.filter(username="admin").exclude(pk=admin.pk).update(
        is_staff=False,
        is_superuser=False,
        is_active=False,
    )

    print_group, _ = Group.objects.get_or_create(name=PRINT_GROUP_NAME)
    printer_user, _ = User.objects.get_or_create(username="juanmanuel")
    printer_user.first_name = "Juan Manuel"
    printer_user.is_staff = False
    printer_user.is_superuser = False
    printer_user.is_active = True
    printer_user.set_password("imprimir")
    printer_user.save()
    printer_user.groups.add(print_group)

    debug_user, _ = User.objects.get_or_create(username=DEBUG_USER_USERNAME)
    debug_user.first_name = "Ivan Prueba"
    debug_user.is_staff = True
    debug_user.is_superuser = True
    debug_user.is_active = True
    debug_user.set_password(DEBUG_USER_PASSWORD)
    debug_user.save()

    productos = {}
    for orden, (
        nombre,
        nombre_ticket,
        unidad_medida,
        unidad_abreviatura,
        cantidad_por_precio,
    ) in enumerate(PRODUCTOS_CATALOGO, start=1):
        producto, _ = Producto.objects.update_or_create(
            nombre=nombre,
            defaults={
                "nombre_ticket": nombre_ticket,
                "unidad_medida": unidad_medida,
                "unidad_abreviatura": unidad_abreviatura,
                "cantidad_por_precio": Decimal(cantidad_por_precio),
                "orden": orden,
                "activo": True,
            },
        )
        productos[nombre] = producto

    Producto.objects.exclude(nombre__in=productos.keys()).update(activo=False)

    hoy = timezone.localdate()
    for nombre, tipo, grupo_precio in CLIENTES_DEMO:
        username = username_for_name(nombre)
        user, user_created = User.objects.get_or_create(username=username)
        user.first_name = nombre
        user.is_staff = False
        user.is_superuser = False
        user.is_active = True
        if user_created or not user.has_usable_password():
            user.set_password(password_for_cliente(nombre))
        user.save()

        sucursal, _ = SucursalCliente.objects.update_or_create(
            nombre=nombre,
            defaults={"tipo": tipo, "activa": True, "usuario": user},
        )

        precios_grupo = PRECIOS_POR_GRUPO[grupo_precio]
        for nombre_producto, producto in productos.items():
            if nombre_producto not in precios_grupo:
                continue
            precio_unitario, nombre_ticket = precios_grupo[nombre_producto]
            ensure_demo_price(producto, sucursal, precio_unitario, nombre_ticket, hoy)

    config = Configuracion.get_solo()
    if not config.email_remitente:
        config.email_remitente = DEFAULT_REMINDER_FROM
        config.save(update_fields=["email_remitente"])

    return {
        "usuarios": User.objects.count(),
        "sucursales_clientes": SucursalCliente.objects.count(),
        "productos": Producto.objects.count(),
        "precios": Precio.objects.count(),
    }
