from django.db import migrations, models


def actualizar_remitente(apps, schema_editor):
    Configuracion = apps.get_model("pedidos", "Configuracion")
    nuevo_remitente = "Los Tocayos <tocayos.tacos@gmail.com>"
    Configuracion.objects.filter(email_remitente="").update(email_remitente=nuevo_remitente)
    Configuracion.objects.filter(email_remitente__icontains="correos@lostocayos.com").update(
        email_remitente=nuevo_remitente
    )


class Migration(migrations.Migration):
    dependencies = [
        ("pedidos", "0006_limpia_historial_pedidos_prueba"),
    ]

    operations = [
        migrations.AlterField(
            model_name="configuracion",
            name="email_remitente",
            field=models.CharField(
                blank=True,
                help_text=(
                    'Nombre visible del remitente, ej. "Los Tocayos <tocayos.tacos@gmail.com>". '
                    "Si se deja vacío se usa DEFAULT_FROM_EMAIL/EMAIL_HOST_USER."
                ),
                max_length=255,
            ),
        ),
        migrations.RunPython(actualizar_remitente, migrations.RunPython.noop),
    ]
