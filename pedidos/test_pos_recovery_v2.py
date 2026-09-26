"""Negative contract tests; end-to-end freeze/close also run on PostgreSQL."""

import base64
import hashlib
import io
import json
import os
import tempfile
import threading
import uuid
import zipfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import skipUnless

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.core.management.base import CommandError
from django.core.management import call_command
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    ExportacionRetencion, ItemPedido, Pedido, PedidoPurgado, PosAggregatedCredential,
    PosRecoveryV2, PosRecoveryV2Freeze, PosRecoveryV2Nonce,
    PosRecoveryV2SigningKey, Producto, RegistroPurga, SucursalCliente,
)
from .pos_recovery import canonical, digest
from .api_pos import _encode_cursor, _filter_fingerprint, _serialize_pedido
from .retencion import contenido_pedido
from .pos_recovery_v2 import (
    _b64, _identity, _prestate, _sender_ids, _strict_json, _verified_ack,
    _archive_to_api_v2, _snapshot, complete, prepare, start_freeze,
)

SENDERS = [1, 2, 3, 7, 8, 9]


@override_settings(POS_API_REQUIRE_HTTPS=False, POS_API_RATE_LIMIT_PER_MINUTE=0,
                   POS_API_MAX_WINDOW=timedelta(days=31))
class AggregateRecoveryContractTests(TestCase):
    def setUp(self):
        self.edge_id = uuid.uuid4()
        self.pos_branch_id = uuid.UUID("93a42904-4d09-4a50-94cf-2edbf5aa0e51")
        self.bearer = "synthetic-lab-aggregate-bearer-not-production"
        for sender_id in SENDERS + [4]:
            SucursalCliente.objects.create(id=sender_id, nombre=f"Sender {sender_id}", tipo="sucursal")
        self.grant = PosAggregatedCredential.objects.create(
            edge_id=self.edge_id, pos_branch_id=self.pos_branch_id, sender_ids=SENDERS,
            token_sha256=hashlib.sha256(self.bearer.encode()).hexdigest(),
            scopes=["orders:v2:read"], issued_reference="LAB-CONTRACT",
        )
        self.now = timezone.now()

    def _query(self, ids):
        return self.client.get(reverse("api_pos_pedidos_v2"), data={
            "desde": (self.now - timedelta(hours=1)).isoformat(),
            "hasta": (self.now + timedelta(hours=1)).isoformat(),
            "sucursal_id": ids,
        }, HTTP_AUTHORIZATION=f"Bearer {self.bearer}",
            HTTP_X_POS_EDGE_ID=str(self.edge_id),
            HTTP_X_POS_BRANCH_ID=str(self.pos_branch_id))

    def test_bearer_is_exactly_scoped_to_six_senders(self):
        Pedido.objects.create(
            sucursal_cliente_id=2, usuario_nombre="fixture", estado="confirmado",
            fecha_confirmacion=self.now,
        )
        Pedido.objects.create(
            sucursal_cliente_id=4, usuario_nombre="fixture", estado="confirmado",
            fecha_confirmacion=self.now,
        )
        response = self._query("1,2,3,7,8,9")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["sucursal"]["id"] for p in response.json()["data"]], [2])
        self.assertEqual(self._query("1,2,3,4,7,8,9").status_code, 403)
        wrong_edge = self.client.get(reverse("api_pos_pedidos_v2"), data={
            "desde": (self.now - timedelta(hours=1)).isoformat(),
            "hasta": (self.now + timedelta(hours=1)).isoformat(),
            "sucursal_id": "1,2,3,7,8,9",
        }, HTTP_AUTHORIZATION=f"Bearer {self.bearer}",
            HTTP_X_POS_EDGE_ID=str(uuid.uuid4()),
            HTTP_X_POS_BRANCH_ID=str(self.pos_branch_id))
        self.assertEqual(wrong_edge.status_code, 403)
        self.grant.active = False
        self.grant.save(update_fields=["active"])
        self.assertEqual(self._query("1,2,3,7,8,9").status_code, 401)

    def test_410_scope_is_opaque_to_other_senders(self):
        purge = RegistroPurga.objects.create(
            motivo="antiguedad", numero_pedidos=1, numero_items=0,
            numero_macropedidos=0,
        )
        PedidoPurgado.objects.create(
            codigo_publico=uuid.uuid4(), pedido_id_origen=9001,
            sucursal_cliente_id=4, fecha_confirmacion=self.now,
            motivo="antiguedad", registro=purge,
        )
        self.assertEqual(self._query("1,2,3,7,8,9").status_code, 200)
        PedidoPurgado.objects.create(
            codigo_publico=uuid.uuid4(), pedido_id_origen=9002,
            sucursal_cliente_id=2, fecha_confirmacion=self.now,
            motivo="antiguedad", registro=purge,
        )
        response = self._query("1,2,3,7,8,9")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "retention_gap")
        self.assertNotIn("9002", response.content.decode())

    def test_aggregate_credential_revocation_is_audited(self):
        call_command(
            "revoke_pos_v2_aggregate_credential", credential_id=str(self.grant.pk),
            referencia="LAB-REVOKE", confirm=True,
        )
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.revocation_reference, "LAB-REVOKE")
        self.assertFalse(self.grant.active)
        self.assertEqual(self._query("1,2,3,7,8,9").status_code, 401)

    def test_static_crypto_vector_and_negative_mutation(self):
        path = settings.BASE_DIR / "docs/integracion/schemas/recovery-v2-crypto-fixture.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for name, public_name in (("manifest", "pedidos_public_key_b64"),
                                  ("edge_ack", "edge_public_key_b64"),
                                  ("receipt", "pedidos_public_key_b64")):
            envelope = fixture[name]
            key = base64.urlsafe_b64decode(fixture[public_name] + "=")
            signature = base64.urlsafe_b64decode(envelope["signature_b64"] + "==")
            Ed25519PublicKey.from_public_bytes(key).verify(signature, canonical(envelope["payload"]))
            with self.assertRaises(Exception):
                Ed25519PublicKey.from_public_bytes(key).verify(
                    signature, canonical({**envelope["payload"], "sender_ids": [1]})
                )
        self.assertEqual(digest(canonical(fixture["manifest"]) + b"\n"),
                         fixture["manifest_sha256"])
        self.assertEqual(digest(fixture["recovered_orders_jsonl"].encode()),
                         fixture["manifest"]["payload"]["recovered_orders_sha256"])
        self.assertEqual(digest(fixture["orders_jsonl"].encode()),
                         fixture["manifest"]["payload"]["orders_sha256"])
        self.assertEqual(digest(fixture["tombstones_json"].encode()),
                         fixture["manifest"]["payload"]["tombstones_sha256"])
        recovered = json.loads(fixture["recovered_orders_jsonl"])
        current = json.loads(fixture["orders_jsonl"])
        received = fixture["edge_ack"]["payload"]["received"]
        self.assertEqual(received[0]["order_sha256"], digest(canonical(recovered["order"])))
        self.assertEqual(received[1]["order_sha256"], digest(canonical(current["order"])))

    def test_prestate_full_even_with_empty_cursor(self):
        desde = self.now - timedelta(hours=1)
        hasta = self.now + timedelta(hours=1)
        state = {
            "cursor": "", "ultimo_cursor_confirmado": None,
            "ventana_desde": desde.isoformat(), "ventana_hasta": hasta.isoformat(),
            "agua_alta_hasta": None, "sucursales_origen": SENDERS,
            "estado": "reconciliacion", "version_api": "v2",
        }
        accepted = _prestate(canonical(state), SENDERS, desde, hasta)
        self.assertEqual(accepted["cursor"], "")
        with self.assertRaises(CommandError):
            _prestate(canonical({k: v for k, v in state.items() if k != "agua_alta_hasta"}),
                      SENDERS, desde, hasta)
        with self.assertRaises(CommandError):
            _prestate(canonical({**state, "sucursales_origen": [1, 2, 3]}),
                      SENDERS, desde, hasta)
        with self.assertRaises(CommandError):
            _prestate(canonical({**state, "agua_alta_hasta": hasta.isoformat()}),
                      SENDERS, desde, hasta)

    def test_duplicate_json_nonfinite_and_compound_identity(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'):
            with self.assertRaises(CommandError):
                _strict_json(raw)
        self.assertEqual(_sender_ids(SENDERS), SENDERS)
        for invalid in ([1, 2, 2], [1, 0], [2, 1], [True]):
            with self.assertRaises(CommandError):
                _sender_ids(invalid)
        public_id = str(uuid.uuid4())
        self.assertNotEqual(_identity({"sender_id": 1, "codigo_publico": public_id}),
                            _identity({"sender_id": 2, "codigo_publico": public_id}))

    def test_ack_signature_scope_nonce_and_wrong_key(self):
        private = Ed25519PrivateKey.generate()
        key = PosRecoveryV2SigningKey.objects.create(
            key_id=uuid.uuid4(), kind="edge", edge_id=self.edge_id,
            pos_branch_id=self.pos_branch_id, sender_ids=SENDERS,
            public_key_b64=_b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
            issued_reference="LAB-EDGE-KEY",
        )
        freeze = PosRecoveryV2Freeze.objects.create(
            freeze_id=uuid.uuid4(), sender_ids=SENDERS, reference="LAB-FREEZE"
        )
        pedidos_key = PosRecoveryV2SigningKey.objects.create(
            key_id=uuid.uuid4(), kind="pedidos", edge_id=self.edge_id,
            pos_branch_id=self.pos_branch_id, sender_ids=SENDERS,
            public_key_b64=_b64(Ed25519PrivateKey.generate().public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw)), issued_reference="LAB-PEDIDOS-KEY",
        )
        record = PosRecoveryV2.objects.create(
            recovery_id=uuid.uuid4(), edge_id=self.edge_id, pos_branch_id=self.pos_branch_id,
            sender_ids=SENDERS, desde=self.now - timedelta(hours=1), hasta=self.now,
            pos_prestate_sha256="a" * 64, purge_epoch="b" * 64,
            snapshot_sha256="c" * 64, manifest_sha256="d" * 64,
            orders_sha256="e" * 64, tombstones_sha256="f" * 64,
            orders_count=0, tombstones_count=0, freeze=freeze,
            pedidos_key=pedidos_key, opened_reference="LAB-RECOVERY",
        )
        payload = {
            "type": "pedidos.edge.recovery_ack.v2", "key_id": str(key.pk),
            "nonce": str(uuid.uuid4()), "issued_at": self.now.isoformat(),
            "recovery_id": str(record.pk), "edge_id": str(self.edge_id),
            "pos_branch_id": str(self.pos_branch_id), "sender_ids": SENDERS,
            "pos_prestate_sha256": record.pos_prestate_sha256,
            "snapshot_sha256": record.snapshot_sha256,
            "manifest_sha256": record.manifest_sha256,
            "orders_sha256": record.orders_sha256,
            "tombstones_sha256": record.tombstones_sha256,
            "received": [], "unresolved": [],
        }
        signed = {"payload": payload, "signature_b64": _b64(private.sign(canonical(payload)))}
        ack, matched, nonce = _verified_ack(canonical(signed), record)
        self.assertEqual((ack, matched, nonce), (payload, key, uuid.UUID(payload["nonce"])))
        with self.assertRaises(CommandError):
            _verified_ack(canonical({"payload": {**payload, "sender_ids": [1]},
                                    "signature_b64": signed["signature_b64"]}), record)
        with self.assertRaises(CommandError):
            _verified_ack(canonical({"payload": payload, "signature_b64": _b64(
                Ed25519PrivateKey.generate().sign(canonical(payload)))}), record)
        PosRecoveryV2Nonce.objects.create(key=key, nonce=nonce, recovery=record)
        self.assertTrue(PosRecoveryV2Nonce.objects.filter(key=key, nonce=nonce).exists())


