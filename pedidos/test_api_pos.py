from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import ItemPedido, Pedido, Producto, SucursalCliente


TOKEN = "token-pos-pruebas-abcdefghijklmnopqrstuvwxyz-0123456789"


@override_settings(
    POS_API_TOKENS=(TOKEN,),
    POS_API_ALLOWED_SUCURSAL_IDS=(101, 102, 103),
    POS_API_REQUIRE_HTTPS=False,
    POS_API_DEFAULT_PAGE_SIZE=100,
    POS_API_MAX_PAGE_SIZE=500,
    POS_API_RATE_LIMIT_PER_MINUTE=0,
    POS_API_MAX_WINDOW=timedelta(days=31),
)
class PosApiV1Tests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("api_pos_pedidos_v1")
        self.base_time = timezone.now().replace(microsecond=123456)
        self.sucursal = SucursalCliente.objects.create(
            id=101,
            nombre="Sucursal API Permitida",
            tipo=SucursalCliente.Tipo.SUCURSAL,
        )
        self.otra_sucursal = SucursalCliente.objects.create(
            id=102,
            nombre="Otra Sucursal API",
            tipo=SucursalCliente.Tipo.SUCURSAL,
        )
        self.mayorista = SucursalCliente.objects.create(
            id=103,
            nombre="Cliente Mayorista API",
            tipo=SucursalCliente.Tipo.CLIENTE_MAYORISTA,
        )
        self.no_permitida = SucursalCliente.objects.create(
            id=999,
            nombre="Sucursal Fuera de Allowlist",
            tipo=SucursalCliente.Tipo.SUCURSAL,
        )
        self.producto = Producto.objects.create(
            id=201,
            nombre="Producto API",
            nombre_ticket="PROD API",
            unidad_medida="KILOGRAMO (KG)",
            unidad_abreviatura="KG",
            cantidad_por_precio=Decimal("30.000"),
        )

    def _params(self, **overrides):
        params = {
            "desde": (self.base_time - timedelta(hours=1)).isoformat(),
            "hasta": (self.base_time + timedelta(hours=1)).isoformat(),
        }
        params.update(overrides)
        return params

    def _get(self, params=None, token=TOKEN, secure=False, request_id="test-request"):
        headers = {"HTTP_X_REQUEST_ID": request_id}
        if token is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.get(
            self.url,
            data=params or self._params(),
            secure=secure,
            **headers,
        )

    def _crear_pedido(
        self,
        *,
        sucursal=None,
        estado=Pedido.Estado.CONFIRMADO,
        eliminado=False,
        fecha=None,
        total=Decimal("13.00"),
        subtotal_guardado=None,
    ):
        fecha = self.base_time if fecha is None else fecha
        pedido = Pedido.objects.create(
            sucursal_cliente=sucursal or self.sucursal,
            usuario_nombre="usuario-api",
            fecha_confirmacion=(
                fecha if estado != Pedido.Estado.PENDIENTE else None
            ),
            estado=estado,
            eliminado=eliminado,
            total=total,
        )
        item = ItemPedido.objects.create(
            pedido=pedido,
            producto=self.producto,
            cantidad=Decimal("30.000"),
            precio_unitario=Decimal("13.00"),
        )
        if subtotal_guardado is not None:
            ItemPedido.objects.filter(pk=item.pk).update(subtotal=subtotal_guardado)
        return pedido

    def test_autenticacion_ausente(self):
        response = self._get(token=None)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")
        self.assertEqual(response["WWW-Authenticate"], "Bearer")

    def test_autenticacion_incorrecta_no_filtra_el_token(self):
        invalid_token = "token-incorrecto-no-debe-aparecer-en-la-respuesta"

        response = self._get(token=invalid_token)

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(invalid_token, response.content.decode("utf-8"))
        self.assertEqual(
            set(response.json()["error"]), {"code", "message", "request_id"}
        )

    def test_autenticacion_correcta(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], "v1")
        self.assertEqual(response["Cache-Control"], "no-store")

    @override_settings(POS_API_TOKENS=())
    def test_sin_credenciales_configuradas_falla_cerrado(self):
        response = self._get()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "service_unavailable")

    @override_settings(POS_API_ALLOWED_SUCURSAL_IDS=())
    def test_sin_allowlist_configurada_falla_cerrado(self):
        response = self._get()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "service_unavailable")

    @override_settings(
        POS_API_TOKENS=(
            "token-pos-anterior-abcdefghijklmnopqrstuvwxyz-0123456789",
            TOKEN,
        )
    )
    def test_rotacion_acepta_token_actual_y_anterior(self):
        previous = self._get(
            token="token-pos-anterior-abcdefghijklmnopqrstuvwxyz-0123456789"
        )
        current = self._get(token=TOKEN)

        self.assertEqual(previous.status_code, 200)
        self.assertEqual(current.status_code, 200)

    @override_settings(POS_API_REQUIRE_HTTPS=True, SECURE_SSL_REDIRECT=False)
    def test_https_es_obligatorio_cuando_se_configura(self):
        insecure = self._get(secure=False)
        secure = self._get(secure=True)

        self.assertEqual(insecure.status_code, 426)
        self.assertEqual(insecure.json()["error"]["code"], "https_required")
        self.assertEqual(secure.status_code, 200)

    def test_metodos_de_escritura_se_rechazan_sin_modificar_datos(self):
        self._crear_pedido()
        pedidos_antes = Pedido.objects.count()
        items_antes = ItemPedido.objects.count()

        response = self.client.post(
            self.url,
            data={"estado": "enviado"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
        self.assertEqual(Pedido.objects.count(), pedidos_antes)
        self.assertEqual(ItemPedido.objects.count(), items_antes)

    def test_solo_incluye_confirmados_no_eliminados(self):
        incluido = self._crear_pedido()
        self._crear_pedido(estado=Pedido.Estado.PENDIENTE)
        self._crear_pedido(estado=Pedido.Estado.ENVIADO)
        self._crear_pedido(eliminado=True)

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [pedido["id"] for pedido in response.json()["data"]], [incluido.id]
        )

    def test_ventana_es_inclusiva_desde_y_exclusiva_hasta(self):
        desde = self.base_time
        hasta = self.base_time + timedelta(hours=1)
        self._crear_pedido(fecha=desde - timedelta(microseconds=1))
        en_desde = self._crear_pedido(fecha=desde)
        dentro = self._crear_pedido(fecha=hasta - timedelta(microseconds=1))
        self._crear_pedido(fecha=hasta)

        response = self._get(
            {"desde": desde.isoformat(), "hasta": hasta.isoformat()}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [pedido["id"] for pedido in response.json()["data"]],
            [en_desde.id, dentro.id],
        )

    def test_restringe_allowlist_tipo_y_filtro_de_sucursal(self):
        permitido = self._crear_pedido(sucursal=self.sucursal)
        otra = self._crear_pedido(sucursal=self.otra_sucursal)
        self._crear_pedido(sucursal=self.mayorista)
        self._crear_pedido(sucursal=self.no_permitida)

        all_response = self._get()
        filtered = self._get(self._params(sucursal_id=str(self.sucursal.id)))
        forbidden = self._get(self._params(sucursal_id=str(self.no_permitida.id)))

        self.assertEqual(
            [pedido["id"] for pedido in all_response.json()["data"]],
            [permitido.id, otra.id],
        )
        self.assertEqual(
            [pedido["id"] for pedido in filtered.json()["data"]],
            [permitido.id],
        )
        self.assertEqual(forbidden.status_code, 400)

    def test_paginacion_cursor_estable_y_repeticion_de_pagina(self):
        pedidos = [self._crear_pedido() for _ in range(3)]
        params = self._params(limite="2")

        first = self._get(params, request_id="pagina-repetible")
        repeated = self._get(params, request_id="pagina-repetible")
        first_payload = first.json()
        second = self._get(
            self._params(limite="2", cursor=first_payload["page"]["next_cursor"])
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first_payload, repeated.json())
        self.assertEqual(
            [record["id"] for record in first_payload["data"]],
            [pedidos[0].id, pedidos[1].id],
        )
        self.assertTrue(first_payload["page"]["has_more"])
        self.assertEqual(
            [record["id"] for record in second.json()["data"]],
            [pedidos[2].id],
        )
        self.assertFalse(second.json()["page"]["has_more"])

    def test_cursor_rechaza_cambio_de_filtros_y_manipulacion(self):
        for _ in range(2):
            self._crear_pedido()
        first = self._get(self._params(limite="1")).json()
        cursor = first["page"]["next_cursor"]

        changed_filter = self._get(
            self._params(limite="1", cursor=cursor, sucursal_id="102")
        )
        tampered = self._get(self._params(limite="1", cursor=f"{cursor}x"))

        self.assertEqual(changed_filter.status_code, 400)
        self.assertEqual(tampered.status_code, 400)
        self.assertEqual(tampered.json()["error"]["code"], "invalid_parameter")

    def test_payload_preserva_ids_decimales_campos_y_subtotal_guardado(self):
        pedido = self._crear_pedido(
            total=Decimal("99.99"), subtotal_guardado=Decimal("99.99")
        )

        response = self._get()
        record = response.json()["data"][0]
        item = record["items"][0]

        self.assertEqual(record["id"], pedido.id)
        self.assertIsInstance(record["id"], int)
        self.assertEqual(record["total"], "99.99")
        self.assertTrue(record["fecha_confirmacion"].endswith("Z"))
        self.assertEqual(record["sucursal"]["id"], self.sucursal.id)
        self.assertEqual(item["pedido_id"], pedido.id)
        self.assertEqual(item["producto"]["id"], self.producto.id)
        self.assertEqual(item["producto"]["nombre"], "Producto API")
        self.assertEqual(item["cantidad"], "30.000")
        self.assertEqual(item["precio_unitario"], "13.00")
        self.assertEqual(item["subtotal"], "99.99")

    def test_respuesta_vacia_tiene_cursor_nulo(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], [])
        self.assertEqual(response.json()["page"]["returned"], 0)
        self.assertIsNone(response.json()["page"]["next_cursor"])

    @override_settings(POS_API_MAX_PAGE_SIZE=2, POS_API_DEFAULT_PAGE_SIZE=1)
    def test_limite_maximo_es_estricto(self):
        response = self._get(self._params(limite="3"))

        self.assertEqual(response.status_code, 400)
        self.assertIn("entre 1 y 2", response.json()["error"]["message"])

    @override_settings(POS_API_RATE_LIMIT_PER_MINUTE=1)
    def test_rate_limit_devuelve_error_json_estable(self):
        cache.clear()

        first = self._get(request_id="rate-1")
        second = self._get(request_id="rate-2")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "rate_limited")
        self.assertIn("Retry-After", second)

    def test_parametros_desconocidos_y_ventana_excesiva_fallan(self):
        unknown = self._get(self._params(secreto="valor"))
        excessive = self._get(
            {
                "desde": self.base_time.isoformat(),
                "hasta": (self.base_time + timedelta(days=32)).isoformat(),
            }
        )

        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(excessive.status_code, 400)
        self.assertNotIn("valor", unknown.content.decode("utf-8"))
