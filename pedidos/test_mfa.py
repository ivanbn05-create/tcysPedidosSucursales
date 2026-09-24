import time
import os
import stat
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from unittest import skipUnless

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from django.core.management import call_command
from django_otp.oath import TOTP
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice
from axes.models import AccessAttempt

from .mfa import RECENT_KEY, generar_codigos_recuperacion
from .models import MFAEstado, MFACodigoRecuperacion, SesionActiva


@override_settings(MFA_ENFORCE=True, SECURE_SSL_REDIRECT=False)
class MFASeguridadTests(TestCase):
    def setUp(self):
        self.usuario = User.objects.create_user(
            username="staff_mfa", password="ClaveFuerte-Ensayo-123", is_staff=True
        )
        self.dispositivo = TOTPDevice.objects.create(
            user=self.usuario, name="principal", confirmed=True
        )

    def codigo(self, dispositivo=None, instante=None):
        dispositivo = dispositivo or self.dispositivo
        instante = instante or time.time()
        totp = TOTP(
            dispositivo.bin_key, dispositivo.step, dispositivo.t0,
            dispositivo.digits, dispositivo.drift,
        )
        totp.time = instante
        return str(totp.token()).zfill(dispositivo.digits)

    def primer_factor(self, client=None):
        client = client or self.client
        return client.post(
            "/login/",
            {"username": self.usuario.username, "password": "ClaveFuerte-Ensayo-123"},
        )

    def login_completo(self, client=None, instante=None):
        client = client or self.client
        self.assertRedirects(self.primer_factor(client), "/mfa/login/")
        instante = instante or time.time()
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            respuesta = client.post("/mfa/login/", {"codigo": self.codigo(instante=instante)})
        self.assertEqual(respuesta.status_code, 302)
        return client

    def test_contrasena_no_crea_sesion_privilegiada_y_sin_otp_no_hay_admin(self):
        self.assertRedirects(self.primer_factor(), "/mfa/login/")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(self.client.post("/login/", {"force_takeover": "1"}).status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_otp_valido_autentica_y_reutilizacion_falla(self):
        instante = time.time()
        self.login_completo(instante=instante)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        antes = self.client.session[RECENT_KEY]
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            respuesta = self.client.post(
                "/mfa/revalidar/",
                {"password": "ClaveFuerte-Ensayo-123", "codigo": self.codigo(instante=instante)},
            )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(self.client.session[RECENT_KEY], antes)

    def test_fallos_otp_bloquean_incluso_codigo_correcto(self):
        self.primer_factor()
        for _ in range(5):
            self.assertEqual(self.client.post("/mfa/login/", {"codigo": "000000"}).status_code, 200)
        estado = MFAEstado.objects.get(usuario=self.usuario)
        self.assertGreater(estado.bloqueado_hasta, timezone.now())
        instante = time.time()
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            respuesta = self.client.post("/mfa/login/", {"codigo": self.codigo(instante=instante)})
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_staff_sin_dispositivo_falla_cerrado(self):
        self.dispositivo.delete()
        self.assertRedirects(self.primer_factor(), "/mfa/login/")
        respuesta = self.client.post("/mfa/login/", {"codigo": "123456"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_desafio_expirado_no_acepta_otp_ni_se_repite(self):
        self.primer_factor()
        sesion = self.client.session
        pendiente = sesion["pedidos_mfa_pending"]
        pendiente["creado"] = timezone.now().timestamp() - 301
        sesion["pedidos_mfa_pending"] = pendiente
        sesion.save()
        instante = time.time()
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            self.assertRedirects(
                self.client.post("/mfa/login/", {"codigo": self.codigo(instante=instante)}),
                "/login/",
            )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn("pedidos_mfa_pending", self.client.session)

        self.login_completo(instante=time.time() + 31)
        repeticion = self.client.post("/mfa/login/", {"codigo": "000000"})
        self.assertEqual(repeticion.status_code, 302)
        self.assertEqual(repeticion.url, "/")

    def test_client_login_directo_no_elude_mfa(self):
        self.client.force_login(self.usuario, backend="django.contrib.auth.backends.ModelBackend")
        self.assertRedirects(self.client.get("/admin/"), "/login/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admin_django_exige_otp_y_estampa_solo_login_real(self):
        password_sola = self.client.post(
            "/django-admin/login/",
            {"username": "staff_mfa", "password": "ClaveFuerte-Ensayo-123"},
        )
        self.assertEqual(password_sola.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

        instante = time.time()
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            completa = self.client.post(
                "/django-admin/login/",
                {"username": "staff_mfa", "password": "ClaveFuerte-Ensayo-123",
                 "otp_device": self.dispositivo.persistent_id,
                 "otp_token": self.codigo(instante=instante)},
            )
        self.assertEqual(completa.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertIn(DEVICE_ID_SESSION_KEY, self.client.session)
        self.assertIn("pedidos_mfa_credenciales", self.client.session)
        self.assertEqual(self.client.get("/django-admin/").status_code, 200)

        falsificada = Client()
        falsificada.force_login(self.usuario, backend="django.contrib.auth.backends.ModelBackend")
        sesion = falsificada.session
        sesion[DEVICE_ID_SESSION_KEY] = self.dispositivo.persistent_id
        sesion.save()
        falsificada.post(
            "/django-admin/login/",
            {"username": "staff_mfa", "password": "ClaveFuerte-Ensayo-123"},
        )
        self.assertNotIn("pedidos_mfa_credenciales", falsificada.session)
        self.assertRedirects(
            falsificada.get("/django-admin/"),
            "/django-admin/login/?next=%2Fdjango-admin%2F",
        )

    def test_cambio_rol_o_password_invalida_sesion(self):
        self.login_completo()
        self.usuario.is_superuser = True
        self.usuario.save(update_fields=["is_superuser"])
        self.assertRedirects(self.client.get("/admin/"), "/login/")
        self.login_completo(instante=time.time() + 31)
        self.usuario.set_password("ClaveNueva-Ensayo-456")
        self.usuario.save(update_fields=["password"])
        self.assertRedirects(self.client.get("/admin/"), "/login/?next=%2Fadmin%2F")

    def test_retiro_de_staff_invalida_sesion_privilegiada(self):
        self.login_completo()
        self.usuario.is_staff = False
        self.usuario.save(update_fields=["is_staff"])
        self.assertRedirects(self.client.get("/pedidos/"), "/login/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_lease_inactivo_invalida_cookie_y_token_anteriores(self):
        self.login_completo()
        SesionActiva.objects.filter(usuario=self.usuario).update(
            ultima_actividad=timezone.now() - timedelta(minutes=11)
        )
        self.assertRedirects(self.client.get("/admin/"), "/login/")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_otp_reciente_para_post_admin(self):
        self.login_completo()
        sesion = self.client.session
        sesion[RECENT_KEY] = timezone.now().timestamp() - 1000
        sesion.save()
        respuesta = self.client.post("/admin/configuracion/", {"action": "desconocida"})
        self.assertRedirects(respuesta, "/mfa/revalidar/?next=/admin/")
        self.assertRedirects(
            self.client.patch("/django-admin/", data="{}", content_type="application/json"),
            "/mfa/revalidar/?next=/admin/",
        )

    def test_revalidacion_no_acepta_redireccion_externa(self):
        self.login_completo()
        formulario = self.client.get("/mfa/revalidar/?next=//externo.invalid/")
        self.assertEqual(formulario.status_code, 200)
        self.assertNotContains(formulario, "externo.invalid")
        instante = time.time() + 31
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            respuesta = self.client.post(
                "/mfa/revalidar/",
                {"password": "ClaveFuerte-Ensayo-123", "codigo": self.codigo(instante=instante),
                 "next": "https://externo.invalid/"},
            )
        self.assertRedirects(respuesta, "/admin/")

    def test_toma_de_sesion_exige_otp_y_expulsa_sesion_anterior(self):
        instante = time.time()
        anterior = Client()
        nuevo = Client()
        self.login_completo(anterior, instante)
        self.assertRedirects(self.primer_factor(nuevo), "/mfa/login/")
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante + 31):
            conflicto = nuevo.post(
                "/mfa/login/", {"codigo": self.codigo(instante=instante + 31)}
            )
        self.assertContains(conflicto, "Sesión iniciada en otro dispositivo")
        self.assertNotIn("_auth_user_id", nuevo.session)
        respuesta = nuevo.post("/login/", {"force_takeover": "1"})
        self.assertRedirects(respuesta, "/admin/")
        self.assertEqual(nuevo.get("/admin/").status_code, 200)
        self.assertRedirects(anterior.get("/admin/"), "/login/")

    def test_recuperacion_es_un_uso_y_no_abre_admin_antes_de_nuevo_totp(self):
        codigo_recuperacion = generar_codigos_recuperacion(self.usuario, cantidad=1)[0]
        self.primer_factor()
        self.assertRedirects(
            self.client.post("/mfa/login/", {"codigo": codigo_recuperacion}),
            "/mfa/recuperacion/",
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertRedirects(self.client.get("/admin/"), "/login/?next=%2Fadmin%2F")
        self.assertEqual(
            MFACodigoRecuperacion.objects.filter(usuario=self.usuario, usado_en__isnull=False).count(), 1
        )
        self.assertEqual(
            self.client.post("/mfa/recuperacion/", {"action": "iniciar"}).status_code, 200
        )
        nuevo = TOTPDevice.objects.filter(user=self.usuario, confirmed=False).get()
        instante = time.time()
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            respuesta = self.client.post(
                "/mfa/recuperacion/",
                {"action": "confirmar", "codigo": self.codigo(nuevo, instante)},
            )
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "MFA restablecido")
        self.assertEqual(TOTPDevice.objects.filter(user=self.usuario, confirmed=True).count(), 1)
        self.assertEqual(MFACodigoRecuperacion.objects.filter(usuario=self.usuario, usado_en__isnull=True).count(), 8)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_codigo_recuperacion_consumido_no_puede_repetirse(self):
        codigo = generar_codigos_recuperacion(self.usuario, cantidad=1)[0]
        primer_cliente = Client()
        segundo_cliente = Client()
        self.primer_factor(primer_cliente)
        self.assertRedirects(
            primer_cliente.post("/mfa/login/", {"codigo": codigo}),
            "/mfa/recuperacion/",
        )
        self.primer_factor(segundo_cliente)
        respuesta = segundo_cliente.post("/mfa/login/", {"codigo": codigo})
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotIn("_auth_user_id", segundo_cliente.session)

    def test_reset_recuperacion_invalida_sesion_anterior_y_get_no_crea_dispositivo(self):
        anterior = Client()
        recuperador = Client()
        self.login_completo(anterior)
        codigo = generar_codigos_recuperacion(self.usuario, cantidad=1)[0]
        self.primer_factor(recuperador)
        self.assertRedirects(
            recuperador.post("/mfa/login/", {"codigo": codigo}),
            "/mfa/recuperacion/",
        )
        cuenta_antes = TOTPDevice.objects.filter(user=self.usuario).count()
        self.assertEqual(recuperador.get("/mfa/recuperacion/").status_code, 200)
        self.assertEqual(TOTPDevice.objects.filter(user=self.usuario).count(), cuenta_antes)
        recuperador.post("/mfa/recuperacion/", {"action": "iniciar"})
        nuevo = TOTPDevice.objects.filter(user=self.usuario, confirmed=False).get()
        instante = time.time()
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            self.assertEqual(
                recuperador.post(
                    "/mfa/recuperacion/",
                    {"action": "confirmar", "codigo": self.codigo(nuevo, instante)},
                ).status_code,
                200,
            )
        self.assertEqual(recuperador.get("/admin/").status_code, 200)
        self.assertRedirects(anterior.get("/admin/"), "/login/")

    def test_password_incorrecta_y_cuenta_desconocida_comparten_error(self):
        incorrecta = self.client.post(
            "/login/", {"username": "staff_mfa", "password": "incorrecta"}
        )
        desconocida = self.client.post(
            "/login/", {"username": "usuario_que_no_existe", "password": "incorrecta"}
        )
        self.assertEqual(incorrecta.status_code, desconocida.status_code)
        self.assertContains(incorrecta, "No fue posible verificar las credenciales")
        self.assertContains(desconocida, "No fue posible verificar las credenciales")

    def test_axes_no_persiste_password_otp_ni_csrf_en_fallo_stepup(self):
        self.login_completo()
        respuesta = self.client.post(
            "/mfa/revalidar/",
            {
                "password": "CLAVE_FALSA_SENTINEL",
                "codigo": "123456",
                "csrfmiddlewaretoken": "CSRF_SENTINEL",
                "device_id": "DEVICE_SENTINEL",
            },
        )
        self.assertEqual(respuesta.status_code, 200)
        intento = AccessAttempt.objects.order_by("-pk").first()
        self.assertIsNotNone(intento)
        for valor in ("CLAVE_FALSA_SENTINEL", "123456", "CSRF_SENTINEL", "DEVICE_SENTINEL"):
            self.assertNotIn(valor, intento.post_data or "")

    def test_logout_get_no_tiene_efecto(self):
        self.login_completo()
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertRedirects(self.client.post("/logout/"), "/login/")

    def test_csrf_login_otp_y_logout(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(
            client.post("/login/", {"username": "staff_mfa", "password": "ClaveFuerte-Ensayo-123"}).status_code,
            403,
        )
        client.get("/login/")
        token = client.cookies["csrftoken"].value
        self.assertRedirects(
            client.post(
                "/login/",
                {"username": "staff_mfa", "password": "ClaveFuerte-Ensayo-123", "csrfmiddlewaretoken": token},
            ),
            "/mfa/login/",
        )
        self.assertEqual(client.post("/mfa/login/", {"codigo": "123456"}).status_code, 403)

    def test_csrf_recuperacion_revalidacion_y_logout(self):
        codigo = generar_codigos_recuperacion(self.usuario, cantidad=1)[0]
        client = Client(enforce_csrf_checks=True)
        client.get("/login/")
        csrf = client.cookies["csrftoken"].value
        self.assertRedirects(
            client.post(
                "/login/",
                {"username": "staff_mfa", "password": "ClaveFuerte-Ensayo-123",
                 "csrfmiddlewaretoken": csrf},
            ),
            "/mfa/login/",
        )
        self.assertRedirects(
            client.post("/mfa/login/", {"codigo": codigo, "csrfmiddlewaretoken": csrf}),
            "/mfa/recuperacion/",
        )
        self.assertEqual(client.post("/mfa/recuperacion/", {"action": "iniciar"}).status_code, 403)

        otro = Client(enforce_csrf_checks=True)
        otro.get("/login/")
        csrf_otro = otro.cookies["csrftoken"].value
        self.assertRedirects(
            otro.post(
                "/login/",
                {"username": "staff_mfa", "password": "ClaveFuerte-Ensayo-123",
                 "csrfmiddlewaretoken": csrf_otro},
            ),
            "/mfa/login/",
        )
        instante = time.time()
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante):
            self.assertRedirects(
                otro.post(
                    "/mfa/login/",
                    {"codigo": self.codigo(instante=instante), "csrfmiddlewaretoken": csrf_otro},
                ),
                "/admin/",
            )
        self.assertEqual(
            otro.post("/mfa/revalidar/", {"password": "ClaveFuerte-Ensayo-123", "codigo": "000000"}).status_code,
            403,
        )
        self.assertEqual(otro.post("/logout/").status_code, 403)

    def test_csrf_toma_de_sesion_no_se_puede_saltar(self):
        instante = time.time()
        anterior = Client()
        otro = Client(enforce_csrf_checks=True)
        self.login_completo(anterior, instante)
        otro.get("/login/")
        csrf = otro.cookies["csrftoken"].value
        self.assertRedirects(
            otro.post(
                "/login/",
                {"username": "staff_mfa", "password": "ClaveFuerte-Ensayo-123",
                 "csrfmiddlewaretoken": csrf},
            ),
            "/mfa/login/",
        )
        with patch("django_otp.plugins.otp_totp.models.time.time", return_value=instante + 31):
            conflicto = otro.post(
                "/mfa/login/",
                {"codigo": self.codigo(instante=instante + 31), "csrfmiddlewaretoken": csrf},
            )
        self.assertContains(conflicto, "Sesión iniciada en otro dispositivo")
        self.assertEqual(otro.post("/login/", {"force_takeover": "1"}).status_code, 403)
        self.assertNotIn("_auth_user_id", otro.session)

    @skipUnless(os.name == "posix", "Permisos de archivo probados sólo en Linux/POSIX")
    def test_provisionamiento_offline_privado_no_imprime_secretos(self):
        self.dispositivo.delete()
        with tempfile.TemporaryDirectory(prefix="tcys-mfa-prueba-") as temporal:
            os.chmod(temporal, 0o700)
            destino = os.path.join(temporal, "entrega-mfa.txt")
            salida = StringIO()
            call_command(
                "provisionar_mfa", usuario="staff_mfa", output=destino,
                confirm=True, stdout=salida,
            )
            self.assertEqual(stat.S_IMODE(os.stat(destino).st_mode), 0o600)
            with open(destino, encoding="utf-8") as archivo:
                contenido = archivo.read()
            self.assertIn("otpauth://totp/", contenido)
            self.assertIn("RC-", contenido)
            self.assertNotIn("otpauth://", salida.getvalue())
            self.assertNotIn("RC-", salida.getvalue())
