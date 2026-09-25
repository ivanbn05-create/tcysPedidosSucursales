import base64
import hashlib
import json
import os
import stat
import tempfile
import uuid
import zipfile
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest import skipUnless

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from .models import (
    Pedido, PedidoPurgado, PosApiCredential, PosEdgeSigningKey, PosRetentionRecovery,
    RegistroPurga, SucursalCliente,
)
from .pos_recovery import _read_zip_entry, _snapshot, _uuid_set, _verified_ack, canonical, complete, prepare


def _b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _sign_ack(private_key, payload):
    return {"payload": payload, "signature_b64": _b64url(private_key.sign(canonical(payload)))}


class RecoveryValidationTests(TestCase):
    def test_fixture_sintetico_firma_canonica(self):
        path = Path(settings.BASE_DIR) / "docs/integracion/schemas/recovery-fixture-sintetico.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        public = base64.urlsafe_b64decode(fixture["edge_public_key_b64"] + "=")
        signature = base64.urlsafe_b64decode(fixture["edge_ack"]["signature_b64"] + "==")
        Ed25519PublicKey.from_public_bytes(public).verify(
            signature, canonical(fixture["edge_ack"]["payload"])
        )

    def test_acuse_firmado_rechaza_suplantacion_scope_y_replay(self):
        branch = SucursalCliente.objects.create(nombre="Rama sintética", tipo="sucursal")
        other = SucursalCliente.objects.create(nombre="Otra rama sintética", tipo="sucursal")
        edge_id, pos_branch_id, key_id, recovery_id = (uuid.uuid4() for _ in range(4))
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        key = PosEdgeSigningKey.objects.create(
            key_id=key_id, edge_id=edge_id, pos_branch_id=pos_branch_id,
            sucursal_cliente=branch, public_key_b64=_b64url(public_key),
            issued_reference="LAB-KEY-001", issued_operator="fixture",
        )
        now = timezone.now()
        record = PosRetentionRecovery.objects.create(
            recovery_id=recovery_id, edge_id=edge_id, pos_branch_id=pos_branch_id,
            sucursal_cliente=branch, desde=now - timedelta(hours=1), hasta=now,
            old_cursor_sha256="a" * 64, purge_epoch="b" * 64,
            snapshot_sha256="c" * 64, orders_sha256="d" * 64,
            orders_count=0, tombstones_sha256="e" * 64, tombstones_count=0,
            referencia_apertura="LAB-REC-KEY", operador_apertura="fixture",
        )
        payload = {
            "type": "pedidos.edge.recovery_ack.v1", "key_id": str(key_id),
            "nonce": str(uuid.uuid4()), "issued_at": now.isoformat(),
            "recovery_id": str(recovery_id), "edge_id": str(edge_id),
            "pos_branch_id": str(pos_branch_id), "sucursal_cliente_id": branch.pk,
            "snapshot_sha256": record.snapshot_sha256,
            "orders_sha256": record.orders_sha256,
            "old_cursor_sha256": record.old_cursor_sha256,
            "received_order_ids": [], "unresolved_order_ids": [],
        }
        ack, matched, nonce = _verified_ack(_sign_ack(private_key, payload), record)
        self.assertEqual((ack, matched, nonce), (payload, key, uuid.UUID(payload["nonce"])))
        tampered = _sign_ack(private_key, payload)
        tampered["payload"] = {**payload, "sucursal_cliente_id": other.pk}
        with self.assertRaises(CommandError):
            _verified_ack(tampered, record)
        with self.assertRaises(CommandError):
            _verified_ack(_sign_ack(Ed25519PrivateKey.generate(), payload), record)
        other_private = Ed25519PrivateKey.generate()
        other_key_id = uuid.uuid4()
        PosEdgeSigningKey.objects.create(
            key_id=other_key_id, edge_id=edge_id, pos_branch_id=pos_branch_id,
            sucursal_cliente=other,
            public_key_b64=_b64url(other_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
            issued_reference="LAB-KEY-OTHER", issued_operator="fixture",
        )
        payload["key_id"] = str(other_key_id)
        with self.assertRaises(CommandError):
            _verified_ack(_sign_ack(other_private, payload), record)
        payload["key_id"] = str(key_id)
        payload["issued_at"] = (now - timedelta(days=1)).isoformat()
        with self.assertRaises(CommandError):
            _verified_ack(_sign_ack(private_key, payload), record)
        payload["issued_at"] = now.isoformat()
        with self.assertRaises(CommandError):
            _verified_ack(_sign_ack(private_key, {**payload, "unexpected": True}), record)
        record.edge_ack_nonce = nonce
        record.edge_ack_sha256 = "f" * 64
        with self.assertRaises(CommandError):
            _verified_ack(_sign_ack(private_key, payload), record)
        payload["nonce"] = str(uuid.uuid4())
        key.active = False
        key.revoked_at = timezone.now()
        key.save(update_fields=["active", "revoked_at"])
        with self.assertRaises(CommandError):
            _verified_ack(_sign_ack(private_key, payload), record)

    def test_rechaza_identidades_duplicadas_y_malformadas(self):
        value = str(uuid.uuid4())
        with self.assertRaises(CommandError):
            _uuid_set([value, value])
        with self.assertRaises(CommandError):
            _uuid_set(["no-es-uuid"])

    def test_purga_real_no_acepta_un_flag_simple(self):
        with self.assertRaises(CommandError):
            call_command("purgar_retencion", apply=True, stdout=StringIO())

    def test_zip_manipulado_no_descomprime_mas_del_limite(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("orders.jsonl", b"x" * 1024)
        buffer.seek(0)
        with zipfile.ZipFile(buffer) as archive:
            with self.assertRaises(CommandError):
                _read_zip_entry(archive, "orders.jsonl", 100)
            self.assertEqual(_read_zip_entry(archive, "orders.jsonl", 1024), b"x" * 1024)

    def test_baseline_no_mezcla_sucursales_y_incluye_purga_fuera_de_ventana(self):
        one = SucursalCliente.objects.create(nombre="Arboledas sintética", tipo="sucursal")
        other = SucursalCliente.objects.create(nombre="Otra sintética", tipo="sucursal")
        now = timezone.now()
        valid = Pedido.objects.create(
            sucursal_cliente=one, usuario_nombre="fixture",
            estado=Pedido.Estado.CONFIRMADO, fecha_confirmacion=now,
        )
        Pedido.objects.create(
            sucursal_cliente=other, usuario_nombre="fixture",
            estado=Pedido.Estado.CONFIRMADO, fecha_confirmacion=now,
        )
        receipt = RegistroPurga.objects.create(
            motivo="antiguedad", numero_pedidos=2, numero_items=0,
            numero_macropedidos=0,
        )
        tombstone_one = uuid.uuid4()
        tombstone_other = uuid.uuid4()
        for branch, key, origin in ((one, tombstone_one, 9001), (other, tombstone_other, 9002)):
            PedidoPurgado.objects.create(
                codigo_publico=key, pedido_id_origen=origin,
                sucursal_cliente_id=branch.pk,
                fecha_confirmacion=now - timedelta(days=2),
                motivo="antiguedad", registro=receipt,
            )
        rows, tombstones = _snapshot(one.pk, now - timedelta(hours=1), now + timedelta(hours=1))
        self.assertEqual([row["codigo_publico"] for row in rows], [str(valid.codigo_publico)])
        self.assertEqual(tombstones, [str(tombstone_one)])


@skipUnless(os.name == "posix", "Artefactos privados 0600 requieren Linux/POSIX")
class RecoveryCommandTests(TestCase):
    def setUp(self):
        self.branch = SucursalCliente.objects.create(
            nombre="Arboledas sintética", tipo=SucursalCliente.Tipo.SUCURSAL,
        )
        self.edge_id = uuid.uuid4()
        self.pos_branch_id = uuid.uuid4()
        PosApiCredential.objects.create(
            edge_id=self.edge_id, pos_branch_id=self.pos_branch_id,
            sucursal_cliente=self.branch,
            token_sha256=hashlib.sha256(b"credencial-sintetica-recovery").hexdigest(),
            scopes=["orders:v2:read"],
        )
        self.private_key = Ed25519PrivateKey.generate()
        self.key_id = uuid.uuid4()
        PosEdgeSigningKey.objects.create(
            key_id=self.key_id, edge_id=self.edge_id, pos_branch_id=self.pos_branch_id,
            sucursal_cliente=self.branch,
            public_key_b64=_b64url(self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
            issued_reference="LAB-KEY-002", issued_operator="fixture",
        )
        self.desde = timezone.now() - timedelta(hours=2)
        self.hasta = timezone.now() + timedelta(hours=2)
        self.order = Pedido.objects.create(
            sucursal_cliente=self.branch, usuario_nombre="Fixture sintético",
            estado=Pedido.Estado.CONFIRMADO, fecha_confirmacion=timezone.now(),
        )
        self.purged_id = uuid.uuid4()
        receipt = RegistroPurga.objects.create(
            motivo="exportacion", numero_pedidos=1, numero_items=0,
            numero_macropedidos=0,
        )
        PedidoPurgado.objects.create(
            codigo_publico=self.purged_id, pedido_id_origen=9999,
            sucursal_cliente_id=self.branch.pk, fecha_confirmacion=timezone.now(),
            motivo="exportacion", registro=receipt,
        )
        self.recovery_id = uuid.uuid4()

    def _write_private(self, path, value):
        path.write_bytes(value)
        os.chmod(path, 0o600)
        return hashlib.sha256(value).hexdigest()

    def test_registro_y_revocacion_de_clave_no_imprimen_material(self):
        with tempfile.TemporaryDirectory(prefix="tcys-edge-key-") as root:
            os.chmod(root, 0o700)
            public_file = Path(root) / "public.b64"
            private = Ed25519PrivateKey.generate()
            public_b64 = _b64url(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
            self._write_private(public_file, public_b64.encode("ascii"))
            key_id = uuid.uuid4()
            output = StringIO()
            options = dict(
                key_id=str(key_id), edge_id=str(self.edge_id),
                pos_branch_id=str(self.pos_branch_id), sucursal_id=self.branch.pk,
                public_key_file=str(public_file), referencia="LAB-REGISTER-KEY", confirm=True,
            )
            call_command("registrar_clave_firma_edge_v2", **options, stdout=output)
            call_command("registrar_clave_firma_edge_v2", **options, stdout=output)
            self.assertNotIn(public_b64, output.getvalue())
            self.assertEqual(PosEdgeSigningKey.objects.filter(pk=key_id).count(), 1)
            call_command("revocar_clave_firma_edge_v2", key_id=str(key_id),
                         referencia="LAB-REVOKE-KEY", confirm=True, stdout=output)
            self.assertFalse(PosEdgeSigningKey.objects.get(pk=key_id).active)
            with self.assertRaises(CommandError):
                call_command("registrar_clave_firma_edge_v2", **options, stdout=StringIO())

    def test_baseline_idempotente_y_cierre_solo_con_cobertura_total(self):
        with tempfile.TemporaryDirectory(prefix="tcys-recovery-") as root:
            os.chmod(root, 0o700)
            root = Path(root)
            cursor_file = root / "cursor.txt"
            self._write_private(cursor_file, b"")
            snapshot = root / "baseline.zip"
            args = dict(
                recovery_id=self.recovery_id, edge_id=self.edge_id,
                pos_branch_id=self.pos_branch_id, branch_id=self.branch.pk,
                desde=self.desde.isoformat(), hasta=self.hasta.isoformat(),
                old_cursor_file=cursor_file, output=snapshot, reference="LAB-REC-001",
            )
            record = prepare(**args)
            self.assertEqual(record.orders_count, 1)
            self.assertEqual(record.tombstones_count, 1)
            self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
            self.assertEqual(prepare(**args).pk, record.pk)
            self.assertEqual(PosRetentionRecovery.objects.count(), 1)

            ack = root / "ack.json"
            custody = root / "custody.json"
            ack_body = {
                "type": "pedidos.edge.recovery_ack.v1", "key_id": str(self.key_id),
                "nonce": str(uuid.uuid4()), "issued_at": timezone.now().isoformat(),
                "recovery_id": str(record.pk), "edge_id": str(self.edge_id),
                "pos_branch_id": str(self.pos_branch_id),
                "sucursal_cliente_id": self.branch.pk,
                "snapshot_sha256": record.snapshot_sha256,
                "orders_sha256": record.orders_sha256,
                "old_cursor_sha256": record.old_cursor_sha256,
                "received_order_ids": [str(self.order.codigo_publico)],
                "unresolved_order_ids": [str(self.purged_id)],
            }
            custody_body = {
                "recovery_id": str(record.pk),
                "restored_from_archive_ids": [], "already_on_edge_ids": [],
            }
            ack_hash = self._write_private(ack, json.dumps(_sign_ack(self.private_key, ack_body)).encode())
            custody_hash = self._write_private(custody, json.dumps(custody_body).encode())
            close_args = dict(
                recovery_id=self.recovery_id, snapshot_file=snapshot,
                snapshot_sha256=record.snapshot_sha256, edge_ack_file=ack,
                edge_ack_sha256=ack_hash, custody_file=custody,
                custody_sha256=custody_hash, freeze_evidence_sha256="a" * 64,
                reference="LAB-CLOSE-001",
            )
            with self.assertRaises(CommandError):
                complete(**{**close_args, "snapshot_sha256": "0" * 64})
            tampered = _sign_ack(self.private_key, ack_body)
            tampered["payload"] = {**ack_body, "sucursal_cliente_id": self.branch.pk + 1}
            bad_hash = self._write_private(ack, json.dumps(tampered).encode())
            with self.assertRaises(CommandError):
                complete(**{**close_args, "edge_ack_sha256": bad_hash})
            self._write_private(ack, json.dumps(_sign_ack(self.private_key, ack_body)).encode())
            credential = PosApiCredential.objects.get()
            credential.scopes = []
            credential.save(update_fields=["scopes"])
            with self.assertRaises(CommandError):
                complete(**close_args)
            credential.scopes = ["orders:v2:read"]
            credential.save(update_fields=["scopes"])
            self.assertEqual(PosRetentionRecovery.objects.get(pk=record.pk).estado,
                             PosRetentionRecovery.Estado.PENDIENTE)
            manual = complete(**close_args)
            self.assertEqual(manual.estado, PosRetentionRecovery.Estado.MANUAL)
            self.assertIsNone(manual.cerrada_en)

            ack_body["received_order_ids"].append(str(self.purged_id))
            ack_body["unresolved_order_ids"] = []
            ack_body["nonce"] = str(uuid.uuid4())
            ack_body["issued_at"] = timezone.now().isoformat()
            custody_body["restored_from_archive_ids"] = [str(self.purged_id)]
            close_args["edge_ack_sha256"] = self._write_private(ack, json.dumps(_sign_ack(self.private_key, ack_body)).encode())
            close_args["custody_sha256"] = self._write_private(custody, json.dumps(custody_body).encode())
            done = complete(**close_args)
            self.assertEqual(done.estado, PosRetentionRecovery.Estado.COMPLETADA)
            self.assertEqual(complete(**close_args).pk, done.pk)

    def test_no_abre_recuperacion_sin_410(self):
        PedidoPurgado.objects.all().delete()
        RegistroPurga.objects.all().delete()
        with tempfile.TemporaryDirectory(prefix="tcys-recovery-") as root:
            os.chmod(root, 0o700)
            root = Path(root)
            cursor_file = root / "cursor.txt"
            self._write_private(cursor_file, b"")
            with self.assertRaises(CommandError):
                prepare(
                    recovery_id=self.recovery_id, edge_id=self.edge_id,
                    pos_branch_id=self.pos_branch_id, branch_id=self.branch.pk,
                    desde=self.desde.isoformat(), hasta=self.hasta.isoformat(),
                    old_cursor_file=cursor_file, output=root / "baseline.zip",
                    reference="LAB-REC-002",
                )
            self.assertEqual(PosRetentionRecovery.objects.count(), 0)

    def test_cursor_obsoleto_incluye_purga_fuera_de_ventana(self):
        PedidoPurgado.objects.filter(pk=self.purged_id).update(
            fecha_confirmacion=self.desde - timedelta(days=1)
        )
        with tempfile.TemporaryDirectory(prefix="tcys-recovery-") as root:
            os.chmod(root, 0o700)
            root = Path(root)
            cursor_file = root / "cursor.txt"
            self._write_private(cursor_file, b"1727034600123456")
            record = prepare(
                recovery_id=self.recovery_id, edge_id=self.edge_id,
                pos_branch_id=self.pos_branch_id, branch_id=self.branch.pk,
                desde=self.desde.isoformat(), hasta=self.hasta.isoformat(),
                old_cursor_file=cursor_file, output=root / "baseline.zip",
                reference="LAB-REC-003",
            )
            self.assertEqual(record.orders_count, 1)
            self.assertEqual(record.tombstones_count, 1)
