import json

from django.core.management.base import BaseCommand, CommandError

from pedidos.retencion import conciliar_restauracion


class Command(BaseCommand):
    help = "Detecta o elimina pedidos reintroducidos por una copia temporal aislada."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opciones):
        try:
            resumen = conciliar_restauracion(aplicar=opciones["apply"])
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {"modo": "real" if opciones["apply"] else "dry-run", **resumen},
                sort_keys=True,
            )
        )
