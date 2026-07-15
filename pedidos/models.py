from datetime import time
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone

# Cache key compartida entre views.py y admin.py para invalidar la
# configuración cacheada cuando se edita desde cualquiera de los dos admins.
CONFIGURACION_CACHE_KEY = "pedidos:configuracion"


class SucursalCliente(models.Model):
    """Sucursal o cliente mayorista que puede crear pedidos."""

    class Tipo(models.TextChoices):
        SUCURSAL = "sucursal", "Sucursal"
        CLIENTE_MAYORISTA = "cliente_mayorista", "Cliente mayorista"

    usuario = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="perfil_pedidos",
    )
    nombre = models.CharField(max_length=120, unique=True)
    tipo = models.CharField(max_length=24, choices=Tipo.choices)
    activa = models.BooleanField(default=True)
    email = models.EmailField(
        blank=True,
        help_text="Correo al que se envía el recordatorio diario de pedidos.",
    )

    class Meta:
        ordering = ["tipo", "nombre"]
        verbose_name = "Sucursal/cliente"
        verbose_name_plural = "Sucursales/clientes"

    def __str__(self):
        return self.nombre


class Producto(models.Model):
    """Producto disponible para pedido."""

    nombre = models.CharField(max_length=80, unique=True)
    nombre_ticket = models.CharField(
        max_length=24,
        blank=True,
        help_text="Nombre breve usado en el ticket termico.",
    )
    descripcion = models.TextField(blank=True)
    orden = models.PositiveSmallIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["orden", "nombre"]

    def __str__(self):
        return self.nombre

    @property
    def etiqueta_ticket(self):
        return (self.nombre_ticket.strip() or self.nombre.strip())[:24]


class Precio(models.Model):
    """Precio vigente de un producto para una sucursal o cliente."""

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="precios")
    sucursal_cliente = models.ForeignKey(
        SucursalCliente,
        on_delete=models.CASCADE,
        related_name="precios",
    )
    precio_unitario = models.DecimalField(max_digits=6, decimal_places=2)
    fecha_vigencia = models.DateField(default=timezone.localdate)

    class Meta:
        ordering = ["producto__orden", "-fecha_vigencia"]
        constraints = [
            models.UniqueConstraint(
                fields=["producto", "sucursal_cliente", "fecha_vigencia"],
                name="precio_unico_por_fecha",
            )
        ]

    def __str__(self):
        return f"{self.sucursal_cliente} - {self.producto}: ${self.precio_unitario}"


class Pedido(models.Model):
    """Pedido creado por una sucursal o cliente."""

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        CONFIRMADO = "confirmado", "Confirmado"
        ENVIADO = "enviado", "Enviado"
        RECIBIDO = "recibido", "Recibido"

    sucursal_cliente = models.ForeignKey(
        SucursalCliente,
        on_delete=models.PROTECT,
        related_name="pedidos",
    )
    usuario_nombre = models.CharField(max_length=150)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_confirmacion = models.DateTimeField(null=True, blank=True)
    estado = models.CharField(
        max_length=16,
        choices=Estado.choices,
        default=Estado.PENDIENTE,
    )
    total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    eliminado = models.BooleanField(default=False)

    class Meta:
        ordering = ["-fecha_creacion"]

    def __str__(self):
        return f"Pedido #{self.pk} - {self.sucursal_cliente}"

    @property
    def cantidad_items(self):
        return self.items.count()

    def recalcular_total(self):
        total = (
            self.items.model.objects.filter(pedido_id=self.pk).aggregate(total=Sum("subtotal"))["total"]
            or Decimal("0.00")
        )
        self.total = total.quantize(Decimal("0.01"))
        self.save(update_fields=["total"])
        return self.total


