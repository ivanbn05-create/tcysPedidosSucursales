"""Regresiones para secretos en Admin y salida del comando legacy."""

from io import StringIO
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django_otp.plugins.otp_totp.models import TOTPDevice

from .models import EventoCliente, LogRecordatorio, SesionActiva, SucursalCliente


class AdminTecnicoSeguroTests(TestCase):
    def test_totp_no_puede_crearse_ni_cambiarse_por_admin_generico(self):
        self.assertNotIn(TOTPDevice, admin.site._registry)

    def test_sesion_no_muestra_token_ni_dispositivo_y_no_se_borra_en_admin(self):
        modelo_admin = admin.site._registry[SesionActiva]
        solicitud = RequestFactory().get("/django-admin/pedidos/sesionactiva/")
        solicitud.user = User.objects.create_superuser(
            "supervisor", "supervisor@example.invalid", "ClaveLarga-2026"
        )

        self.assertTrue(modelo_admin.has_view_permission(solicitud))
        self.assertFalse(modelo_admin.has_delete_permission(solicitud))
        campos = set(modelo_admin.get_fields(solicitud)) | set(modelo_admin.list_display)
        self.assertFalse(campos & {"token", "dispositivo_id", "dispositivo", "direccion_ip"})

    def test_staff_con_permiso_modelo_no_ve_auditoria_tecnica(self):
        usuario = User.objects.create_user(
            "staff_auditoria", password="ClaveLarga-2026", is_staff=True
        )
        usuario.user_permissions.add(Permission.objects.get(codename="view_sesionactiva"))
        solicitud = RequestFactory().get("/django-admin/pedidos/sesionactiva/")
        solicitud.user = usuario

        for modelo in (SesionActiva, EventoCliente, LogRecordatorio):
            modelo_admin = admin.site._registry[modelo]
            self.assertFalse(modelo_admin.has_module_permission(solicitud))
            self.assertFalse(modelo_admin.has_view_permission(solicitud))

    def test_admin_no_muestra_mensajes_historicos_ni_payload_de_eventos(self):
        log_admin = admin.site._registry[LogRecordatorio]
        evento_admin = admin.site._registry[EventoCliente]
        self.assertNotIn("mensaje_error", log_admin.fields)
        self.assertNotIn("mensaje_error", log_admin.list_display)
        self.assertTrue(
            {"detalle", "direccion_ip", "user_agent", "dispositivo_id"}.isdisjoint(
                evento_admin.fields
            )
        )


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RecordatoriosSinDatosPersonalesTests(TestCase):
    def setUp(self):
        self.nombre = "Sucursal Persona Reservada"
        self.correo = "persona.reservada@example.invalid"
        self.sucursal = SucursalCliente.objects.create(
            nombre=self.nombre,
            tipo=SucursalCliente.Tipo.SUCURSAL,
            email=self.correo,
        )

    def test_modo_test_no_imprime_nombre_ni_correo(self):
        salida = StringIO()

        call_command("enviar_recordatorios", test=True, fuerza=True, stdout=salida)

        self.assertIn(f"sucursal_id={self.sucursal.pk}", salida.getvalue())
        self.assertNotIn(self.nombre, salida.getvalue())
        self.assertNotIn(self.correo, salida.getvalue())

    def test_error_smtp_no_imprime_ni_guarda_respuesta_con_datos(self):
        sensible = f"Error de SMTP para {self.nombre} <{self.correo}>"
        salida = StringIO()

        with patch(
            "pedidos.management.commands.enviar_recordatorios.EmailMultiAlternatives.send",
            side_effect=RuntimeError(sensible),
        ), self.assertLogs("pedidos.management.commands.enviar_recordatorios", level="ERROR") as logs:
            call_command("enviar_recordatorios", fuerza=True, stdout=salida)

        registro = LogRecordatorio.objects.get(sucursal_cliente=self.sucursal)
        observado = salida.getvalue() + " ".join(logs.output) + registro.mensaje_error
        self.assertEqual(registro.estado, LogRecordatorio.Estado.ERROR)
        self.assertNotIn(self.nombre, observado)
        self.assertNotIn(self.correo, observado)
        self.assertNotIn(sensible, observado)
        self.assertIn("RuntimeError", observado)

    def test_error_de_plantilla_tampoco_expone_contexto_personal(self):
        sensible = f"Plantilla falló para {self.nombre} <{self.correo}>"
        salida = StringIO()

        with patch(
            "pedidos.management.commands.enviar_recordatorios.render_to_string",
            side_effect=RuntimeError(sensible),
        ), self.assertLogs("pedidos.management.commands.enviar_recordatorios", level="ERROR") as logs:
            call_command("enviar_recordatorios", test=True, fuerza=True, stdout=salida)

        registro = LogRecordatorio.objects.get(sucursal_cliente=self.sucursal)
        observado = salida.getvalue() + " ".join(logs.output) + registro.mensaje_error
        self.assertEqual(registro.estado, LogRecordatorio.Estado.ERROR)
        self.assertNotIn(self.nombre, observado)
        self.assertNotIn(self.correo, observado)
        self.assertNotIn(sensible, observado)

    def test_filtro_inexistente_no_repite_el_nombre_suministrado(self):
        with self.assertRaises(CommandError) as error:
            call_command(
                "enviar_recordatorios", fuerza=True,
                sucursal="nombre-sensible-inexistente", stdout=StringIO(),
            )

        self.assertNotIn("nombre-sensible-inexistente", str(error.exception))
