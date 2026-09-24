from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pedidos", "0014_first_received_at_trigger"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MFAEstado",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.PositiveBigIntegerField(default=1)),
                ("fallos", models.PositiveSmallIntegerField(default=0)),
                ("bloqueado_hasta", models.DateTimeField(blank=True, null=True)),
                ("usuario", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="estado_mfa_pedidos", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="MFACodigoRecuperacion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("digest", models.CharField(editable=False, max_length=64, unique=True)),
                ("creado_en", models.DateTimeField(auto_now_add=True)),
                ("usado_en", models.DateTimeField(blank=True, null=True)),
                ("usuario", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="codigos_recuperacion_mfa_pedidos", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
