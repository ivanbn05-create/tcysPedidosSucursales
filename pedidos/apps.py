import os
import sys

from django.apps import AppConfig

# Comandos de manage.py durante los que NO debe arrancar el scheduler de
# recordatorios (mantenimiento, tests, comandos de un solo tiro). Ver
# pedidos/scheduler.py para el porqué (Render/APScheduler vs VPS/cron).
COMANDOS_SIN_SCHEDULER = {
    "test",
    "migrate",
    "makemigrations",
    "shell",
    "shell_plus",
    "collectstatic",
    "seed_demo",
    "createsuperuser",
    "check",
    "dbshell",
    "showmigrations",
    "enviar_recordatorios",
}


class PedidosConfig(AppConfig):
    name = 'pedidos'

    def ready(self):
        self._maybe_start_scheduler()

    def _maybe_start_scheduler(self):
        argv = sys.argv
        es_manage_py = bool(argv) and "manage.py" in argv[0]
        comando = argv[1] if es_manage_py and len(argv) > 1 else None

        if comando in COMANDOS_SIN_SCHEDULER:
            return
        # En runserver, el autoreloader lanza un proceso "watcher" y luego un
        # hijo recargado marcado con RUN_MAIN=true; solo el hijo debe arrancar
        # el scheduler para no duplicarlo.
        if comando == "runserver" and os.environ.get("RUN_MAIN") != "true":
            return

        from .scheduler import start_scheduler

        start_scheduler()