@skipUnless(os.name == "posix" and connection.vendor == "postgresql",
            "Recovery-v2 requires POSIX private files and PostgreSQL freeze triggers")
class AggregateRecoveryPostgresTests(TransactionTestCase):
    def setUp(self):
        for sender_id in SENDERS:
            SucursalCliente.objects.create(
                id=sender_id, nombre=f"Synthetic sender {sender_id}", tipo="sucursal"
            )
        self.edge_id = uuid.uuid4()
        self.branch_id = uuid.UUID("93a42904-4d09-4a50-94cf-2edbf5aa0e51")
        self.now = timezone.now()
        self.desde = self.now - timedelta(hours=1)
        self.hasta = self.now + timedelta(hours=1)
        self.order = Pedido.objects.create(
            sucursal_cliente_id=2, usuario_nombre="synthetic", estado="confirmado",
            fecha_confirmacion=self.now,
        )
        self.product = Producto.objects.create(nombre="Synthetic recovery product")
        self.item = ItemPedido.objects.create(
            pedido=self.order, producto=self.product, cantidad=Decimal("1.000"),
            precio_unitario=Decimal("5.00"),
        )
        self.purged_id = uuid.uuid4()
        self.export_id = uuid.uuid4()
        self.archive_order = {
            "id": 991, "codigo_publico": str(self.purged_id),
            "sucursal": {"id": 1, "nombre": "synthetic", "tipo": "sucursal"},
            "fecha_confirmacion": self.now.isoformat(), "total": "0.00",
            "estado": "confirmado", "eliminado": False, "items": [],
        }
        archive_lines = canonical(self.archive_order) + b"\n"
        archive_manifest = {
            "lote_id": str(self.export_id), "sha256_pedidos_jsonl": digest(archive_lines),
        }
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", canonical(archive_manifest) + b"\n")
            archive.writestr("pedidos.jsonl", archive_lines)
        self.archive_bytes = archive_buffer.getvalue()
        export = ExportacionRetencion.objects.create(
            id=self.export_id, estado=ExportacionRetencion.Estado.CONFIRMADA,
            desde_recepcion=self.desde, hasta_recepcion=self.hasta,
            sha256_archivo=digest(self.archive_bytes),
            sha256_contenido=digest(archive_lines), numero_pedidos=1,
            numero_items=0,
        )
        purge = RegistroPurga.objects.create(
            motivo="exportacion", numero_pedidos=1, numero_items=0,
            numero_macropedidos=0, exportacion=export,
        )
        PedidoPurgado.objects.create(
            codigo_publico=self.purged_id, pedido_id_origen=991,
            sucursal_cliente_id=1, fecha_confirmacion=self.now,
            motivo="exportacion", registro=purge, exportacion=export,
        )
        PosAggregatedCredential.objects.create(
            edge_id=self.edge_id, pos_branch_id=self.branch_id, sender_ids=SENDERS,
            token_sha256=hashlib.sha256(b"synthetic-aggregate-bearer").hexdigest(),
            scopes=["orders:v2:read"], issued_reference="LAB-GRANT",
        )
        self.edge_private = Ed25519PrivateKey.generate()
        self.server_private = Ed25519PrivateKey.generate()
        self.edge_key = self._key("edge", self.edge_private)
        self.server_key = self._key("pedidos", self.server_private)
        self.temp = tempfile.TemporaryDirectory(prefix="tcys-recovery-v2-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self._file("pedidos.key", self.server_private.private_bytes_raw())
        self._file(f"{self.export_id}.zip", self.archive_bytes)
        self._file("prestate.json", canonical({
            "cursor": "", "ultimo_cursor_confirmado": None,
            "ventana_desde": self.desde.isoformat(),
            "ventana_hasta": self.hasta.isoformat(),
            "agua_alta_hasta": None, "sucursales_origen": SENDERS,
            "estado": "reconciliacion", "version_api": "v2",
        }))
        self.freeze_id = uuid.uuid4()
        start_freeze(freeze_id=self.freeze_id, sender_ids=SENDERS, reference="LAB-FREEZE")
        self.recovery_id = uuid.uuid4()

    def _key(self, kind, private):
        return PosRecoveryV2SigningKey.objects.create(
            key_id=uuid.uuid4(), kind=kind, edge_id=self.edge_id,
            pos_branch_id=self.branch_id, sender_ids=SENDERS,
            public_key_b64=_b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
            issued_reference=f"LAB-{kind}",
        )

    def _file(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return path

    def _prepare(self):
        return prepare(
            recovery_id=self.recovery_id, edge_id=self.edge_id,
            pos_branch_id=self.branch_id, sender_ids=SENDERS,
            desde=self.desde.isoformat(), hasta=self.hasta.isoformat(),
            prestate_file=self.root / "prestate.json", freeze_id=self.freeze_id,
            pedidos_key_id=self.server_key.pk, private_key_file=self.root / "pedidos.key",
            output=self.root / "baseline.zip", archive_dir=self.root,
            reference="LAB-OPEN",
        )

    def _ack(self, record, *, include_tombstone=True, unresolved=False):
        rows, _ = _snapshot(SENDERS, self.desde, self.hasta)
        received = [{
            "sender_id": row["sender_id"],
            "codigo_publico": row["order"]["codigo_publico"],
            "order_sha256": digest(canonical(row["order"])),
        } for row in rows]
        if include_tombstone:
            received.append({
                "sender_id": 1, "codigo_publico": str(self.purged_id),
                "order_sha256": digest(canonical(_archive_to_api_v2(self.archive_order))),
            })
        received.sort(key=lambda item: (item["sender_id"], item["codigo_publico"]))
        payload = {
            "type": "pedidos.edge.recovery_ack.v2", "key_id": str(self.edge_key.pk),
            "nonce": str(uuid.uuid4()), "issued_at": timezone.now().isoformat(),
            "recovery_id": str(record.pk), "edge_id": str(self.edge_id),
            "pos_branch_id": str(self.branch_id), "sender_ids": SENDERS,
            "pos_prestate_sha256": record.pos_prestate_sha256,
            "snapshot_sha256": record.snapshot_sha256,
            "manifest_sha256": record.manifest_sha256,
            "orders_sha256": record.orders_sha256,
            "tombstones_sha256": record.tombstones_sha256,
            "received": received,
            "unresolved": [{"sender_id": 1, "codigo_publico": str(self.purged_id)}]
            if unresolved else [],
        }
        signed = {"payload": payload,
                  "signature_b64": _b64(self.edge_private.sign(canonical(payload)))}
        return self._file("ack.json", canonical(signed))

    def _complete(self, record, ack_file, receipt_name="receipt.json"):
        return complete(
            recovery_id=record.pk, snapshot_file=self.root / "baseline.zip",
            snapshot_sha256=record.snapshot_sha256, edge_ack_file=ack_file,
            edge_ack_sha256=digest(ack_file.read_bytes()), archive_dir=self.root,
            private_key_file=self.root / "pedidos.key",
            receipt_file=self.root / receipt_name, reference="LAB-CLOSE",
        )

    def test_freeze_signed_baseline_ack_and_receipt(self):
        self.assertEqual(_archive_to_api_v2(contenido_pedido(self.order)),
                         _serialize_pedido(self.order, version=2))
        with self.assertRaises(DatabaseError), transaction.atomic():
            Pedido.objects.create(
                sucursal_cliente_id=3, usuario_nombre="blocked", estado="confirmado",
                fecha_confirmacion=self.now,
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            ItemPedido.objects.filter(pk=self.item.pk).update(precio_unitario="6.00")
        with self.assertRaises(DatabaseError), transaction.atomic():
            PedidoPurgado.objects.filter(pk=self.purged_id).update(motivo="antiguedad")
        record = self._prepare()
        self.assertEqual((record.orders_count, record.tombstones_count), (1, 1))
        ack = self._ack(record)
        closed = self._complete(record, ack)
        self.assertEqual(closed.estado, "completada")
        self.assertEqual(self._complete(record, ack).pk, record.pk)
        receipt = _strict_json((self.root / "receipt.json").read_bytes())
        self.assertEqual(receipt["payload"]["status"], "completada")
        self.assertEqual(receipt["payload"]["sender_ids"], SENDERS)
        Ed25519PublicKey.from_public_bytes(
            self.server_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).verify(
            base64.urlsafe_b64decode(receipt["signature_b64"] + "=="),
            canonical(receipt["payload"]),
        )

    def test_nonempty_midwindow_cursor_is_preserved_in_signed_prestate(self):
        cursor = _encode_cursor(
            self.order,
            _filter_fingerprint(self.desde, self.hasta, SENDERS, version=2),
            version=2, purge_epoch="0" * 64,
        )
        prestate = _strict_json((self.root / "prestate.json").read_bytes())
        prestate["cursor"] = cursor
        prestate["ultimo_cursor_confirmado"] = cursor
        self._file("prestate.json", canonical(prestate))
        record = self._prepare()
        with zipfile.ZipFile(self.root / "baseline.zip") as archive:
            manifest = _strict_json(archive.read("manifest.json"))["payload"]
        self.assertEqual(manifest["pos_prestate"]["cursor"], cursor)
        self.assertEqual(manifest["pos_prestate"]["ultimo_cursor_confirmado"], cursor)
        self.assertEqual(record.pos_prestate_sha256,
                         digest(canonical(manifest["pos_prestate"])))

    def test_terminal_historical_order_cannot_be_reopened(self):
        terminal = {**self.archive_order, "estado": "recibido"}
        self.assertIsNone(_archive_to_api_v2(terminal))
        terminal["estado"] = "enviado"
        self.assertIsNone(_archive_to_api_v2(terminal))

    def test_freeze_drains_an_inflight_writer_before_snapshot(self):
        PosRecoveryV2Freeze.objects.filter(pk=self.freeze_id).update(active=False)
        writer_entered = threading.Event()
        writer_release = threading.Event()
        freeze_finished = threading.Event()
        failures = []
        next_freeze = uuid.uuid4()

        def writer():
            try:
                with transaction.atomic():
                    Pedido.objects.filter(pk=self.order.pk).update(usuario_nombre="committed-before-freeze")
                    writer_entered.set()
                    if not writer_release.wait(5):
                        raise AssertionError("writer release timed out")
            except Exception as exc:
                failures.append(exc)
            finally:
                connection.close()

        def freezer():
            try:
                start_freeze(freeze_id=next_freeze, sender_ids=SENDERS, reference="LAB-DRAIN")
            except Exception as exc:
                failures.append(exc)
            finally:
                freeze_finished.set()
                connection.close()

        writer_thread = threading.Thread(target=writer)
        freezer_thread = threading.Thread(target=freezer)
        writer_thread.start()
        self.assertTrue(writer_entered.wait(5))
        freezer_thread.start()
        try:
            self.assertFalse(freeze_finished.wait(0.2))
        finally:
            writer_release.set()
            writer_thread.join(5)
            freezer_thread.join(5)
        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(freezer_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(PosRecoveryV2Freeze.objects.get(pk=next_freeze).active)
        self.order.refresh_from_db()
        self.assertEqual(self.order.usuario_nombre, "committed-before-freeze")
        self.freeze_id = next_freeze
        record = self._prepare()
        self.assertEqual(record.orders_count, 1)

    def test_incomplete_archive_manual_and_invalid_signature_no_close(self):
        record = self._prepare()
        ack = self._ack(record)
        raw = _strict_json(ack.read_bytes())
        raw["signature_b64"] = _b64(Ed25519PrivateKey.generate().sign(canonical(raw["payload"])))
        self._file("bad-ack.json", canonical(raw))
        with self.assertRaises(CommandError):
            self._complete(record, self.root / "bad-ack.json")
        self.assertEqual(PosRecoveryV2.objects.get(pk=record.pk).estado, "pendiente")
        (self.root / f"{self.export_id}.zip").unlink()
        manual = self._complete(record, ack)
        self.assertEqual(manual.estado, "intervencion_manual")
        self.assertEqual(self._complete(record, ack).pk, record.pk)
        self.assertIsNone(manual.closed_at)
        self.assertIsNone(_strict_json((self.root / "receipt.json").read_bytes())["payload"]["next_desde"])

    def test_baseline_without_archive_is_incomplete_and_manual(self):
        (self.root / f"{self.export_id}.zip").unlink()
        record = self._prepare()
        self.assertEqual(record.recovered_orders_count, 0)
        ack = self._ack(record, include_tombstone=False, unresolved=True)
        result = self._complete(record, ack)
        self.assertEqual(result.estado, "intervencion_manual")
        self.assertIsNone(result.closed_at)

    def test_nonce_replay_wrong_edge_and_wrong_sender_blocked(self):
        record = self._prepare()
        ack = self._ack(record)
        raw = _strict_json(ack.read_bytes())
        wrong_sender = {**raw["payload"], "sender_ids": [1, 2, 3]}
        self._file("wrong-sender.json", canonical({
            "payload": wrong_sender,
            "signature_b64": _b64(self.edge_private.sign(canonical(wrong_sender))),
        }))
        with self.assertRaises(CommandError):
            self._complete(record, self.root / "wrong-sender.json")
        wrong_edge = {**raw["payload"], "edge_id": str(uuid.uuid4())}
        self._file("wrong-edge.json", canonical({
            "payload": wrong_edge,
            "signature_b64": _b64(self.edge_private.sign(canonical(wrong_edge))),
        }))
        with self.assertRaises(CommandError):
            self._complete(record, self.root / "wrong-edge.json")
        other = PosRecoveryV2.objects.create(
            recovery_id=uuid.uuid4(), edge_id=record.edge_id,
            pos_branch_id=record.pos_branch_id, sender_ids=record.sender_ids,
            desde=record.desde, hasta=record.hasta,
            pos_prestate_sha256=record.pos_prestate_sha256,
            purge_epoch=record.purge_epoch, snapshot_sha256=record.snapshot_sha256,
            manifest_sha256=record.manifest_sha256, orders_sha256=record.orders_sha256,
            tombstones_sha256=record.tombstones_sha256,
            orders_count=record.orders_count, tombstones_count=record.tombstones_count,
            freeze=record.freeze, pedidos_key=record.pedidos_key,
            opened_reference="LAB-OTHER",
        )
        PosRecoveryV2Nonce.objects.create(
            key=self.edge_key, nonce=uuid.UUID(raw["payload"]["nonce"]), recovery=other
        )
        with self.assertRaises(CommandError):
            self._complete(record, ack)
        self.assertEqual(PosRecoveryV2.objects.get(pk=record.pk).estado, "pendiente")

    def test_revoked_edge_key_rejected(self):
        record = self._prepare()
        ack = self._ack(record)
        self.edge_key.active = False
        self.edge_key.revoked_at = timezone.now()
        self.edge_key.save(update_fields=["active", "revoked_at"])
        with self.assertRaises(CommandError):
            self._complete(record, ack)
        self.assertFalse((self.root / "receipt.json").exists())

    def test_catalog_drift_under_freeze_aborts_close(self):
        record = self._prepare()
        ack = self._ack(record)
        self.product.nombre = "Changed while barrier active"
        self.product.save(update_fields=["nombre"])
        with self.assertRaises(CommandError):
            self._complete(record, ack)
        self.assertEqual(PosRecoveryV2.objects.get(pk=record.pk).estado, "pendiente")

    def test_orphan_snapshot_and_receipt_retry_identical(self):
        class SimulatedCrash(Exception):
            pass

        with self.assertRaises(SimulatedCrash), transaction.atomic():
            self._prepare()
            raise SimulatedCrash
        self.assertTrue((self.root / "baseline.zip").exists())
        self.assertFalse(PosRecoveryV2.objects.filter(pk=self.recovery_id).exists())
        record = self._prepare()
        ack = self._ack(record)
        with self.assertRaises(SimulatedCrash), transaction.atomic():
            self._complete(record, ack)
            raise SimulatedCrash
        self.assertTrue((self.root / "receipt.json").exists())
        self.assertEqual(PosRecoveryV2.objects.get(pk=record.pk).estado, "pendiente")
        closed = self._complete(record, ack)
        self.assertEqual(closed.estado, "completada")
