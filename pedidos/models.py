import uuid
from datetime import time
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Max, Sum
from django.utils import timezone

# Cache key compartida entre views.py y admin.py para invalidar la
# configuración cacheada cuando se edita desde cualquiera de los dos admins.
CONFIGURACION_CACHE_KEY = "pedidos:configuracion"
MAX_PEDIDOS_POR_DIA = 5


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


class PosApiCredential(models.Model):
    """Credencial v2 de un Edge, limitada a una sucursal de Pedidos.

    Sólo se conserva el SHA-256 del bearer aleatorio; la credencial v1 global
    permanece separada para los consumidores existentes.
    """

    credential_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    edge_id = models.UUIDField(db_index=True)
    # Identidad de sucursal del POS; NULL sólo para credenciales E2E anteriores.
    pos_branch_id = models.UUIDField(null=True, blank=True, db_index=True)
    sucursal_cliente = models.ForeignKey(
        SucursalCliente, on_delete=models.PROTECT, related_name="credenciales_pos_v2"
    )
    token_sha256 = models.CharField(max_length=64, unique=True, editable=False)
    scopes = models.JSONField(default=list)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    issued_reference = models.CharField(max_length=120, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reference = models.CharField(max_length=120, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    rotated_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="rotations",
    )

    class Meta:
        indexes = [models.Index(fields=["edge_id", "sucursal_cliente"])]

    def __str__(self):
        return str(self.credential_id)


class PosRetentionRecovery(models.Model):
    """Acta técnica de una conciliación; nunca contiene cuerpos de pedidos."""

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente de conciliación Edge"
        MANUAL = "intervencion_manual", "Pérdida o discrepancia por resolver"
        COMPLETADA = "completada", "Conciliación verificada"

    recovery_id = models.UUIDField(primary_key=True, editable=False)
    edge_id = models.UUIDField(db_index=True)
    pos_branch_id = models.UUIDField()
    sucursal_cliente = models.ForeignKey(
        SucursalCliente, on_delete=models.PROTECT, related_name="recuperaciones_pos_v2"
    )
    desde = models.DateTimeField()
    hasta = models.DateTimeField()
    old_cursor_sha256 = models.CharField(max_length=64)
    purge_epoch = models.CharField(max_length=64)
    snapshot_sha256 = models.CharField(max_length=64)
    orders_sha256 = models.CharField(max_length=64)
    orders_count = models.PositiveIntegerField()
    tombstones_sha256 = models.CharField(max_length=64)
    tombstones_count = models.PositiveIntegerField()
    estado = models.CharField(max_length=24, choices=Estado.choices, default=Estado.PENDIENTE)
    referencia_apertura = models.CharField(max_length=120)
    operador_apertura = models.CharField(max_length=64)
    referencia_cierre = models.CharField(max_length=120, blank=True)
    operador_cierre = models.CharField(max_length=64, blank=True)
    edge_ack_sha256 = models.CharField(max_length=64, blank=True)
    custody_sha256 = models.CharField(max_length=64, blank=True)
    freeze_evidence_sha256 = models.CharField(max_length=64, blank=True)
    creada_en = models.DateTimeField(auto_now_add=True)
    cerrada_en = models.DateTimeField(null=True, blank=True)


class Producto(models.Model):
    """Producto disponible para pedido."""

    nombre = models.CharField(max_length=80, unique=True)
    nombre_ticket = models.CharField(
        max_length=24,
        blank=True,
        help_text="Nombre breve usado en el ticket termico.",
    )
    unidad_medida = models.CharField(max_length=40, default="PIEZA (PZA)")
    unidad_abreviatura = models.CharField(max_length=8, default="PZA")
    cantidad_por_precio = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=Decimal("1.000"),
        validators=[
            MinValueValidator(Decimal("0.001")),
            MaxValueValidator(Decimal("999.999")),
        ],
        help_text=(
            "Cantidad capturada que equivale a una unidad de precio. "
            "Ejemplo: chile guero = 30 piezas por kilo."
        ),
    )
    orden = models.PositiveSmallIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["orden", "nombre"]

    def __str__(self):
        return self.nombre

    @property
    def etiqueta_ticket(self):
        return (self.nombre_ticket.strip() or self.nombre.strip())[:24]

    @property
    def unidad_corta(self):
        return (self.unidad_abreviatura.strip() or "PZA")[:8].upper()


