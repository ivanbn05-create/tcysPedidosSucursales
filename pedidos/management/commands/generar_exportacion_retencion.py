from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from pedidos.retencion import generar_exportacion


def _fecha(valor, nombre):
    fecha = parse_datetime(valor)
    if fecha is None or fecha.tzinfo is None:
        raise CommandError(f"{nombre} requiere ISO 8601 con zona horaria.")
    return fecha


class Command(BaseCommand):
    help = "Genera un ZIP local 0600 y un ticket; todavía no confirma descarga."

    def add_arguments(self, parser):
        parser.add_argument("--desde-recepcion", required=True)
        parser.add_argument("--hasta-recepcion", required=True)
        parser.add_argument("--archivo", required=True)
        parser.add_argument("--limite", type=int, default=500)
        parser.add_argument("--id-desde", type=int, default=0)
        parser.add_argument("--id-hasta", type=int)

    def handle(self, *args, **opciones):
        try:
            lote = generar_exportacion(
                desde=_fecha(opciones["desde_recepcion"], "desde-recepcion"),
                hasta=_fecha(opciones["hasta_recepcion"], "hasta-recepcion"),
                destino=opciones["archivo"],
                limite=opciones["limite"],
                id_desde=opciones["id_desde"],
                id_hasta=opciones["id_hasta"],
            )
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"lote={lote.pk} estado={lote.estado} pedidos={lote.numero_pedidos} "
            f"items={lote.numero_items} sha256_archivo={lote.sha256_archivo}"
        )
