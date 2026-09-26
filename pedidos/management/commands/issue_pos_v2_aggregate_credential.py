"""Issue an exact-tuple bearer without exposing it in terminal output."""

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from pedidos.models import PosAggregatedCredential, SucursalCliente
from pedidos.pos_recovery_v2 import _sender_ids, _write_private


class Command(BaseCommand):
    help = "Emite/rota credencial agregada POS v2 en archivo privado 0600."

    def add_arguments(self, parser):
        for name in ("edge-id", "pos-branch-id", "sender-ids", "output", "reference"):
            parser.add_argument(f"--{name}", required=True)
        parser.add_argument("--rotate-from", default="")
        parser.add_argument("--expires-days", type=int, default=90)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"] or not 3 <= len(options["reference"]) <= 120:
            raise CommandError("Se requiere --confirm y referencia de aprobación.")
        if not 1 <= options["expires_days"] <= 365:
            raise CommandError("Vigencia fuera de límite.")
        try:
            edge_id = uuid.UUID(options["edge_id"])
            branch_id = uuid.UUID(options["pos_branch_id"])
            sender_ids = _sender_ids([int(v) for v in options["sender_ids"].split(",")])
            previous_id = uuid.UUID(options["rotate_from"]) if options["rotate_from"] else None
        except ValueError as exc:
            raise CommandError("Identidad inválida.") from exc
        if SucursalCliente.objects.filter(
            pk__in=sender_ids, tipo=SucursalCliente.Tipo.SUCURSAL, activa=True
        ).count() != len(sender_ids):
            raise CommandError("Tupla contiene un remitente no aprobado/activo.")
        bearer = secrets.token_urlsafe(48)
        with transaction.atomic():
            existing = PosAggregatedCredential.objects.select_for_update().filter(
                edge_id=edge_id, pos_branch_id=branch_id, sender_ids=sender_ids,
                active=True, revoked_at__isnull=True,
            )
            previous = existing.filter(pk=previous_id).first() if previous_id else None
            if (previous_id and previous is None) or (not previous_id and existing.exists()):
                raise CommandError("Rotación requiere credential_id activo del mismo alcance.")
            _write_private(options["output"], (bearer + "\n").encode("ascii"))
            try:
                credential = PosAggregatedCredential.objects.create(
                    edge_id=edge_id, pos_branch_id=branch_id, sender_ids=sender_ids,
                    token_sha256=hashlib.sha256(bearer.encode()).hexdigest(),
                    scopes=["orders:v2:read"],
                    expires_at=timezone.now() + timedelta(days=options["expires_days"]),
                    issued_reference=options["reference"],
                )
                if previous:
                    previous.active = False
                    previous.revoked_at = timezone.now()
                    previous.save(update_fields=["active", "revoked_at"])
            except Exception:
                from pathlib import Path
                Path(options["output"]).unlink(missing_ok=True)
                raise
        self.stdout.write(f"Credencial {credential.pk} emitida para tupla {sender_ids}; bearer no impreso.")
