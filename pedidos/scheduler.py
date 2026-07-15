"""Scheduler en proceso para disparar el recordatorio diario de pedidos.

Nota: Este proyecto está en Render ahora, irá a VPS después. Usa APScheduler
para esta implementación (Render), porque el plan actual no tiene cron nativo
atado al web service. Cuando migremos a VPS, cambiaremos a cron nativo del SO
(crontab) llamando al mismo management command `enviar_recordatorios` — el
comando no cambia, solo quién lo dispara. Ese día:
  1. Poner SCHEDULER_ENABLED=False en el entorno del VPS.
  2. Agregar la entrada de crontab (ver README.md / CLAUDE.md).
  3. Este archivo (y su arranque en pedidos/apps.py) dejan de usarse.

Caveat conocido: si algún día `gunicorn proyecto.wsgi` corre con más de un
worker, cada worker arrancaría su propio scheduler y el recordatorio se
mandaría duplicado. El Procfile actual no fija --workers (gunicorn usa 1 por
defecto), así que hoy no es un problema, pero si se agrega concurrencia hay
que deshabilitar el scheduler en todos los workers menos uno, o mover el
disparo a un servicio dedicado.
"""

import logging
import threading

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

logger = logging.getLogger(__name__)

_scheduler = None
_lock = threading.Lock()


def _job_revisar_recordatorio():
    """Corre cada minuto: si ya es la hora configurada y no se ha enviado hoy,
    dispara el management command `enviar_recordatorios`."""

    from pedidos.models import Configuracion, LogRecordatorio

    config = Configuracion.get_solo()
    ahora = timezone.localtime()
    hora_actual = ahora.time().replace(second=0, microsecond=0)
    hora_objetivo = config.hora_envio_recordatorio.replace(second=0, microsecond=0)

    if hora_actual != hora_objetivo:
        return

    ya_corrio_hoy = LogRecordatorio.objects.filter(fecha_envio__date=ahora.date()).exists()
    if ya_corrio_hoy:
        return

    logger.info("Disparando enviar_recordatorios desde APScheduler (%s).", ahora.strftime("%H:%M"))
    try:
        call_command("enviar_recordatorios")
    except Exception:
        logger.exception("Fallo al ejecutar enviar_recordatorios desde el scheduler.")


def start_scheduler():
    """Arranca el BackgroundScheduler una sola vez por proceso (idempotente)."""

    global _scheduler

    if not getattr(settings, "SCHEDULER_ENABLED", True):
        logger.info("SCHEDULER_ENABLED=False: no se arranca APScheduler.")
        return

    with _lock:
        if _scheduler is not None:
            return

        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(timezone=str(timezone.get_current_timezone()))
        scheduler.add_job(
            _job_revisar_recordatorio,
            trigger="interval",
            minutes=1,
            id="revisar_recordatorio_diario",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        _scheduler = scheduler
        logger.info("APScheduler iniciado: revisa cada minuto si toca enviar el recordatorio diario.")


def stop_scheduler():
    """Solo para tests/depuración manual."""

    global _scheduler
    with _lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
