from django.core.management.color import no_style
from django.db import migrations


def limpiar_historial_pedidos(apps, schema_editor):
    Pedido = apps.get_model("pedidos", "Pedido")
    ItemPedido = apps.get_model("pedidos", "ItemPedido")
    db_alias = schema_editor.connection.alias

    ItemPedido.objects.using(db_alias).all().delete()
    Pedido.objects.using(db_alias).all().delete()

    with schema_editor.connection.cursor() as cursor:
        if schema_editor.connection.vendor == "sqlite":
            cursor.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('pedidos_itempedido', 'pedidos_pedido')"
            )
            return

        statements = schema_editor.connection.ops.sequence_reset_sql(
            no_style(),
            [ItemPedido, Pedido],
        )
        for statement in statements:
            cursor.execute(statement)


class Migration(migrations.Migration):

    dependencies = [
        ("pedidos", "0005_limpia_catalogo_legacy"),
    ]

    operations = [
        migrations.RunPython(limpiar_historial_pedidos, migrations.RunPython.noop),
    ]
