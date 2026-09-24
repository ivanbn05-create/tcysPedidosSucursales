"""Emite o rota una credencial de lectura POS v2 sin imprimir el bearer."""

import hashlib
import os
import secrets
import stat
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pedidos.models import PosApiCredential, SucursalCliente


class Command(BaseCommand):
    help = "Emite una credencial POS v2 en un archivo privado 0600."

    def add_arguments(self, parser):
        parser.add_argument("--edge-id", required=True)
        parser.add_argument("--sucursal-id", required=True, type=int)
        parser.add_argument("--output", required=True)
        parser.add_argument("--reference", required=True)
        parser.add_argument("--rotate-from", default="")
        parser.add_argument("--expires-days", type=int, default=90)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Se requiere --confirm.")
        if os.name != "posix":
            raise CommandError("Este comando sólo puede entregar secretos en Linux/POSIX.")
        reference = options["reference"].strip()
        if not 3 <= len(reference) <= 120:
            raise CommandError("--reference debe tener entre 3 y 120 caracteres.")
        if not 1 <= options["expires_days"] <= 365:
            raise CommandError("--expires-days debe estar entre 1 y 365.")
        try:
            edge_id = uuid.UUID(options["edge_id"])
            previous_id = uuid.UUID(options["rotate_from"]) if options["rotate_from"] else None
        except ValueError as exc:
            raise CommandError("Los UUID de Edge y credencial deben ser válidos.") from exc

        output = Path(options["output"])
        if not output.is_absolute() or output.name in ("", ".", ".."):
            raise CommandError("--output debe ser un archivo absoluto.")
        try:
            directory = output.parent.resolve(strict=True)
        except OSError as exc:
            raise CommandError("El directorio privado no existe.") from exc
        public_roots = [settings.BASE_DIR, settings.STATIC_ROOT, *settings.STATICFILES_DIRS]
        if any(directory.is_relative_to(Path(root).resolve()) for root in public_roots if root):
            raise CommandError("El secreto debe quedar fuera del release y rutas públicas.")
        if any((ancestor / ".git").exists() for ancestor in (directory, *directory.parents)):
            raise CommandError("El secreto debe quedar fuera de repositorios Git.")
        if output.exists() or output.is_symlink():
            raise CommandError("El archivo de salida ya existe; no se sobrescribirá.")

        fd_directory = os.open(
            str(directory), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
        file_created = False
        try:
            info = os.fstat(fd_directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise CommandError("El directorio debe pertenecer al operador y tener modo 0700.")
            with transaction.atomic():
                branch = (
                    SucursalCliente.objects.select_for_update()
                    .filter(pk=options["sucursal_id"], tipo=SucursalCliente.Tipo.SUCURSAL, activa=True)
                    .first()
                )
                if branch is None:
                    raise CommandError("Sucursal activa de tipo sucursal no encontrada.")
                previous = None
                if previous_id:
                    previous = PosApiCredential.objects.select_for_update().filter(
                        pk=previous_id, edge_id=edge_id, sucursal_cliente=branch,
                        active=True, revoked_at__isnull=True,
                    ).first()
                    if previous is None:
                        raise CommandError("Credencial previa activa del mismo Edge/sucursal no encontrada.")
                elif PosApiCredential.objects.filter(
                    edge_id=edge_id, sucursal_cliente=branch,
                    active=True, revoked_at__isnull=True,
                ).exists():
                    raise CommandError("Ya hay credencial activa; usa --rotate-from.")

                bearer = secrets.token_urlsafe(48)
                credential = PosApiCredential.objects.create(
                    edge_id=edge_id,
                    sucursal_cliente=branch,
                    token_sha256=hashlib.sha256(bearer.encode("utf-8")).hexdigest(),
                    scopes=["orders:v2:read"],
                    expires_at=timezone.now() + timedelta(days=options["expires_days"]),
                    issued_reference=reference,
                    rotated_from=previous,
                )
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd_output = os.open(output.name, flags, 0o600, dir_fd=fd_directory)
                file_created = True
                with os.fdopen(fd_output, "w", encoding="ascii") as secret_file:
                    secret_file.write(bearer + "\n")
                    secret_file.flush()
                    os.fsync(secret_file.fileno())
                if stat.S_IMODE(os.stat(output.name, dir_fd=fd_directory).st_mode) != 0o600:
                    raise CommandError("No se pudo garantizar modo 0600.")
                if previous is not None:
                    previous.active = False
                    previous.revoked_at = timezone.now()
                    previous.revocation_reference = reference
                    previous.save(update_fields=["active", "revoked_at", "revocation_reference"])
            self.stdout.write(
                f"Credencial {credential.credential_id} emitida para Edge {edge_id} "
                f"y sucursal {branch.pk}; entrega el archivo por canal privado."
            )
        except Exception:
            if file_created:
                os.unlink(output.name, dir_fd=fd_directory)
            raise
        finally:
            os.close(fd_directory)
