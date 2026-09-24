from datetime import timedelta
from decimal import Decimal
import hashlib
import uuid

from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .api_pos import CURSOR_SALT_V2
from .models import ItemPedido, Pedido, PedidoPurgado, PosApiCredential, Producto, RegistroPurga, SucursalCliente


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

    def test_token_no_ascii_no_provoca_error_500(self):
        response = self._get(token="credencial-invalida-ñ")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

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


@override_settings(
    POS_API_TOKENS=(TOKEN,),
    POS_API_ALLOWED_SUCURSAL_IDS=(101,),
    POS_API_REQUIRE_HTTPS=False,
    POS_API_DEFAULT_PAGE_SIZE=100,
    POS_API_MAX_PAGE_SIZE=500,
    POS_API_RATE_LIMIT_PER_MINUTE=0,
    POS_API_MAX_WINDOW=timedelta(days=31),
)
class PosApiV2Tests(TestCase):
    def setUp(self):
        self.url = reverse("api_pos_pedidos_v2")
        self.base_time = timezone.now().replace(microsecond=123456)
        self.sucursal = SucursalCliente.objects.create(
            id=101, nombre="Sucursal POS v2", tipo=SucursalCliente.Tipo.SUCURSAL
        )
        self.edge_id = uuid.uuid4()
        self.credential = PosApiCredential.objects.create(
            edge_id=self.edge_id,
            sucursal_cliente=self.sucursal,
            token_sha256=hashlib.sha256(TOKEN.encode("utf-8")).hexdigest(),
            scopes=["orders:v2:read"],
        )
        self.producto = Producto.objects.create(
            nombre="Producto POS v2", cantidad_por_precio=Decimal("1.000")
        )

    def _params(self, **overrides):
        params = {
            "desde": (self.base_time - timedelta(hours=1)).isoformat(),
            "hasta": (self.base_time + timedelta(hours=1)).isoformat(),
            "sucursal_id": "101",
        }
        params.update(overrides)
        return params

    def _get(self, **overrides):
        return self.client.get(
            self.url,
            data=self._params(**overrides),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )

    def _pedido(self, *, fecha=None, sucursal=None):
        pedido = Pedido.objects.create(
            sucursal_cliente=sucursal or self.sucursal,
            usuario_nombre="usuario-sintetico",
            estado=Pedido.Estado.CONFIRMADO,
            fecha_confirmacion=fecha or self.base_time,
            total=Decimal("8.00"),
        )
        ItemPedido.objects.create(
            pedido=pedido,
            producto=self.producto,
            cantidad=Decimal("1.000"),
            precio_unitario=Decimal("8.00"),
        )
        return pedido

    def _purga(self, minimum, maximum, *, sucursal_id=101):
        registro = RegistroPurga.objects.create(
            motivo="antiguedad",
            numero_pedidos=1 if minimum == maximum else 2,
            numero_items=1,
            numero_macropedidos=1,
            fecha_confirmacion_min=minimum,
            fecha_confirmacion_max=maximum,
        )
        for fecha in {minimum, maximum}:
            PedidoPurgado.objects.create(
                codigo_publico=uuid.uuid4(),
                pedido_id_origen=100000 + PedidoPurgado.objects.count(),
                sucursal_cliente_id=sucursal_id,
                fecha_confirmacion=fecha,
                motivo="antiguedad",
                registro=registro,
            )
        return registro

    def test_v2_expone_uuid_publico_y_preserva_v1(self):
        pedido = self._pedido()

        v2 = self._get()
        v1 = self.client.get(
            reverse("api_pos_pedidos_v1"),
            data=self._params(),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )

        self.assertEqual(v2.status_code, 200)
        self.assertEqual(v2.json()["version"], "v2")
        self.assertEqual(v2.json()["data"][0]["codigo_publico"], str(pedido.codigo_publico))
        self.assertEqual(v2.json()["data"][0]["id"], pedido.id)
        self.assertEqual(v1.status_code, 200)
        self.assertNotIn("codigo_publico", v1.json()["data"][0])

    def test_cursor_v2_usa_id_para_desempatar_fechas_iguales(self):
        pedidos = [self._pedido() for _ in range(3)]

        first = self._get(limite="1")
        second = self._get(limite="1", cursor=first.json()["page"]["next_cursor"])
        third = self._get(limite="1", cursor=second.json()["page"]["next_cursor"])

        self.assertEqual([first.status_code, second.status_code, third.status_code], [200] * 3)
        self.assertEqual(
            [response.json()["data"][0]["codigo_publico"] for response in (first, second, third)],
            [str(pedido.codigo_publico) for pedido in pedidos],
        )
        self.assertIsNone(third.json()["page"]["next_cursor"])

    def test_cursores_v1_y_v2_no_son_intercambiables(self):
        self._pedido()
        self._pedido()
        v1_url = reverse("api_pos_pedidos_v1")
        v1_page = self.client.get(
            v1_url,
            data=self._params(limite="1"),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        ).json()
        v2_page = self._get(limite="1").json()

        v1_cursor_in_v2 = self._get(
            limite="1", cursor=v1_page["page"]["next_cursor"]
        )
        v2_cursor_in_v1 = self.client.get(
            v1_url,
            data=self._params(limite="1", cursor=v2_page["page"]["next_cursor"]),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )

        self.assertEqual(v1_cursor_in_v2.status_code, 400)
        self.assertEqual(v2_cursor_in_v1.status_code, 400)

    def test_rango_purgado_devuelve_410_aun_si_no_quedan_filas(self):
        self._purga(self.base_time, self.base_time)

        response = self._get()

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "retention_gap")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_410_no_revela_ids_ni_fechas_y_requiere_autenticacion(self):
        self._purga(self.base_time, self.base_time)

        sin_token = self.client.get(self.url, data=self._params())
        autorizado = self._get()

        self.assertEqual(sin_token.status_code, 401)
        self.assertEqual(sin_token.json()["error"]["code"], "unauthorized")
        self.assertEqual(autorizado.status_code, 410)
        self.assertEqual(
            set(autorizado.json()["error"]),
            {"code", "message", "request_id"},
        )
        self.assertNotIn(str(self.sucursal.pk), autorizado.content.decode())
        self.assertNotIn(self.base_time.isoformat(), autorizado.content.decode())

    def test_purga_de_otra_sucursal_no_revela_brecha_ni_invalida_cursor(self):
        self._pedido()
        segundo = self._pedido()
        cursor = self._get(limite="1").json()["page"]["next_cursor"]
        self._purga(self.base_time, self.base_time, sucursal_id=102)

        primera = self._get(limite="1")
        siguiente = self._get(limite="1", cursor=cursor)

        self.assertEqual(primera.status_code, 200)
        self.assertEqual(siguiente.status_code, 200)
        self.assertEqual(siguiente.json()["data"][0]["id"], segundo.pk)

    def test_credencial_de_mayorista_no_revela_brecha(self):
        mayorista = SucursalCliente.objects.create(
            id=103, nombre="Mayorista no retornable", tipo=SucursalCliente.Tipo.CLIENTE_MAYORISTA
        )
        pedido = self._pedido()
        self._purga(self.base_time, self.base_time, sucursal_id=103)
        token_mayorista = "token-mayorista-abcdefghijklmnopqrstuvwxyz-0123456789"
        PosApiCredential.objects.create(
            edge_id=uuid.uuid4(), sucursal_cliente=mayorista,
            token_sha256=hashlib.sha256(token_mayorista.encode("utf-8")).hexdigest(),
            scopes=["orders:v2:read"],
        )

        general = self._get()
        respuesta_mayorista = self.client.get(
            self.url, data=self._params(sucursal_id="103"),
            HTTP_AUTHORIZATION=f"Bearer {token_mayorista}",
        )

        self.assertEqual(general.status_code, 200)
        self.assertEqual(general.json()["data"][0]["id"], pedido.pk)
        self.assertEqual(respuesta_mayorista.status_code, 403)

    def test_hueco_entre_fechas_purgadas_no_genera_410(self):
        self._purga(
            self.base_time - timedelta(minutes=30),
            self.base_time + timedelta(minutes=30),
        )
        pedido = self._pedido()

        response = self._get(
            desde=(self.base_time - timedelta(minutes=1)).isoformat(),
            hasta=(self.base_time + timedelta(minutes=1)).isoformat(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], pedido.pk)

    def test_cursor_anterior_a_purga_fuera_de_ventana_es_obsoleto(self):
        self._pedido()
        self._pedido()
        old_cursor = self._get(limite="1").json()["page"]["next_cursor"]
        self._purga(
            self.base_time - timedelta(days=2),
            self.base_time - timedelta(days=2),
        )

        stale = self._get(limite="1", cursor=old_cursor)
        fresh = self._get(limite="1")

        self.assertEqual(stale.status_code, 410)
        self.assertEqual(stale.json()["error"]["code"], "retention_gap")
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(len(fresh.json()["data"]), 1)

    def test_cursor_no_expone_id_global_de_purga(self):
        self._pedido()
        self._pedido()
        registro = self._purga(
            self.base_time - timedelta(days=2),
            self.base_time - timedelta(days=2),
        )

        cursor = self._get(limite="1").json()["page"]["next_cursor"]
        carga = signing.loads(cursor, salt=CURSOR_SALT_V2)

        self.assertRegex(carga["purge_epoch"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(carga["purge_epoch"], str(registro.pk))

    def test_cursor_v2_previo_con_epoch_numerico_solicita_conciliacion(self):
        self._pedido()
        self._pedido()
        cursor = self._get(limite="1").json()["page"]["next_cursor"]
        carga = signing.loads(cursor, salt=CURSOR_SALT_V2)
        carga["purge_epoch"] = 0
        cursor_anterior = signing.dumps(carga, salt=CURSOR_SALT_V2, compress=True)

        response = self._get(limite="1", cursor=cursor_anterior)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "retention_gap")

    def test_cursor_legado_numerico_del_contrato_pos_solicita_conciliacion(self):
        response = self._get(cursor="1727034600123456")

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "retention_gap")

    def test_datos_posteriores_a_purga_y_limite_exclusivo(self):
        self._purga(
            self.base_time - timedelta(hours=2),
            self.base_time - timedelta(hours=1, microseconds=1),
        )
        self._purga(
            self.base_time + timedelta(hours=1),
            self.base_time + timedelta(hours=1),
        )
        retained = self._pedido()

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [record["codigo_publico"] for record in response.json()["data"]],
            [str(retained.codigo_publico)],
        )

    def test_v2_reutiliza_autenticacion_y_allowlist(self):
        unauthorized = self.client.get(self.url, data=self._params())
        forbidden = self._get(sucursal_id="999")

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(forbidden.status_code, 403)

    def test_scope_por_credencial_y_revocacion(self):
        self.credential.scopes = []
        self.credential.save(update_fields=["scopes"])
        self.assertEqual(self._get().status_code, 403)

        self.credential.scopes = ["orders:v2:read"]
        self.credential.save(update_fields=["scopes"])
        self.sucursal.activa = False
        self.sucursal.save(update_fields=["activa"])
        self.assertEqual(self._get().status_code, 403)
        self.sucursal.activa = True
        self.sucursal.save(update_fields=["activa"])
        self.credential.revoked_at = timezone.now()
        self.credential.active = False
        self.credential.save(update_fields=["scopes", "revoked_at", "active"])
        self.assertEqual(self._get().status_code, 401)

    def test_credencial_expirada_y_rotacion_independiente_de_v1(self):
        self.credential.expires_at = timezone.now() - timedelta(seconds=1)
        self.credential.save(update_fields=["expires_at"])
        self.assertEqual(self._get().status_code, 401)
        token_nuevo = "token-rotado-abcdefghijklmnopqrstuvwxyz-0123456789"
        PosApiCredential.objects.create(
            edge_id=self.edge_id, sucursal_cliente=self.sucursal,
            token_sha256=hashlib.sha256(token_nuevo.encode("utf-8")).hexdigest(),
            scopes=["orders:v2:read"], rotated_from=self.credential,
        )
        nuevo = self.client.get(
            self.url, data=self._params(), HTTP_AUTHORIZATION=f"Bearer {token_nuevo}",
        )
        anterior_v1 = self.client.get(
            reverse("api_pos_pedidos_v1"), data=self._params(),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )
        self.assertEqual(nuevo.status_code, 200)
        self.assertEqual(anterior_v1.status_code, 200)

    def test_otra_credencial_no_puede_leer_sucursal_ajena(self):
        otra = SucursalCliente.objects.create(
            id=102, nombre="Otra sucursal de otro Edge", tipo=SucursalCliente.Tipo.SUCURSAL
        )
        token_otro = "token-otro-edge-abcdefghijklmnopqrstuvwxyz-0123456789"
        PosApiCredential.objects.create(
            edge_id=uuid.uuid4(), sucursal_cliente=otra,
            token_sha256=hashlib.sha256(token_otro.encode("utf-8")).hexdigest(),
            scopes=["orders:v2:read"],
        )
        ajena = self.client.get(
            self.url, data=self._params(sucursal_id="101"),
            HTTP_AUTHORIZATION=f"Bearer {token_otro}",
        )
        propia = self.client.get(
            self.url, data=self._params(sucursal_id="102"),
            HTTP_AUTHORIZATION=f"Bearer {token_otro}",
        )
        self.assertEqual(ajena.status_code, 403)
        self.assertEqual(propia.status_code, 200)

    def test_id_y_uuid_de_otra_sucursal_no_amplian_el_alcance(self):
        permitido = self._pedido()
        otra_sucursal = SucursalCliente.objects.create(
            id=102, nombre="Sucursal fuera del POS", tipo=SucursalCliente.Tipo.SUCURSAL
        )
        ajeno = self._pedido(sucursal=otra_sucursal)

        general = self._get()
        por_id = self._get(sucursal_id="102")
        por_uuid = self._get(codigo_publico=str(ajeno.codigo_publico))

        self.assertEqual(general.status_code, 200)
        self.assertEqual([fila["codigo_publico"] for fila in general.json()["data"]],
                         [str(permitido.codigo_publico)])
        self.assertEqual(por_id.status_code, 403)
        self.assertEqual(por_uuid.status_code, 400)
        self.assertNotIn(str(ajeno.codigo_publico), por_id.content.decode())
        self.assertNotIn(str(ajeno.codigo_publico), por_uuid.content.decode())

    def test_get_y_post_no_borran_ni_confirman(self):
        pedido = self._pedido()
        respuesta = self._get()
        escritura = self.client.post(
            self.url, data=self._params(),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )

        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(escritura.status_code, 405)
        self.assertTrue(Pedido.objects.filter(pk=pedido.pk).exists())
        self.assertEqual(RegistroPurga.objects.count(), 0)
        self.assertEqual(PedidoPurgado.objects.count(), 0)
