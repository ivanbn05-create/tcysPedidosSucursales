from django.db import migrations, models


def completar_nombres_ticket(apps, schema_editor):
    Producto = apps.get_model("pedidos", "Producto")
    for producto in Producto.objects.filter(nombre_ticket=""):
        producto.nombre_ticket = producto.nombre[:24].upper()
        producto.save(update_fields=["nombre_ticket"])


class Migration(migrations.Migration):

    dependencies = [
        ("pedidos", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="producto",
            name="nombre_ticket",
            field=models.CharField(
                blank=True,
                help_text="Nombre breve usado en el ticket termico.",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="producto",
            name="activo",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(completar_nombres_ticket, migrations.RunPython.noop),
    ]