class Precio(models.Model):
    """Precio vigente de un producto para una sucursal o cliente."""

    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="precios")
    sucursal_cliente = models.ForeignKey(
        SucursalCliente,
        on_delete=models.CASCADE,
        related_name="precios",
    )
    precio_unitario = models.DecimalField(max_digits=6, decimal_places=2)
    nombre_ticket = models.CharField(
        max_length=24,
        blank=True,
        help_text="Nombre breve usado para esta sucursal/cliente; si se deja vacio usa el del producto.",
    )
    fecha_vigencia = models.DateField(default=timezone.localdate)

    class Meta:
        ordering = ["producto__orden", "-fecha_vigencia"]
        indexes = [
            models.Index(
                fields=["producto", "sucursal_cliente", "-fecha_vigencia"],
                name="precio_vigente_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["producto", "sucursal_cliente", "fecha_vigencia"],
                name="precio_unico_por_fecha",
            )
        ]

    def __str__(self):
        return f"{self.sucursal_cliente} - {self.producto}: ${self.precio_unitario}"

    @property
    def etiqueta_ticket(self):
        return (self.nombre_ticket.strip() or self.producto.etiqueta_ticket)[:24]


class MacroPedido(models.Model):
    """Agrupador operativo de los pedidos de una sucursal en un día local."""

    class Estado(models.TextChoices):
        CONFIRMADO = "confirmado", "Confirmado"
        ENVIADO = "enviado", "Enviado"
        RECIBIDO = "recibido", "Recibido"

    sucursal_cliente = models.ForeignKey(
        SucursalCliente,
        on_delete=models.PROTECT,
        related_name="macropedidos",
    )
    codigo_publico = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    fecha_pedido = models.DateField(db_index=True)
    ultima_confirmacion = models.DateTimeField()
    estado = models.CharField(
        max_length=16,
        choices=Estado.choices,
        default=Estado.CONFIRMADO,
    )
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    eliminado = models.BooleanField(default=False)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-fecha_pedido", "-ultima_confirmacion"]
        constraints = [
            models.UniqueConstraint(
                fields=["sucursal_cliente", "fecha_pedido"],
                name="macropedido_unico_por_sucursal_dia",
            )
        ]
        indexes = [
            models.Index(
                fields=["sucursal_cliente", "-fecha_pedido"],
                name="macro_sucursal_fecha_idx",
            )
        ]

    def __str__(self):
        return f"Macropedido {self.folio_fecha} - {self.sucursal_cliente}"

    @property
    def fecha_referencia(self):
        return self.ultima_confirmacion

    @property
    def folio_fecha(self):
        hora = timezone.localtime(self.ultima_confirmacion).strftime("%H:%M")
        return f"{self.fecha_pedido:%d/%m/%Y} {hora}"

    @property
    def folio_dia(self):
        return self.fecha_pedido.strftime("%d/%m/%Y")

    @property
    def cantidad_pedidos(self):
        return self.pedidos.filter(eliminado=False).exclude(estado=Pedido.Estado.PENDIENTE).count()

    @property
    def cantidad_items(self):
        return (
            self.pedidos.filter(eliminado=False)
            .exclude(estado=Pedido.Estado.PENDIENTE)
            .values("items__producto_id")
            .exclude(items__producto_id__isnull=True)
            .distinct()
            .count()
        )

    def recalcular_resumen(self):
        resumen = self.pedidos.filter(eliminado=False).exclude(
            estado=Pedido.Estado.PENDIENTE
        ).aggregate(total=Sum("total"), ultima=Max("fecha_confirmacion"))
        self.total = (resumen["total"] or Decimal("0.00")).quantize(Decimal("0.01"))
        if resumen["ultima"] is not None:
            self.ultima_confirmacion = resumen["ultima"]
        self.save(update_fields=["total", "ultima_confirmacion", "fecha_actualizacion"])
        return self.total


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
    macropedido = models.ForeignKey(
        MacroPedido,
        on_delete=models.PROTECT,
        related_name="pedidos",
        null=True,
        blank=True,
    )
    codigo_publico = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    usuario_nombre = models.CharField(max_length=150)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    # Primera recepción en el VPS. Los registros migrados quedan en NULL hasta
    # un backfill documentado; nunca se deriva de la fecha de negocio.
    first_received_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
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
        constraints = [
            models.UniqueConstraint(
                fields=["sucursal_cliente"],
                condition=models.Q(estado="pendiente", eliminado=False),
                name="pedido_pendiente_unico_por_sucursal",
            )
        ]

    def __str__(self):
        return f"Pedido {self.folio_fecha} - {self.sucursal_cliente}"

    @property
    def fecha_referencia(self):
        return self.fecha_confirmacion or self.fecha_creacion

    @property
    def folio_fecha(self):
        if self.fecha_referencia is None:
            return "sin fecha"
        return timezone.localtime(self.fecha_referencia).strftime("%d/%m/%Y %H:%M")

    @property
    def folio_archivo(self):
        if self.fecha_referencia is None:
            return "sin_fecha"
        return timezone.localtime(self.fecha_referencia).strftime("%Y%m%d_%H%M%S")

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
        divisor = self.producto.cantidad_por_precio or Decimal("1.000")
        return ((self.cantidad / divisor) * self.precio_unitario).quantize(Decimal("0.01"))

    def save(self, *args, **kwargs):
        self.subtotal = self.calcular_subtotal()
        super().save(*args, **kwargs)