class ItemPedido(models.Model):
    """Producto y cantidad capturados dentro de un pedido."""

    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="items")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="items_pedido")
    cantidad = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        validators=[
            MinValueValidator(Decimal("0.001")),
            MaxValueValidator(Decimal("999.999")),
        ],
    )
    precio_unitario = models.DecimalField(max_digits=6, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ["producto__orden"]
        constraints = [
            models.UniqueConstraint(
                fields=["pedido", "producto"],
                name="item_unico_por_producto_en_pedido",
            )
        ]

    def __str__(self):
        return f"{self.producto} x {self.cantidad}"

    def calcular_subtotal(self):
        return (self.cantidad * self.precio_unitario).quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        self.subtotal = self.calcular_subtotal()
        super().save(*args, **kwargs)


class Configuracion(models.Model):
    """Configuración global de horario de pedidos y recordatorios por correo.

    Debe existir un único registro (patrón singleton, ver ``get_solo``). Las
    credenciales SMTP (usuario/password de Gmail) NO viven aquí: se leen de
    variables de entorno como el resto de secretos del proyecto (ver
    ``settings.py`` y ``.env``), para no guardar contraseñas en texto plano
    en la base de datos.
    """

    hora_inicio_pedidos = models.TimeField(
        default=time(8, 0),
        help_text="Hora a partir de la cual se aceptan pedidos.",
    )
    hora_fin_pedidos = models.TimeField(
        default=time(16, 0),
        help_text="Hora hasta la cual se aceptan pedidos.",
    )
    hora_envio_recordatorio = models.TimeField(
        default=time(14, 0),
        help_text="Hora a la que se envía el recordatorio diario de pedidos.",
    )
    dias_recordatorio = models.CharField(
        max_length=20,
        default="1,2,3,4,5",
        help_text="Días ISO (1=lunes ... 7=domingo) separados por coma en que se envía el recordatorio.",
    )
    email_remitente = models.CharField(
        max_length=255,
        blank=True,
        help_text=(
            'Nombre visible del remitente, ej. "Los Tocayos <correos@lostocayos.com>". '
            "Si se deja vacío se usa DEFAULT_FROM_EMAIL/EMAIL_HOST_USER."
        ),
    )
    recordatorios_habilitados = models.BooleanField(
        default=True,
        help_text="Habilitar o deshabilitar el envío de recordatorios diarios.",
    )
    actualizado_en = models.DateTimeField(auto_now=True)
    actualizado_por = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = "Configuración"
        verbose_name_plural = "Configuración del sistema"

    def __str__(self):
        return "Configuración de pedidos y recordatorios"

    def clean(self):
        if self.hora_inicio_pedidos >= self.hora_fin_pedidos:
            raise ValidationError(
                "La hora de inicio de pedidos debe ser menor que la hora de fin."
            )

    def dias_recordatorio_lista(self):
        """Regresa los días ISO configurados como lista de enteros (1=lunes...7=domingo)."""
        dias = []
        for part in self.dias_recordatorio.split(","):
            part = part.strip()
            if part.isdigit():
                dias.append(int(part))
        return dias

    @classmethod
    def get_solo(cls):
        """Regresa el único registro de configuración, creándolo si no existe."""
        config = cls.objects.first()
        if config is None:
            config = cls.objects.create()
        return config


class LogRecordatorio(models.Model):
    """Registro de intentos de envío del recordatorio diario por sucursal/cliente."""

    class Estado(models.TextChoices):
        ENVIADO = "enviado", "Enviado exitosamente"
        ERROR = "error", "Error al enviar"
        SALTADO = "saltado", "Saltado (sin correo)"

    sucursal_cliente = models.ForeignKey(
        SucursalCliente,
        on_delete=models.CASCADE,
        related_name="logs_recordatorio",
    )
    fecha_envio = models.DateTimeField(auto_now_add=True)
    estado = models.CharField(max_length=16, choices=Estado.choices)
    mensaje_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-fecha_envio"]
        verbose_name = "Log de recordatorio"
        verbose_name_plural = "Logs de recordatorios"

    def __str__(self):
        return f"{self.sucursal_cliente} - {self.fecha_envio:%Y-%m-%d %H:%M} ({self.estado})"
