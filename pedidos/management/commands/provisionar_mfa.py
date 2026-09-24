"""Provisiona TOTP fuera de banda: nunca imprime semillas ni códigos."""

import os
import stat
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from pedidos.mfa import generar_codigos_recuperacion, mfa_requerido
from pedidos.models import MFAEstado, SesionActiva


class Command(BaseCommand):
    help = "Prepara un TOTP y códigos de recuperación en un archivo privado 0600."

    def add_arguments(self, parser):
        parser.add_argument("--usuario", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--confirm", action="store_true")
        parser.add_argument("--reset", action="store_true")
        parser.add_argument("--aprobacion", default="")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Se requiere --confirm.")
        if options["reset"] and not options["aprobacion"].strip():
            raise CommandError("El reset exige una referencia --aprobacion validada fuera de banda.")
        if os.name != "posix":
            raise CommandError("Ejecuta este comando únicamente en Linux/POSIX.")

        salida = Path(options["output"])
        if not salida.is_absolute() or salida.name in ("", ".", ".."):
            raise CommandError("--output debe ser un archivo absoluto.")
        try:
            directorio = salida.parent.resolve(strict=True)
        except OSError as exc:
            raise CommandError("El directorio privado no existe.") from exc
        bases_publicas = [settings.BASE_DIR, settings.STATIC_ROOT, getattr(settings, "MEDIA_ROOT", None)]
        for entrada in getattr(settings, "STATICFILES_DIRS", ()):
            bases_publicas.append(entrada[1] if isinstance(entrada, (tuple, list)) else entrada)
        if any(
            base and directorio.is_relative_to(Path(base).resolve(strict=False))
            for base in bases_publicas
        ) or any((ancestor / ".git").exists() for ancestor in (directorio, *directorio.parents)):
            raise CommandError("El archivo MFA debe quedar fuera de repositorios, releases y rutas públicas.")
        if salida.exists() or salida.is_symlink():
            raise CommandError("El archivo de salida ya existe; no se sobrescribirá.")

        flags_directorio = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd_directorio = os.open(str(salida.parent), flags_directorio)
        except OSError as exc:
            raise CommandError("No se pudo abrir el directorio privado.") from exc
        try:
            info = os.fstat(fd_directorio)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise CommandError("El directorio debe pertenecer al operador y tener modo 0700.")
            usuario = User.objects.filter(username=options["usuario"], is_active=True).first()
            if not usuario or not mfa_requerido(usuario):
                raise CommandError("Cuenta privilegiada activa no encontrada.")

            ruta_creada = False
            try:
                with transaction.atomic():
                    existentes = TOTPDevice.objects.select_for_update().filter(user=usuario)
                    if existentes.exists() and not options["reset"]:
                        raise CommandError("Ya existe TOTP; usa --reset con aprobación operativa.")
                    existentes.delete()
                    dispositivo = TOTPDevice.objects.create(
                        user=usuario, name="principal", confirmed=True
                    )
                    codigos = generar_codigos_recuperacion(usuario)
                    estado, _ = MFAEstado.objects.select_for_update().get_or_create(usuario=usuario)
                    estado.version += 1
                    estado.fallos = 0
                    estado.bloqueado_hasta = None
                    estado.save(update_fields=["version", "fallos", "bloqueado_hasta"])
                    SesionActiva.objects.filter(usuario=usuario).delete()

                    flags_archivo = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    fd_archivo = os.open(salida.name, flags_archivo, 0o600, dir_fd=fd_directorio)
                    ruta_creada = True
                    with os.fdopen(fd_archivo, "w", encoding="utf-8") as archivo:
                        archivo.write("MFA de Los Tocayos Pedidos. Entregar por canal privado y destruir tras el enrolamiento.\n")
                        archivo.write("URI TOTP (Google Authenticator):\n")
                        archivo.write(dispositivo.config_url + "\n")
                        archivo.write("Códigos de recuperación de un solo uso:\n")
                        for codigo in codigos:
                            archivo.write(codigo + "\n")
                        archivo.flush()
                        os.fsync(archivo.fileno())
                    info_archivo = os.stat(salida.name, dir_fd=fd_directorio, follow_symlinks=False)
                    if stat.S_IMODE(info_archivo.st_mode) != 0o600:
                        raise CommandError("No se pudo garantizar modo 0600.")
            except Exception:
                if ruta_creada:
                    os.unlink(salida.name, dir_fd=fd_directorio)
                raise
        finally:
            os.close(fd_directorio)

        self.stdout.write("Provisionamiento MFA preparado. Entrega y elimina el archivo por el procedimiento privado.")
