from django.db import migrations


LEGACY_PRODUCT_NAMES = [
    "Barbacoa",
    "Tortilla",
    "Cebolla Guisada",
    "Chile Toreado",
    "Salsa Verde",
    "Salsa Roja",
]


def limpiar_catalogo_legacy(apps, schema_editor):
    Producto = apps.get_model("pedidos", "Producto")
    Precio = apps.get_model("pedidos", "Precio")
    ItemPedido = apps.get_model("pedidos", "ItemPedido")

    Producto.objects.filter(
        nombre="SALSA DE SERRANO",
        nombre_ticket="S. SERRANI",
    ).update(nombre_ticket="S. SERRANO")
    Precio.objects.filter(
        producto__nombre="SALSA DE SERRANO",
        nombre_ticket="S. SERRANI",
    ).update(nombre_ticket="S. SERRANO")

    for producto in Producto.objects.filter(nombre__in=LEGACY_PRODUCT_NAMES):
        if ItemPedido.objects.filter(producto_id=producto.id).exists():
            Precio.objects.filter(producto_id=producto.id).delete()
            producto.activo = False
            producto.save(update_fields=["activo"])
        else:
            producto.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pedidos", "0004_producto_unidades_calculo"),
    ]

    operations = [
        migrations.RunPython(limpiar_catalogo_legacy, migrations.RunPython.noop),
    ]
