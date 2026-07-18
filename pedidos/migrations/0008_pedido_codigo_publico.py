import uuid

from django.db import migrations, models


def populate_codigo_publico(apps, schema_editor):
    Pedido = apps.get_model("pedidos", "Pedido")
    for pedido in Pedido.objects.filter(codigo_publico__isnull=True):
        codigo = uuid.uuid4()
        while Pedido.objects.filter(codigo_publico=codigo).exists():
            codigo = uuid.uuid4()
        pedido.codigo_publico = codigo
        pedido.save(update_fields=["codigo_publico"])


class Migration(migrations.Migration):

    dependencies = [
        ("pedidos", "0007_actualiza_correo_recordatorios"),
    ]

    operations = [
        migrations.AddField(
            model_name="pedido",
            name="codigo_publico",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(populate_codigo_publico, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="pedido",
            name="codigo_publico",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