class ExportacionRetencion(models.Model):
    """Comprobante técnico de un ZIP generado y, después, verificado en destino."""

    class Estado(models.TextChoices):
        GENERADA = "generada", "Generada"
        CONFIRMADA = "confirmada", "Confirmada en destino"
        INVALIDADA = "invalidada", "Invalidada por cambios"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.GENERADA)
    generada_en = models.DateTimeField(auto_now_add=True)
    confirmada_en = models.DateTimeField(null=True, blank=True)
    archivo_local_eliminado_en = models.DateTimeField(null=True, blank=True)
    desde_recepcion = models.DateTimeField()
    hasta_recepcion = models.DateTimeField()
    sha256_archivo = models.CharField(max_length=64)
    sha256_contenido = models.CharField(max_length=64)
    archivo_local = models.CharField(max_length=500, blank=True)
    numero_pedidos = models.PositiveIntegerField()
    numero_items = models.PositiveIntegerField()
    version_formato = models.PositiveSmallIntegerField(default=1)
    referencia_confirmacion = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-generada_en"]


class PedidoEnExportacion(models.Model):
    """Identidad y firma de cada pedido del lote; desaparece con el pedido."""

    exportacion = models.ForeignKey(
        ExportacionRetencion, on_delete=models.CASCADE, related_name="miembros"
    )
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="exportaciones_retencion")
    sha256_contenido = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["exportacion", "pedido"], name="pedido_unico_por_exportacion"
            )
        ]


class RegistroPurga(models.Model):
    """Recibo mínimo, sin contenido transaccional, para auditoría y cursores POS."""

    ejecutada_en = models.DateTimeField(auto_now_add=True)
    motivo = models.CharField(max_length=24)
    numero_pedidos = models.PositiveIntegerField()
    numero_items = models.PositiveIntegerField()
    numero_macropedidos = models.PositiveIntegerField()
    numero_eventos = models.PositiveIntegerField(default=0)
    fecha_confirmacion_min = models.DateTimeField(null=True, blank=True)
    fecha_confirmacion_max = models.DateTimeField(null=True, blank=True)
    exportacion = models.ForeignKey(
        ExportacionRetencion, null=True, blank=True,
        on_delete=models.PROTECT, related_name="purgas",
    )


