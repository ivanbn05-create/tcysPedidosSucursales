from decimal import Decimal

from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone


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

    class Meta:
        ordering = ["tipo", "nombre"]
        verbose_name = "Sucursal/cliente"
        verbose_name_plural = "Sucursales/clientes"

    def __str__(self):
        return self.nombre


class Producto(models.Model):
    """Producto disponible para pedido."""

    nombre = models.CharField(max_length=80, unique=True)
    descripcion = models.TextField(blank=True)
    orden = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["orden", "nombre"]

    def __str__(self):
        return self.nombre


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
