from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify

from .models import Configuracion, Precio, Producto, SucursalCliente


PRODUCTOS_CATALOGO = [
    ("LITRO DE BARBACOA", "BARBACOA", "KILOGRAMO (KG)", "KG", "1.000", True),
    ("TORTILLA ESPECIAL", "TORTILLA", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("BOLILLO", "BOLILLO", "PIEZA (PZA)", "PZA", "1.000", False),
    ("QUESO", "QUESO", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("CEBOLLA BLANCA", "C. PICADA", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("CEBOLLA GUISADA", "C. GUISADA", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("CHILE GüERO", "CHILE", "PIEZA (PZA)", "PZA", "30.000", False),
    ("SALSA DE TOMATE", "S. ROJA", "LITRO (LT)", "LT", "1.000", False),
    ("SALSA DE AGUACATE", "S. VERDE", "LITRO (LT)", "LT", "1.000", False),
    ("SALSA DE CHIPOTLE", "S. CHIPOTLE", "LITRO (LT)", "LT", "1.000", False),
    ("SALSA DE SERRANO", "S. SERRANI", "LITRO (LT)", "LT", "1.000", False),
    ("SALSA VERDE SIN CHILE", "S. VERDE S/CH", "LITRO (LT)", "LT", "1.000", False),
    ("SALSA MEXICANA", "S. MEXICANA", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("SALSA HABANERO TATEMADO", "S. HABANERO", "LITRO (LT)", "LT", "1.000", False),
    ("SALSA DE CACAHUATE", "S. CACAHUATE", "LITRO (LT)", "LT", "1.000", False),
    ("CEBOLLA MORADA RAYADA", "C. MORADA RYD", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("PREPARADO CEBOLLA MORADA", "PREP. C. MORADA", "LITRO (LT)", "LT", "1.000", False),
    ("PEPINO", "PEPINO", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("RÁBANO", "RÁBANO", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("LIMÓN", "LIMÓN", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("CILANTRO", "CILANTRO", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("GRASA", "GRASA", "LITRO (LT)", "LT", "1.000", False),
    ("CONSOMÉ", "CONSOMÉ", "LITRO (LT)", "LT", "1.000", False),
    ("BISTEK", "BISTEK", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("ARRACHERA", "ARRACHERA", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("CHORIZO", "CHORIZO", "KILOGRAMO (KG)", "KG", "1.000", False),
    ("AGUA HORCHATA ROSA 1/2", "HR 1/2", "PIEZA (PZA)", "PZA", "1.000", False),
    ("AGUA HORCHATA ROSA LT", "HR LT", "PIEZA (PZA)", "PZA", "1.000", False),
    ("AGUA HORCHATA BLANCA 1/2", "HB 1/2", "PIEZA (PZA)", "PZA", "1.000", False),
    ("AGUA HORCHATA BLANCA LT", "HB LT", "PIEZA (PZA)", "PZA", "1.000", False),
    ("AGUA JAMAICA 1/2", "JAM 1/2", "PIEZA (PZA)", "PZA", "1.000", False),
    ("AGUA JAMAICA LT", "JAM LT", "PIEZA (PZA)", "PZA", "1.000", False),
    ("SERVILLETAS", "SERVILLETA", "PIEZA (PZA)", "PZA", "1.000", False),
    ("VASO 8 TÉRMICO DART", "VASO 8 oz", "PIEZA (PZA)", "PZA", "1.000", False),
    ("CUCHARA CHICA ECONÓMICA", "CUCHARA", "PIEZA (PZA)", "PZA", "1.000", False),
    ("6x6 NEVADO 125 PZAS", "6x6", "PIEZA (PZA)", "PZA", "1.000", False),
    ("7x7 LISO NEVADO 100 PZAS", "7x7", "PIEZA (PZA)", "PZA", "1.000", False),
    ("HOAGIE REYMA", "HOAGIE", "PIEZA (PZA)", "PZA", "1.000", False),
]

PRECIOS_POR_GRUPO = {
    "sucursal_general": {
        "LITRO DE BARBACOA": ("178", "BARBACOA"),
        "TORTILLA ESPECIAL": ("25.5", "TORTILLA"),
        "BOLILLO": ("9", "BOLILLO"),
        "QUESO": ("160", "QUESO"),
        "CEBOLLA BLANCA": ("52", "C. PICADA"),
        "CEBOLLA GUISADA": ("60", "C. GUISADA"),
        "CHILE GüERO": ("64", "CHILE"),
        "SALSA DE TOMATE": ("60", "S. ROJA"),
        "SALSA DE AGUACATE": ("60", "S. VERDE"),
        "SALSA DE CHIPOTLE": ("56", "S. CHIPOTLE"),
        "SALSA DE SERRANO": ("56", "S. SERRANI"),
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
        "AGUA HORCHATA ROSA 1/2": ("17.5", "HR 1/2"),
        "AGUA HORCHATA ROSA LT": ("29", "HR LT"),
        "AGUA HORCHATA BLANCA 1/2": ("17.5", "HB 1/2"),
        "AGUA HORCHATA BLANCA LT": ("29", "HB LT"),
        "AGUA JAMAICA 1/2": ("17.5", "JAM 1/2"),
        "AGUA JAMAICA LT": ("29", "JAM LT"),
        "SERVILLETAS": ("39", "SERVILLETA"),
        "VASO 8 TÉRMICO DART": ("16.5", "VASO 8 oz"),
        "CUCHARA CHICA ECONÓMICA": ("9.5", "CUCHARA"),
        "6x6 NEVADO 125 PZAS": ("108", "6x6"),
        "7x7 LISO NEVADO 100 PZAS": ("170", "7x7"),
        "HOAGIE REYMA": ("151", "HOAGIE"),
    },
    "aguilas": {
        "LITRO DE BARBACOA": ("178", "BARBACOA"),
        "TORTILLA ESPECIAL": ("25.5", "TORTILLA"),
        "BOLILLO": ("9", "BOLILLO"),
        "QUESO": ("160", "QUESO"),
        "CEBOLLA BLANCA": ("52", "C. PICADA"),
        "CEBOLLA GUISADA": ("60", "C. GUISADA"),
        "CHILE GüERO": ("64", "CHILE"),
        "SALSA DE TOMATE": ("60", "S. ROJA"),
        "SALSA DE AGUACATE": ("60", "S. VERDE"),
        "SALSA DE CHIPOTLE": ("56", "S. CHIPOTLE"),
        "SALSA DE SERRANO": ("56", "S. SERRANI"),
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
        "AGUA HORCHATA ROSA 1/2": ("17.5", "HR 1/2"),
        "AGUA HORCHATA ROSA LT": ("29", "HR LT"),
        "AGUA HORCHATA BLANCA 1/2": ("17.5", "HB 1/2"),
        "AGUA HORCHATA BLANCA LT": ("29", "HB LT"),
        "AGUA JAMAICA 1/2": ("17.5", "JAM 1/2"),
        "AGUA JAMAICA LT": ("29", "JAM LT"),
        "SERVILLETAS": ("39", "SERVILLETA"),
        "VASO 8 TÉRMICO DART": ("16.5", "VASO 8 oz"),
        "CUCHARA CHICA ECONÓMICA": ("9.5", "CUCHARA"),
        "6x6 NEVADO 125 PZAS": ("108", "6x6"),
        "7x7 LISO NEVADO 100 PZAS": ("170", "7x7"),
        "HOAGIE REYMA": ("151", "HOAGIE"),
    },
    "mayoreo": {
        "LITRO DE BARBACOA": ("190", "BARBACOA .M"),
        "TORTILLA ESPECIAL": ("26.5", "TORTILLA .M"),
        "BOLILLO": ("9", "BOLILLO"),
        "QUESO": ("160", "QUESO"),
        "CEBOLLA BLANCA": ("52", "C. PICADA"),
        "CEBOLLA GUISADA": ("60", "C. GUISADA"),
        "CHILE GüERO": ("64", "CHILE"),
        "SALSA DE TOMATE": ("60", "S. ROJA"),
        "SALSA DE AGUACATE": ("60", "S. VERDE"),
        "SALSA DE CHIPOTLE": ("56", "S. CHIPOTLE"),
        "SALSA DE SERRANO": ("56", "S. SERRANI"),
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
        "AGUA HORCHATA ROSA 1/2": ("18", "HR 1/2 .M"),
        "AGUA HORCHATA ROSA LT": ("31", "HR LT .M"),
        "AGUA HORCHATA BLANCA 1/2": ("18", "HB 1/2 .M"),
        "AGUA HORCHATA BLANCA LT": ("31", "HB LT .M"),
        "AGUA JAMAICA 1/2": ("18", "JAM 1/2 .M"),
        "AGUA JAMAICA LT": ("31", "JAM LT .M"),
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
    ("Brot Nueva Galicia", SucursalCliente.Tipo.CLIENTE_MAYORISTA, "mayoreo"),
    ("Brot CAT", SucursalCliente.Tipo.CLIENTE_MAYORISTA, "mayoreo"),
    ("Rakebela", SucursalCliente.Tipo.CLIENTE_MAYORISTA, "mayoreo"),
]


def username_for_name(nombre):
    return slugify(nombre).replace("-", "_")


def ensure_password(user, password):
    if not user.has_usable_password():
        user.set_password(password)


def seed_demo_data():
    """Crea datos demo de Los Tocayos de forma idempotente."""

    User = get_user_model()

    admin, admin_created = User.objects.get_or_create(username="admin")
    admin.is_staff = True
    admin.is_superuser = True
    admin.first_name = admin.first_name or "Admin"
    if admin_created or not admin.has_usable_password():
        admin.set_password("admin123")
    admin.save()

    productos = {}
    for orden, (
        nombre,
        nombre_ticket,
        unidad_medida,
        unidad_abreviatura,
        cantidad_por_precio,
        promo_aguilas_martes,
    ) in enumerate(PRODUCTOS_CATALOGO, start=1):
        producto, _ = Producto.objects.update_or_create(
            nombre=nombre,
            defaults={
                "nombre_ticket": nombre_ticket,
                "unidad_medida": unidad_medida,
                "unidad_abreviatura": unidad_abreviatura,
                "cantidad_por_precio": Decimal(cantidad_por_precio),
                "promo_aguilas_martes": promo_aguilas_martes,
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
            user.set_password(nombre)
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
            Precio.objects.update_or_create(
                producto=producto,
                sucursal_cliente=sucursal,
                fecha_vigencia=hoy,
                defaults={
                    "precio_unitario": Decimal(precio_unitario),
                    "nombre_ticket": nombre_ticket,
                },
            )

    Configuracion.get_solo()

    return {
        "usuarios": User.objects.count(),
        "sucursales_clientes": SucursalCliente.objects.count(),
        "productos": Producto.objects.count(),
        "precios": Precio.objects.count(),
    }