class PedidoPurgado(models.Model):
    """Tombstone técnico para impedir reintroducción al restaurar copias antiguas."""

    codigo_publico = models.UUIDField(primary_key=True, editable=False)
    pedido_id_origen = models.PositiveBigIntegerField(unique=True)
    sucursal_cliente_id = models.PositiveBigIntegerField(db_index=True)
    fecha_confirmacion = models.DateTimeField(null=True, blank=True, db_index=True)
    motivo = models.CharField(max_length=24)
    exportacion = models.ForeignKey(
        ExportacionRetencion, null=True, blank=True,
        on_delete=models.PROTECT, related_name="pedidos_purgados",
    )
    registro = models.ForeignKey(
        RegistroPurga, on_delete=models.PROTECT, related_name="pedidos_purgados"
    )


class SesionActiva(models.Model):
    """Arrendamiento persistente para permitir un solo dispositivo por usuario."""

    usuario = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="sesion_activa_pedidos",
    )
    token = models.CharField(max_length=64, unique=True, editable=False)
    dispositivo_id = models.CharField(max_length=64, blank=True)
    dispositivo = models.CharField(max_length=200, blank=True)
    direccion_ip = models.GenericIPAddressField(null=True, blank=True)
    iniciada_en = models.DateTimeField(auto_now_add=True)
    ultima_actividad = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = "Sesión activa"
        verbose_name_plural = "Sesiones activas"

    def __str__(self):
        return f"{self.usuario} — {self.dispositivo or 'dispositivo desconocido'}"


class EventoCliente(models.Model):
    """Auditoría durable de interacción del navegador y confirmación del servidor."""

    evento_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    usuario = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="eventos_cliente_pedidos",
    )
    sucursal_cliente = models.ForeignKey(
        SucursalCliente,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="eventos_cliente",
    )
    evento = models.CharField(max_length=80, db_index=True)
    intento_id = models.CharField(max_length=64, blank=True, db_index=True)
    dispositivo_id = models.CharField(max_length=64, blank=True, db_index=True)
    sesion_hash = models.CharField(max_length=16, blank=True)
    ocurrido_en = models.DateTimeField(null=True, blank=True, db_index=True)
    recibido_en = models.DateTimeField(auto_now_add=True, db_index=True)
    first_received_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    detalle = models.JSONField(default=dict, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    direccion_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-recibido_en"]
        indexes = [
            models.Index(
                fields=["sucursal_cliente", "-recibido_en"],
                name="evento_sucursal_fecha_idx",
            ),
        ]
        verbose_name = "Evento de cliente"
        verbose_name_plural = "Eventos de cliente"

    def __str__(self):
        return f"{self.evento} — {self.usuario or 'anónimo'}"


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
            'Nombre visible del remitente, ej. "Los Tocayos <tocayos.tacos@gmail.com>". '
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


class MFAEstado(models.Model):
    """Version de credenciales y bloqueo OTP compartidos entre workers."""

    usuario = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="estado_mfa_pedidos"
    )
    version = models.PositiveBigIntegerField(default=1)
    fallos = models.PositiveSmallIntegerField(default=0)
    bloqueado_hasta = models.DateTimeField(null=True, blank=True)


class MFACodigoRecuperacion(models.Model):
    """Solo el SHA-256 de un codigo aleatorio de alta entropia, nunca el codigo."""

    usuario = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="codigos_recuperacion_mfa_pedidos"
    )
    digest = models.CharField(max_length=64, unique=True, editable=False)
    creado_en = models.DateTimeField(auto_now_add=True)
    usado_en = models.DateTimeField(null=True, blank=True)
