"""Exporta recibos y tombstones técnicos para backup fuera del VPS."""

import json

from django.core.management.base import BaseCommand, CommandError

from pedidos.retencion_ledger import LedgerError, exportar_ledger


class Command(BaseCommand):
    help = "Crea JSONL técnico 0600 y .sha256 sin contenido de pedidos."

    def add_arguments(self, parser):
        parser.add_argument("--destino", required=True)

    def handle(self, *args, **opciones):
        try:
            resumen = exportar_ledger(opciones["destino"])
        except (LedgerError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(resumen, sort_keys=True))
