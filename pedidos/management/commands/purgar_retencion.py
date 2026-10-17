import json
import logging

from django.core.management.base import BaseCommand, CommandError

from pedidos.retencion import aplicar_purga, compactar_tablas_purgadas, planificar_purga
from pedidos.pos_recovery import operator_name


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Muestra dry-run de retención; --apply borra lo vencido (15 días) y compacta las tablas."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--referencia-operacion", default="")
        parser.add_argument("--confirm-irreversible", action="store_true")

    def handle(self, *args, **opciones):
        reference = opciones["referencia_operacion"].strip()
        if opciones["apply"] and (
            not opciones["confirm_irreversible"] or not 3 <= len(reference) <= 120
        ):
            raise CommandError("Purga real requiere referencia y --confirm-irreversible.")
        try:
            plan = aplicar_purga() if opciones["apply"] else planificar_purga()
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        salida = {"modo": "real" if opciones["apply"] else "dry-run", **plan.como_dict()}
        if opciones["apply"]:
            salida["tablas_compactadas"] = compactar_tablas_purgadas()
        self.stdout.write(json.dumps(salida, ensure_ascii=False, sort_keys=True))
        logger.info("retencion_purga modo=%s operacion=%s operador_os=%s pedidos_edad=%s pedidos_export=%s eventos=%s",
                    salida["modo"], reference or "dry-run", operator_name(),
                    len(plan.pedidos_por_edad), len(plan.pedidos_por_exportacion),
                    plan.eventos_vencidos)
