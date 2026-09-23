import json

from django.core.management.base import BaseCommand, CommandError

from pedidos.retencion import aplicar_purga, planificar_purga


class Command(BaseCommand):
    help = "Muestra dry-run de retención; --apply exige habilitación y autorización manual."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opciones):
        try:
            plan = aplicar_purga() if opciones["apply"] else planificar_purga()
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        salida = {"modo": "real" if opciones["apply"] else "dry-run", **plan.como_dict()}
        self.stdout.write(json.dumps(salida, ensure_ascii=False, sort_keys=True))
