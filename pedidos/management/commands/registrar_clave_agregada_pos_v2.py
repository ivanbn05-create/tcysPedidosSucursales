"""Register a scoped Ed25519 public key for aggregate recovery."""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pedidos.models import PosRecoveryV2SigningKey
from pedidos.pos_recovery import _decode_base64url, _read_private
from pedidos.pos_recovery_v2 import _active_grant, _sender_ids


class Command(BaseCommand):
    help = "Registra sólo la clave pública Ed25519 de Edge o Pedidos para una tupla exacta."

    def add_arguments(self, parser):
        parser.add_argument("--kind", choices=["edge", "pedidos"], required=True)
        for name in ("key-id", "edge-id", "pos-branch-id", "sender-ids",
                     "public-key-file", "referencia"):
            parser.add_argument(f"--{name}", required=True)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm"] or not 3 <= len(options["referencia"]) <= 120:
            raise CommandError("Se requiere --confirm y referencia no sensible.")
        try:
            key_id = uuid.UUID(options["key_id"])
            edge_id = uuid.UUID(options["edge_id"])
            pos_branch_id = uuid.UUID(options["pos_branch_id"])
            sender_ids = _sender_ids([int(v) for v in options["sender_ids"].split(",")])
            public_b64 = _read_private(options["public_key_file"], maximum=128).decode("ascii").strip()
        except (ValueError, UnicodeDecodeError) as exc:
            raise CommandError("Identidad o clave pública inválida.") from exc
        _decode_base64url(public_b64, 32)
        with transaction.atomic():
            if not _active_grant(edge_id, pos_branch_id, sender_ids):
                raise CommandError("No hay grant activo para la tupla exacta.")
            existing = PosRecoveryV2SigningKey.objects.filter(pk=key_id).first()
            values = dict(kind=options["kind"], edge_id=edge_id,
                          pos_branch_id=pos_branch_id, sender_ids=sender_ids,
                          public_key_b64=public_b64)
            if existing:
                if all(getattr(existing, name) == value for name, value in values.items()) and existing.active:
                    self.stdout.write(f"Clave {key_id} ya registrada.")
                    return
                raise CommandError("key_id ya usado con otro alcance/estado.")
            PosRecoveryV2SigningKey.objects.create(
                key_id=key_id, issued_reference=options["referencia"], **values
            )
        self.stdout.write(f"Clave pública {key_id} registrada sin imprimir material.")
