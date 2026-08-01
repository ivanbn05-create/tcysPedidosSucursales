from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from pedidos.models import Precio


class Command(BaseCommand):
    help = "Elimina precios historicos redundantes cuando no cambio el precio ni el nombre de ticket."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo muestra cuantos precios se eliminarian, sin borrar registros.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        ids_redundantes = []
        combinaciones_revisadas = 0

        combinaciones = (
            Precio.objects.values("producto_id", "sucursal_cliente_id")
            .annotate(total=Count("id"))
            .filter(total__gt=1)
            .order_by("producto_id", "sucursal_cliente_id")
        )

        for combinacion in combinaciones.iterator():
            combinaciones_revisadas += 1
            estado_anterior = None
            precios = (
                Precio.objects.filter(
                    producto_id=combinacion["producto_id"],
                    sucursal_cliente_id=combinacion["sucursal_cliente_id"],
                )
                .only("id", "precio_unitario", "nombre_ticket", "fecha_vigencia")
                .order_by("fecha_vigencia", "id")
            )

            for precio in precios:
                estado_actual = (precio.precio_unitario, precio.nombre_ticket)
                if estado_actual == estado_anterior:
                    ids_redundantes.append(precio.id)
                else:
                    estado_anterior = estado_actual

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "DRY-RUN: se eliminarian "
                    f"{len(ids_redundantes)} precios redundantes "
                    f"en {combinaciones_revisadas} combinaciones."
                )
            )
            return

        with transaction.atomic():
            eliminados, _ = Precio.objects.filter(id__in=ids_redundantes).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Listo: se eliminaron {eliminados} precios redundantes "
                f"en {combinaciones_revisadas} combinaciones."
            )
        )
