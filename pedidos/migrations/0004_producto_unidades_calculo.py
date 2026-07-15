from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pedidos", "0003_configuracion_sucursalcliente_email_logrecordatorio"),
    ]

    operations = [
        migrations.AddField(
            model_name="producto",
            name="cantidad_por_precio",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("1.000"),
                help_text=(
                    "Cantidad capturada que equivale a una unidad de precio. "
                    "Ejemplo: chile guero = 30 piezas por kilo."
                ),
                max_digits=8,
                validators=[
                    MinValueValidator(Decimal("0.001")),
                    MaxValueValidator(Decimal("999.999")),
                ],
            ),
        ),
        migrations.AddField(
            model_name="producto",
            name="promo_aguilas_martes",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="producto",
            name="unidad_abreviatura",
            field=models.CharField(default="PZA", max_length=8),
        ),
        migrations.AddField(
            model_name="producto",
            name="unidad_medida",
            field=models.CharField(default="PIEZA (PZA)", max_length=40),
        ),
        migrations.AddField(
            model_name="precio",
            name="nombre_ticket",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Nombre breve usado para esta sucursal/cliente; "
                    "si se deja vacio usa el del producto."
                ),
                max_length=24,
            ),
        ),
        migrations.RemoveField(
            model_name="producto",
            name="descripcion",
        ),
    ]
