"""Exact mixed sender scope for the Arboledas POS consumer in API v2."""

import hashlib
import io
import os
import stat
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    ItemPedido, Pedido, PedidoPurgado, PosAggregatedCredential, Producto,
    RegistroPurga, SucursalCliente,
)
from .pos_recovery_v2 import _approved_senders, _archive_to_api_v2
from .retencion import contenido_pedido
from .api_pos import _serialize_pedido


SENDERS = list(range(1, 11))
SUCURSAL_IDS = {1, 2, 3, 7, 8, 9}
TOKEN = "synthetic-mixed-senders-v2-bearer-not-production"


@override_settings(
    POS_API_REQUIRE_HTTPS=False,
    POS_API_RATE_LIMIT_PER_MINUTE=0,
    POS_API_MAX_WINDOW=timedelta(days=31),
)
class MixedSendersV2Tests(TestCase):
    def setUp(self):
        self.edge_id = uuid.uuid4()
        self.pos_branch_id = uuid.UUID("93a42904-4d09-4a50-94cf-2edbf5aa0e51")
        self.now = timezone.now().replace(microsecond=123456)
        self.url = reverse("api_pos_pedidos_v2")
        for sender_id in SENDERS:
            SucursalCliente.objects.create(
                id=sender_id,
                nombre=f"Remitente sintético {sender_id}",
                tipo=(SucursalCliente.Tipo.SUCURSAL if sender_id in SUCURSAL_IDS
                      else SucursalCliente.Tipo.CLIENTE_MAYORISTA),
            )
        SucursalCliente.objects.create(
            id=11, nombre="Remitente fuera de alcance",
            tipo=SucursalCliente.Tipo.CLIENTE_MAYORISTA,
        )
        self.credential = PosAggregatedCredential.objects.create(
            edge_id=self.edge_id,
            pos_branch_id=self.pos_branch_id,
            sender_ids=SENDERS,
            token_sha256=hashlib.sha256(TOKEN.encode("ascii")).hexdigest(),
            scopes=["orders:v2:read"],
            issued_reference="SYNTHETIC-MIXED-SENDERS",
        )
        self.product = Producto.objects.create(
            nombre="Producto sintético", cantidad_por_precio=Decimal("1.000")
        )

    def _request(self, sender_ids=SENDERS, **extra):
        data = {
            "desde": (self.now - timedelta(hours=1)).isoformat(),
            "hasta": (self.now + timedelta(hours=1)).isoformat(),
            "sucursal_id": ",".join(map(str, sender_ids)),
        }
        data.update(extra)
        return self.client.get(
            self.url, data=data,
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
            HTTP_X_POS_EDGE_ID=str(self.edge_id),
            HTTP_X_POS_BRANCH_ID=str(self.pos_branch_id),
        )

    def _order(self, sender_id):
        order = Pedido.objects.create(
            sucursal_cliente_id=sender_id,
            usuario_nombre="fixture",
            estado=Pedido.Estado.CONFIRMADO,
            fecha_confirmacion=self.now,
            total=Decimal("8.00"),
        )
        ItemPedido.objects.create(
            pedido=order,
            producto=self.product,
            cantidad=Decimal("1.000"),
            precio_unitario=Decimal("8.00"),
        )
        return order

    def _purge(self, sender_id):
        record = RegistroPurga.objects.create(
            motivo="antiguedad", numero_pedidos=1, numero_items=0,
            numero_macropedidos=0,
        )
        PedidoPurgado.objects.create(
            codigo_publico=uuid.uuid4(),
            pedido_id_origen=9000 + sender_id,
            sucursal_cliente_id=sender_id,
            fecha_confirmacion=self.now,
            motivo="antiguedad",
            registro=record,
        )

    def test_mixed_ten_sender_ids_return_both_types_without_shape_change(self):
        self._order(2)
        self._order(4)
        self._order(10)
        response = self._request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], "v2")
        rows = response.json()["data"]
        self.assertEqual(
            [(row["sucursal"]["id"], row["sucursal"]["tipo"]) for row in rows],
            [(2, "sucursal"), (4, "cliente_mayorista"), (10, "cliente_mayorista")],
        )
        self.assertTrue(all("codigo_publico" in row for row in rows))

    def test_exact_scope_edge_branch_and_type_fail_closed(self):
        self._order(4)
        self._order(11)
        self.assertEqual(self._request(sender_ids=SENDERS + [11]).status_code, 403)
        self.assertEqual(self._request(sender_ids=[11]).status_code, 403)
        self.assertEqual(self._request().status_code, 200)
        wrong_edge = self.client.get(
            self.url, data={
                "desde": (self.now - timedelta(hours=1)).isoformat(),
                "hasta": (self.now + timedelta(hours=1)).isoformat(),
                "sucursal_id": ",".join(map(str, SENDERS)),
            },
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
            HTTP_X_POS_EDGE_ID=str(uuid.uuid4()),
            HTTP_X_POS_BRANCH_ID=str(self.pos_branch_id),
        )
        self.assertEqual(wrong_edge.status_code, 403)
        self.credential.scopes = []
        self.credential.save(update_fields=["scopes"])
        self.assertEqual(self._request().status_code, 403)
        self.credential.scopes = ["orders:v2:read"]
        self.credential.save(update_fields=["scopes"])
        sender = SucursalCliente.objects.get(pk=4)
        sender.tipo = "unexpected"
        sender.save(update_fields=["tipo"])
        self.assertEqual(self._request().status_code, 403)
        self.assertFalse(_approved_senders(SENDERS))

    def test_cursor_and_retention_gap_cover_majorista_but_not_outside_scope(self):
        self._order(2)
        self._order(4)
        first = self._request(limite="1")
        self.assertEqual(first.status_code, 200)
        cursor = first.json()["page"]["next_cursor"]
        self.assertTrue(cursor)
        second = self._request(limite="1", cursor=cursor)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["data"][0]["sucursal"]["id"], 4)
        self._purge(11)
        self.assertEqual(self._request(limite="1", cursor=cursor).status_code, 200)
        self._purge(4)
        gap = self._request(limite="1", cursor=cursor)
        self.assertEqual(gap.status_code, 410)
        self.assertEqual(gap.json()["error"]["code"], "retention_gap")

    def test_all_ten_sender_types_are_recovery_eligible(self):
        self.assertTrue(_approved_senders(SENDERS))
        self.assertFalse(_approved_senders(SENDERS + [12]))

    def test_archived_majorista_order_retains_type_for_v2_recovery(self):
        order = self._order(4)
        archived = contenido_pedido(order)
        self.assertEqual(archived["sucursal"]["tipo"], "cliente_mayorista")
        self.assertEqual(_archive_to_api_v2(archived), _serialize_pedido(order, version=2))
        archived["sucursal"] = {**archived["sucursal"], "tipo": "unexpected"}
        self.assertIsNone(_archive_to_api_v2(archived))

    def test_aggregate_issuer_accepts_mixed_ids_and_rejects_unexpected_type(self):
        self.credential.delete()
        with patch("pedidos.management.commands.issue_pos_v2_aggregate_credential._write_private") as writer:
            output = io.StringIO()
            call_command(
                "issue_pos_v2_aggregate_credential",
                edge_id=str(self.edge_id),
                pos_branch_id=str(self.pos_branch_id),
                sender_ids=",".join(map(str, SENDERS)),
                output="/private/synthetic-only.token",
                reference="SYNTHETIC-MIXED-ISSUE",
                confirm=True,
                stdout=output,
            )
            self.assertEqual(writer.call_count, 1)
            token = writer.call_args.args[1].decode("ascii").strip()
            self.assertNotIn(token, output.getvalue())
            grant = PosAggregatedCredential.objects.get()
            self.assertEqual(grant.sender_ids, SENDERS)
            self.assertEqual(grant.scopes, ["orders:v2:read"])
            self.assertEqual(grant.edge_id, self.edge_id)
            self.assertEqual(grant.pos_branch_id, self.pos_branch_id)
            self.assertEqual(grant.token_sha256, hashlib.sha256(token.encode()).hexdigest())
            grant.delete()
            sender = SucursalCliente.objects.get(pk=4)
            sender.tipo = "unexpected"
            sender.save(update_fields=["tipo"])
            with self.assertRaises(CommandError):
                call_command(
                    "issue_pos_v2_aggregate_credential",
                    edge_id=str(self.edge_id),
                    pos_branch_id=str(self.pos_branch_id),
                    sender_ids=",".join(map(str, SENDERS)),
                    output="/private/synthetic-rejected.token",
                    reference="SYNTHETIC-REJECT",
                    confirm=True,
                    stdout=io.StringIO(),
                )
            self.assertEqual(writer.call_count, 1)

    @skipUnless(os.name == "posix", "Secret delivery requires POSIX 0600")
    def test_issue_aggregate_credential_for_mixed_senders(self):
        self.credential.delete()
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            target = Path(directory) / "orders.token"
            output = io.StringIO()
            call_command(
                "issue_pos_v2_aggregate_credential",
                edge_id=str(self.edge_id),
                pos_branch_id=str(self.pos_branch_id),
                sender_ids=",".join(map(str, SENDERS)),
                output=str(target),
                reference="SYNTHETIC-MIXED-ISSUE",
                confirm=True,
                stdout=output,
            )
            token = target.read_text(encoding="ascii").strip()
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertNotIn(token, output.getvalue())
            grant = PosAggregatedCredential.objects.get()
            self.assertEqual(grant.sender_ids, SENDERS)
            self.assertEqual(grant.scopes, ["orders:v2:read"])
            self.assertEqual(grant.edge_id, self.edge_id)
            self.assertEqual(grant.pos_branch_id, self.pos_branch_id)
            self.assertEqual(grant.token_sha256, hashlib.sha256(token.encode()).hexdigest())
