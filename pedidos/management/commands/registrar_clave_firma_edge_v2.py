"""Register an Edge public key; the private key never enters Pedidos."""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from pedidos.models import PosApiCredential, PosEdgeSigningKey
from pedidos.pos_recovery import _decode_base64url, _read_private, operator_name


class Command(BaseCommand):
    help = "Registra una clave pública Ed25519 vinculada a un Edge/sucursal exactos."

    def add_arguments(self, parser):
        parser.add_argument("--key-id", required=True)
        parser.add_argument("--edge-id", required=True)
        parser.add_argument("--pos-branch-id", required=True)
        parser.add_argument("--sucursal-id", type=int, required=True)
        parser.add_argument("--public-key-file", required=True)
        parser.add_argument("--referencia", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"] or not 3 <= len(options["referencia"]) <= 120:
            raise CommandError("Se requiere confirmación y referencia de aprobación.")
        try:
            key_id = uuid.UUID(options["key_id"])
            edge_id = uuid.UUID(options["edge_id"])
            pos_branch_id = uuid.UUID(options["pos_branch_id"])
        except ValueError as exc:
            raise CommandError("Identidad UUID inválida.") from exc
        try:
            public_key_b64 = _read_private(options["public_key_file"], maximum=128).decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise CommandError("Clave pública no es ASCII base64url.") from exc
        _decode_base64url(public_key_b64, 32)
        with transaction.atomic():
            credentials = PosApiCredential.objects.filter(
                edge_id=edge_id, pos_branch_id=pos_branch_id,
                sucursal_cliente_id=options["sucursal_id"],
                active=True,
                revoked_at__isnull=True,
            )
            if not any(
                isinstance(item.scopes, list) and "orders:v2:read" in item.scopes
                and (item.expires_at is None or item.expires_at > timezone.now())
                for item in credentials
            ):
                raise CommandError("El mapeo Edge/POS/Pedidos no tiene credencial v2 activa.")
            existing = PosEdgeSigningKey.objects.filter(pk=key_id).first()
            if existing:
                if (existing.edge_id == edge_id and existing.pos_branch_id == pos_branch_id
                        and existing.sucursal_cliente_id == options["sucursal_id"]
                        and existing.public_key_b64 == public_key_b64 and existing.active
                        and existing.revoked_at is None):
                    self.stdout.write(f"Clave {key_id} ya registrada para este alcance.")
                    return
                raise CommandError("key_id ya registrado con otro estado o alcance.")
            try:
                PosEdgeSigningKey.objects.create(
                    key_id=key_id, edge_id=edge_id, pos_branch_id=pos_branch_id,
                    sucursal_cliente_id=options["sucursal_id"],
                    public_key_b64=public_key_b64,
                    issued_reference=options["referencia"], issued_operator=operator_name(),
                )
            except IntegrityError as exc:
                raise CommandError("Clave pública ya vinculada a otro registro.") from exc
        self.stdout.write(f"Clave {key_id} registrada; no se imprimió material criptográfico.")
