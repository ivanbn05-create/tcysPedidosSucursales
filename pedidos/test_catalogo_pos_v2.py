"""Catálogo mayorista v2 para el POS consumidor (espejo automático de precios)."""

import hashlib
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from .models import PosAggregatedCredential, Precio, Producto, SucursalCliente


SENDERS = list(range(1, 11))
SUCURSAL_IDS = {1, 2, 3, 7, 8, 9}
TOKEN = "synthetic-catalog-v2-bearer-not-production"


@override_settings(
    POS_API_REQUIRE_HTTPS=False,
    POS_API_RATE_LIMIT_PER_MINUTE=0,
    POS_API_MAX_WINDOW=timedelta(days=31),
)
class CatalogoPosV2Tests(TestCase):
    def setUp(self):
        self.edge_id = uuid.uuid4()
        self.pos_branch_id = uuid.uuid4()
        self.url = reverse("api_pos_catalogo_v2")
        for sender_id in SENDERS + [11]:
            SucursalCliente.objects.create(
                id=sender_id,
                nombre=f" Remitente sintético {sender_id} ",
                tipo=(SucursalCliente.Tipo.SUCURSAL if sender_id in SUCURSAL_IDS
                      else SucursalCliente.Tipo.CLIENTE_MAYORISTA),
                email=f"remitente{sender_id}@example.invalid",
            )
        PosAggregatedCredential.objects.create(
            edge_id=self.edge_id,
            pos_branch_id=self.pos_branch_id,
            sender_ids=SENDERS,
            token_sha256=hashlib.sha256(TOKEN.encode("ascii")).hexdigest(),
            scopes=["orders:v2:read"],
            issued_reference="SYNTHETIC-CATALOG",
        )
        self.barbacoa = Producto.objects.create(
            nombre="Barbacoa", nombre_ticket="BARBACOA", unidad_abreviatura="KG",
            cantidad_por_precio=Decimal("1.000"), orden=1,
        )
        self.chile = Producto.objects.create(
            nombre="Chile güero", nombre_ticket="", unidad_abreviatura="PZA",
            cantidad_por_precio=Decimal("30.000"), orden=2,
        )
        Precio.objects.create(
            producto=self.barbacoa, sucursal_cliente_id=4, precio_unitario=Decimal("300.00"),
            nombre_ticket="BARBACOA.M", fecha_vigencia=date(2026, 1, 1),
        )
        Precio.objects.create(
            producto=self.barbacoa, sucursal_cliente_id=4, precio_unitario=Decimal("320.00"),
            nombre_ticket="", fecha_vigencia=date(2026, 9, 1),
        )
        Precio.objects.create(
            producto=self.chile, sucursal_cliente_id=1, precio_unitario=Decimal("80.00"),
            fecha_vigencia=date(2026, 1, 1),
        )
        Precio.objects.create(
            producto=self.chile, sucursal_cliente_id=11, precio_unitario=Decimal("1.00"),
            fecha_vigencia=date(2026, 1, 1),
        )

    def _get(self, sender_ids=SENDERS, **headers):
        params = {"sucursal_id": ",".join(map(str, sender_ids))} if sender_ids is not None else {}
        return self.client.get(
            self.url,
            data=params,
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
            HTTP_X_POS_EDGE_ID=str(self.edge_id),
            HTTP_X_POS_BRANCH_ID=str(self.pos_branch_id),
            **headers,
        )

    def test_snapshot_del_alcance_con_etag_fuerte_y_sin_datos_personales(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response["Cache-Control"], "no-store")
        body = response.content
        self.assertEqual(response["ETag"], f'"{hashlib.sha256(body).hexdigest()}"')
        datos = json.loads(body)
        self.assertEqual(datos["schema"], "tcys.pedidos.catalogo-pos.v1")
        self.assertEqual([r["id"] for r in datos["remitentes"]], SENDERS)
        self.assertEqual(datos["remitentes"][0]["nombre"], "Remitente sintético 1")
        self.assertNotIn("example.invalid", body.decode("utf-8"))
        self.assertEqual(
            [(p["remitente_id"], p["producto_id"], p["importe"], p["nombre_ticket"], p["vigente_desde"])
             for p in datos["precios"]],
            [
                (1, self.chile.id, "80.00", "Chile güero", "2026-01-01"),
                (4, self.barbacoa.id, "300.00", "BARBACOA.M", "2026-01-01"),
                (4, self.barbacoa.id, "320.00", "BARBACOA", "2026-09-01"),
            ],
        )
        chile = next(p for p in datos["productos"] if p["id"] == self.chile.id)
        self.assertEqual(chile["cantidad_por_precio"], "30.000")
        self.assertEqual(chile["unidad"], "PZA")
        # Canónico: las claves ordenadas y sin espacios reproducen el mismo SHA.
        canonico = json.dumps(datos, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(canonico.encode("utf-8"), body)

    def test_if_none_match_devuelve_304_y_un_cambio_de_precio_cambia_el_etag(self):
        primero = self._get()
        etag = primero["ETag"]
        sin_cambios = self._get(HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(sin_cambios.status_code, 304)
        self.assertEqual(sin_cambios.content, b"")
        self.assertEqual(sin_cambios["ETag"], etag)
        Precio.objects.filter(sucursal_cliente_id=1).update(precio_unitario=Decimal("85.00"))
        cambiado = self._get(HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(cambiado.status_code, 200)
        self.assertNotEqual(cambiado["ETag"], etag)

    def test_alcance_ajeno_credencial_invalida_o_parametros_extra_fallan_cerrado(self):
        self.assertEqual(self._get(SENDERS + [11]).status_code, 403)
        self.assertEqual(
            self.client.get(self.url, {"sucursal_id": "1"}, HTTP_AUTHORIZATION="Bearer otro").status_code,
            401,
        )
        malo = self.client.get(
            self.url, {"sucursal_id": "1"},
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
            HTTP_X_POS_EDGE_ID=str(uuid.uuid4()),
            HTTP_X_POS_BRANCH_ID=str(self.pos_branch_id),
        )
        self.assertEqual(malo.status_code, 403)
        extra = self.client.get(
            self.url, {"sucursal_id": "1", "desde": "x"},
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
            HTTP_X_POS_EDGE_ID=str(self.edge_id),
            HTTP_X_POS_BRANCH_ID=str(self.pos_branch_id),
        )
        self.assertEqual(extra.status_code, 400)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_subconjunto_del_alcance_filtra_remitentes_y_precios(self):
        datos = self._get([4]).json()
        self.assertEqual([r["id"] for r in datos["remitentes"]], [4])
        self.assertEqual({p["remitente_id"] for p in datos["precios"]}, {4})
        self.assertEqual(len(datos["productos"]), 2)

    def test_sucursal_id_es_obligatorio_como_en_pedidos(self):
        self.assertEqual(self._get(sender_ids=None).status_code, 400)
