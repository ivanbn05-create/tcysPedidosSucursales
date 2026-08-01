from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pedidos", "0008_pedido_codigo_publico"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="precio",
            index=models.Index(
                fields=["producto", "sucursal_cliente", "-fecha_vigencia"],
                name="precio_vigente_idx",
            ),
        ),
    ]
