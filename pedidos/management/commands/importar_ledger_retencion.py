"""Importa recibos técnicos solamente en una restauración aislada."""

import json

from django.core.management.base import BaseCommand, CommandError

from pedidos.retencion_ledger import LedgerError, importar_ledger


class Command(BaseCommand):
    help = "Importa ledger verificado en base aislada; no elimina pedidos reintroducidos."

    def add_arguments(self, parser):
        parser.add_argument("--origen", required=True)
        parser.add_argument("--sha256-esperado", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opciones):
        try:
            resumen = importar_ledger(
                opciones["origen"],
                expected_sha256=opciones["sha256_esperado"],
                aplicar=opciones["apply"],
            )
        except (LedgerError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(resumen, sort_keys=True))
