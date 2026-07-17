import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods, require_POST

from .models import (
    CONFIGURACION_CACHE_KEY,
    Configuracion,
    ItemPedido,
    Pedido,
    Precio,
    Producto,
    SucursalCliente,
)
from .tickets import build_ticket_workbook, format_ticket_quantity, ticket_context

logger = logging.getLogger(__name__)

CONFIGURACION_CACHE_TIMEOUT = 300  # 5 minutos
PRINT_GROUP_NAME = "Operador de impresion"
ORDER_HISTORY_STATES = [
    Pedido.Estado.CONFIRMADO,
    Pedido.Estado.ENVIADO,
    Pedido.Estado.RECIBIDO,
]
AGUAS_SUCURSALES = (
    ("Estancia", "E"),
    ("Aguilas", "A"),
    ("Fortin", "F"),
)
AGUAS_PRODUCTOS = (
    ("1/B", "AGUA HORCHATA BLANCA 1/2"),
    ("LB", "AGUA HORCHATA BLANCA LT"),
    ("1/R", "AGUA HORCHATA ROSA 1/2"),
    ("LR", "AGUA HORCHATA ROSA LT"),
    ("1/J", "AGUA JAMAICA 1/2"),
    ("LJ", "AGUA JAMAICA LT"),
)
WEEKDAY_LABELS = {
    1: "Lunes",
    2: "Martes",
    3: "Miercoles",
    4: "Jueves",
    5: "Viernes",
    6: "Sabado",
    7: "Domingo",
}


def is_admin_user(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def is_print_user(user):
    return (
        user.is_authenticated
        and not is_admin_user(user)
        and user.groups.filter(name=PRINT_GROUP_NAME).exists()
    )


def can_view_admin_dashboard(user):
    return is_admin_user(user) or is_print_user(user)


def admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_admin_user(request.user):
            messages.error(request, "No tienes permiso para entrar al panel admin.")
            return redirect("pedidos")
        return view_func(request, *args, **kwargs)

    return wrapped


def dashboard_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not can_view_admin_dashboard(request.user):
            messages.error(request, "No tienes permiso para entrar al panel admin.")
            return redirect("pedidos")
        return view_func(request, *args, **kwargs)

    return wrapped


def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if can_view_admin_dashboard(request.user):
        return redirect("admin_dashboard")
    return redirect("pedidos")


def privacidad_view(request):
    return render(request, "pedidos/privacidad.html")


@require_http_methods(["GET", "POST"])
def login_view(request):
    """Login compatible con nombres visibles y usernames internos sin espacios."""

    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        identificador = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        username = identificador

        sucursal = (
            SucursalCliente.objects.select_related("usuario")
            .filter(nombre__iexact=identificador, activa=True)
            .first()
        )
        if sucursal and sucursal.usuario:
            username = sucursal.usuario.username
        else:
            user_match = User.objects.filter(username__iexact=identificador).first()
            if user_match:
                username = user_match.username

        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("admin_dashboard" if can_view_admin_dashboard(user) else "pedidos")

        messages.error(request, "Usuario o contraseña incorrectos.")

    return render(request, "pedidos/login.html", horario_login_context())


@require_http_methods(["GET", "POST"])
def logout_view(request):
    logout(request)
    return redirect("login")


def sucursal_para_usuario(user):
    try:
        perfil = user.perfil_pedidos
    except SucursalCliente.DoesNotExist:
        return None
    return perfil if perfil.activa else None


def precio_vigente(producto, sucursal):
    return (
        Precio.objects.filter(
            producto=producto,
            sucursal_cliente=sucursal,
            fecha_vigencia__lte=timezone.localdate(),
        )
        .order_by("-fecha_vigencia")
        .first()
    )


def pedido_pendiente(sucursal, crear=False):
    pedido = (
        Pedido.objects.filter(
            sucursal_cliente=sucursal,
            estado=Pedido.Estado.PENDIENTE,
            eliminado=False,
        )
        .order_by("-fecha_creacion")
        .first()
    )
    if pedido is None and crear:
        pedido = Pedido.objects.create(
            sucursal_cliente=sucursal,
            usuario_nombre=sucursal.nombre,
        )
    return pedido


def get_configuracion():
    """Configuración de horarios/recordatorios, cacheada para no pegarle a la BD
    en cada request. Se invalida al guardar desde cualquiera de los dos admins."""

    config = cache.get(CONFIGURACION_CACHE_KEY)
    if config is None:
        config = Configuracion.get_solo()
        cache.set(CONFIGURACION_CACHE_KEY, config, CONFIGURACION_CACHE_TIMEOUT)
    return config


def validar_horario_pedidos():
    """Regresa (es_valido, mensaje) según el horario configurado en Configuracion."""

    config = get_configuracion()
    ahora = timezone.localtime().time()
    dentro_horario = config.hora_inicio_pedidos <= ahora <= config.hora_fin_pedidos
    if dentro_horario:
        mensaje = f"Pedidos habilitados hasta las {config.hora_fin_pedidos:%H:%M}."
    else:
        mensaje = f"Pedidos cerrados. Reabre a las {config.hora_inicio_pedidos:%H:%M}."
    return dentro_horario, mensaje


def horario_login_context():
    config = get_configuracion()
    dentro_horario, mensaje = validar_horario_pedidos()
    return {
        "horario_pedidos": {
            "dentro_horario": dentro_horario,
            "mensaje": mensaje,
            "hora_inicio": config.hora_inicio_pedidos.strftime("%H:%M"),
            "hora_fin": config.hora_fin_pedidos.strftime("%H:%M"),
        }
    }


def info_horarios(request):
    """Endpoint público (sin auth) que informa el horario vigente de aceptación
    de pedidos, para que el frontend habilite/deshabilite el botón de confirmar."""

    config = get_configuracion()
    ahora_dt = timezone.localtime()
    dentro_horario, mensaje = validar_horario_pedidos()
    return JsonResponse(
        {
            "hora_inicio": config.hora_inicio_pedidos.strftime("%H:%M"),
            "hora_fin": config.hora_fin_pedidos.strftime("%H:%M"),
            "hora_actual": ahora_dt.strftime("%H:%M"),
            "dentro_horario": dentro_horario,
            "mensaje": mensaje,
        }
    )


def decimal_to_str(value, places="0.01"):
    return str(Decimal(value).quantize(Decimal(places)))


def parse_filter_date(value):
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def etiqueta_ticket_para_item(item, fecha=None):
    fecha = fecha or timezone.localdate()
    precio = (
        Precio.objects.filter(
            producto=item.producto,
            sucursal_cliente=item.pedido.sucursal_cliente,
            fecha_vigencia__lte=fecha,
        )
        .order_by("-fecha_vigencia")
        .first()
    )
    if precio is not None:
        return precio.etiqueta_ticket
    return item.producto.etiqueta_ticket


def fecha_referencia_pedido(pedido):
    base = pedido.fecha_confirmacion or pedido.fecha_creacion
    return timezone.localtime(base).date()


def serializar_item(item, incluir_precios=False):
    fecha_pedido = fecha_referencia_pedido(item.pedido)
    cantidad_promocion = item.cantidad_bonificacion(fecha_pedido)
    data = {
        "id": item.id,
        "producto_id": item.producto_id,
        "producto": item.producto.nombre,
        "nombre_ticket": etiqueta_ticket_para_item(item, fecha_pedido),
        "unidad": item.producto.unidad_corta,
        "cantidad": decimal_to_str(item.cantidad, "0.001"),
        "cantidad_ticket": decimal_to_str(item.cantidad_con_promocion(fecha_pedido), "0.001"),
        "cantidad_promocion": decimal_to_str(cantidad_promocion, "0.001"),
    }
    if incluir_precios:
        data.update(
            {
                "precio_unitario": decimal_to_str(item.precio_unitario),
                "subtotal": decimal_to_str(item.subtotal),
            }
        )
    return data


def serializar_pedido(pedido, incluir_precios=False):
    if not pedido:
        return {"items": [], "total": "0.00"}
    items = pedido.items.select_related("producto").all()
    return {
        "items": [serializar_item(item, incluir_precios=incluir_precios) for item in items],
        "total": decimal_to_str(pedido.total),
    }


def parse_json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return None


def parse_cantidad(value):
    try:
        cantidad = Decimal(str(value)).quantize(Decimal("0.001"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if cantidad <= 0 or cantidad > Decimal("999.999"):
        return None
    return cantidad


@login_required
def pedidos_view(request):
    if can_view_admin_dashboard(request.user):
        return redirect("admin_dashboard")

    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        messages.error(request, "Tu usuario no tiene una sucursal o cliente activo.")
        return redirect("login")

    pedido = pedido_pendiente(sucursal)
    productos_query = Producto.objects.filter(
        activo=True,
        precios__sucursal_cliente=sucursal,
        precios__fecha_vigencia__lte=timezone.localdate(),
    )
    if pedido is not None:
        productos_query = Producto.objects.filter(
            Q(
                activo=True,
                precios__sucursal_cliente=sucursal,
                precios__fecha_vigencia__lte=timezone.localdate(),
            )
            | Q(items_pedido__pedido=pedido)
        ).distinct()
    productos = list(productos_query.distinct())
    productos_data = []
    for producto in productos:
        precio = precio_vigente(producto, sucursal)
        productos_data.append(
            {
                "id": producto.id,
                "nombre": producto.nombre,
                "nombre_ticket": precio.etiqueta_ticket if precio else producto.etiqueta_ticket,
                "unidad": producto.unidad_corta,
            }
        )

    context = {
        "sucursal": sucursal,
        "productos": productos,
        "initial_data": {
            "productos": productos_data,
            "pedido": serializar_pedido(pedido),
        },
    }
    return render(request, "pedidos/pedidos.html", context)


@require_POST
@login_required
def crear_item(request):
    if is_admin_user(request.user):
        return JsonResponse({"success": False, "mensaje": "Admin no puede crear pedidos."}, status=403)

    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        return JsonResponse({"success": False, "mensaje": "Usuario sin sucursal activa."}, status=403)

    payload = parse_json_body(request)
    if payload is None:
        return JsonResponse({"success": False, "mensaje": "JSON inválido."}, status=400)

    producto = get_object_or_404(Producto, pk=payload.get("producto_id"))
    cantidad = parse_cantidad(payload.get("cantidad"))
    if cantidad is None:
        return JsonResponse({"success": False, "mensaje": "Cantidad inválida."}, status=400)

    precio = precio_vigente(producto, sucursal)
    if precio is None:
        return JsonResponse({"success": False, "mensaje": "No hay precio vigente."}, status=400)

    with transaction.atomic():
        pedido = pedido_pendiente(sucursal, crear=True)
        item, created = ItemPedido.objects.select_for_update().get_or_create(
            pedido=pedido,
            producto=producto,
            defaults={
                "cantidad": cantidad,
                "precio_unitario": precio.precio_unitario,
            },
        )
        if not created:
            nueva_cantidad = cantidad
            if nueva_cantidad > Decimal("999.999"):
                return JsonResponse(
                    {"success": False, "mensaje": "La cantidad máxima es 999.999."},
                    status=400,
                )
            item.cantidad = nueva_cantidad
            item.precio_unitario = precio.precio_unitario
            item.save()
        pedido.recalcular_total()

    return JsonResponse(
        {
            "success": True,
            "mensaje": "Cantidad guardada.",
            "item_id": item.id,
            "total_pedido": decimal_to_str(pedido.total),
            "pedido": serializar_pedido(pedido),
        }
    )


@require_POST
@login_required
def eliminar_item(request):
    sucursal = sucursal_para_usuario(request.user)
    payload = parse_json_body(request)
    if sucursal is None or payload is None:
        return JsonResponse({"success": False, "mensaje": "Solicitud inválida."}, status=400)

    pedido = pedido_pendiente(sucursal)
    item = get_object_or_404(
        ItemPedido,
        pk=payload.get("item_id"),
        pedido=pedido,
        pedido__estado=Pedido.Estado.PENDIENTE,
    )
    item.delete()
    pedido.recalcular_total()
    return JsonResponse(
        {
            "success": True,
            "total_pedido": decimal_to_str(pedido.total),
            "pedido": serializar_pedido(pedido),
        }
    )


@require_POST
@login_required
def limpiar_pedido(request):
    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        return JsonResponse({"success": False, "mensaje": "Usuario sin sucursal activa."}, status=403)
    pedido = pedido_pendiente(sucursal)
    if pedido:
        pedido.items.all().delete()
        pedido.recalcular_total()
    return JsonResponse({"success": True, "pedido": {"items": [], "total": "0.00"}})


@require_POST
@login_required
def confirmar_pedido(request):
    if is_admin_user(request.user):
        return JsonResponse({"success": False, "mensaje": "Admin no puede confirmar pedidos."}, status=403)

    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        return JsonResponse({"success": False, "mensaje": "Usuario sin sucursal activa."}, status=403)

    es_valido, mensaje_horario = validar_horario_pedidos()
    if not es_valido:
        return JsonResponse({"success": False, "mensaje": mensaje_horario}, status=400)

    limite = timezone.now() - timedelta(seconds=60)
    if Pedido.objects.filter(
        sucursal_cliente=sucursal,
        fecha_confirmacion__gte=limite,
        estado__in=[Pedido.Estado.CONFIRMADO, Pedido.Estado.ENVIADO, Pedido.Estado.RECIBIDO],
        eliminado=False,
    ).exists():
        return JsonResponse(
            {"success": False, "mensaje": "Espera un minuto antes de confirmar otro pedido."},
            status=429,
        )

    with transaction.atomic():
        pedido = (
            Pedido.objects.select_for_update()
            .filter(
                sucursal_cliente=sucursal,
                estado=Pedido.Estado.PENDIENTE,
                eliminado=False,
            )
            .prefetch_related("items")
            .order_by("-fecha_creacion")
            .first()
        )
        if pedido is None or not pedido.items.exists():
            return JsonResponse(
                {"success": False, "mensaje": "No hay productos en el pedido."},
                status=400,
            )

        pedido.recalcular_total()
        pedido.estado = Pedido.Estado.CONFIRMADO
        pedido.fecha_confirmacion = timezone.now()
        pedido.save(update_fields=["estado", "fecha_confirmacion"])

    logger.info("Pedido confirmado #%s por %s", pedido.id, sucursal.nombre)
    return JsonResponse(
        {
            "success": True,
            "pedido_id": pedido.id,
            "pedido_folio": pedido.folio_fecha,
            "total": decimal_to_str(pedido.total),
            "mensaje": f"Pedido confirmado {pedido.folio_fecha}.",
        }
    )


def parse_admin_price(value):
    try:
        price = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("Captura precios validos con hasta dos decimales.")
    if price < 0 or price > Decimal("9999.99"):
        raise ValidationError("Cada precio debe estar entre 0 y 9999.99.")
    return price


def parse_admin_order(value):
    try:
        order = int(value)
    except (TypeError, ValueError):
        raise ValidationError("El orden de los productos debe ser un numero entero.")
    if order < 0 or order > 32767:
        raise ValidationError("El orden debe estar entre 0 y 32767.")
    return order


def parse_factor_precio(value):
    try:
        factor = Decimal(str(value)).quantize(Decimal("0.001"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError("El divisor de precio debe ser un numero con hasta tres decimales.")
    if factor <= 0 or factor > Decimal("999.999"):
        raise ValidationError("El divisor de precio debe estar entre 0.001 y 999.999.")
    return factor


def validation_error_text(error):
    if hasattr(error, "message_dict"):
        return " ".join(
            message
            for messages_list in error.message_dict.values()
            for message in messages_list
        )
    return " ".join(error.messages)


def update_products_from_post(request):
    updated = 0
    for producto in Producto.objects.all():
        prefix = f"producto_{producto.id}_"
        if f"{prefix}present" not in request.POST:
            continue

        nombre = request.POST.get(f"{prefix}nombre", "").strip()
        if not nombre:
            raise ValidationError("Todos los productos deben tener nombre.")

        producto.nombre = nombre
        producto.nombre_ticket = request.POST.get(f"{prefix}ticket", "").strip()
        producto.unidad_medida = request.POST.get(f"{prefix}unidad", "").strip() or "PIEZA (PZA)"
        producto.unidad_abreviatura = (
            request.POST.get(f"{prefix}unidad_abreviatura", "").strip().upper() or "PZA"
        )
        producto.cantidad_por_precio = parse_factor_precio(
            request.POST.get(f"{prefix}cantidad_por_precio", "1")
        )
        producto.promo_aguilas_martes = f"{prefix}promo_aguilas_martes" in request.POST
        producto.orden = parse_admin_order(request.POST.get(f"{prefix}orden"))
        producto.activo = f"{prefix}activo" in request.POST
        producto.full_clean()
        producto.save()
        updated += 1
    return f"{updated} productos actualizados."


def create_product_from_post(request):
    nombre = request.POST.get("nuevo_nombre", "").strip()
    if not nombre:
        raise ValidationError("El nuevo producto necesita un nombre.")
    producto = Producto(
        nombre=nombre,
        nombre_ticket=request.POST.get("nuevo_ticket", "").strip(),
        unidad_medida=request.POST.get("nuevo_unidad", "").strip() or "PIEZA (PZA)",
        unidad_abreviatura=(
            request.POST.get("nuevo_unidad_abreviatura", "").strip().upper() or "PZA"
        ),
        cantidad_por_precio=parse_factor_precio(
            request.POST.get("nuevo_cantidad_por_precio", "1")
        ),
        promo_aguilas_martes="nuevo_promo_aguilas_martes" in request.POST,
        orden=parse_admin_order(request.POST.get("nuevo_orden", "0")),
        activo=True,
    )
    producto.full_clean()
    producto.save()
    return f"Producto {producto.nombre} creado. Ahora asigna sus precios."


def update_prices_from_post(request):
    today = timezone.localdate()
    updated = 0
    products = Producto.objects.all()
    branches = SucursalCliente.objects.all()
    for producto in products:
        for branch in branches:
            field_name = f"precio_{producto.id}_{branch.id}"
            if field_name not in request.POST:
                continue
            raw_value = request.POST.get(field_name, "").strip()
            raw_ticket = request.POST.get(f"precio_ticket_{producto.id}_{branch.id}", "").strip()
            if not raw_value:
                continue
            Precio.objects.update_or_create(
                producto=producto,
                sucursal_cliente=branch,
                fecha_vigencia=today,
                defaults={
                    "precio_unitario": parse_admin_price(raw_value),
                    "nombre_ticket": raw_ticket[:24],
                },
            )
            updated += 1
    return f"{updated} precios vigentes actualizados."


def update_branches_from_post(request):
    updated = 0
    valid_types = {value for value, _ in SucursalCliente.Tipo.choices}
    for branch in SucursalCliente.objects.select_related("usuario"):
        prefix = f"sucursal_{branch.id}_"
        if f"{prefix}present" not in request.POST:
            continue

        nombre = request.POST.get(f"{prefix}nombre", "").strip()
        username = request.POST.get(f"{prefix}username", "").strip()
        branch_type = request.POST.get(f"{prefix}tipo", "")
        active = f"{prefix}activa" in request.POST
        password = request.POST.get(f"{prefix}password", "")
        email = request.POST.get(f"{prefix}email", "").strip()
        if not nombre or not username:
            raise ValidationError("Cada sucursal o cliente necesita nombre y usuario.")
        if branch_type not in valid_types:
            raise ValidationError("Selecciona un tipo de cliente valido.")

        user = branch.usuario or User()
        if not user.pk and not password:
            raise ValidationError(f"Captura una contrasena para el usuario {username}.")
        user.username = username
        user.first_name = nombre
        user.is_staff = False
        user.is_superuser = False
        user.is_active = active
        if password:
            user.set_password(password)
        user.full_clean()
        user.save()

        branch.nombre = nombre
        branch.tipo = branch_type
        branch.activa = active
        branch.usuario = user
        branch.email = email
        branch.full_clean()
        branch.save()
        updated += 1
    return f"{updated} usuarios y sucursales actualizados."


def create_branch_from_post(request):
    nombre = request.POST.get("nuevo_sucursal_nombre", "").strip()
    username = request.POST.get("nuevo_sucursal_username", "").strip()
    password = request.POST.get("nuevo_sucursal_password", "")
    branch_type = request.POST.get("nuevo_sucursal_tipo", "")
    valid_types = {value for value, _ in SucursalCliente.Tipo.choices}
    if not nombre or not username or not password:
        raise ValidationError("Nombre, usuario y contrasena son obligatorios.")
    if branch_type not in valid_types:
        raise ValidationError("Selecciona un tipo de cliente valido.")

    user = User(
        username=username,
        first_name=nombre,
        is_active=True,
        is_staff=False,
        is_superuser=False,
    )
    user.set_password(password)
    user.full_clean()
    user.save()
    email = request.POST.get("nuevo_sucursal_email", "").strip()
    branch = SucursalCliente(
        nombre=nombre,
        tipo=branch_type,
        activa=True,
        usuario=user,
        email=email,
    )
    branch.full_clean()
    branch.save()
    return f"Usuario {nombre} creado. Ahora asigna sus precios."


def update_admin_account_from_post(request):
    user = request.user
    username = request.POST.get("admin_username", "").strip()
    display_name = request.POST.get("admin_display_name", "").strip()
    password = request.POST.get("admin_password", "")
    if not username:
        raise ValidationError("La cuenta administradora necesita un usuario.")
    user.username = username
    user.first_name = display_name
    if password:
        user.set_password(password)
    user.full_clean()
    user.save()
    if password:
        update_session_auth_hash(request, user)
    return "Cuenta administradora actualizada."


DIAS_SEMANA_CHOICES = [
    (1, "Lunes"),
    (2, "Martes"),
    (3, "Miercoles"),
    (4, "Jueves"),
    (5, "Viernes"),
    (6, "Sabado"),
    (7, "Domingo"),
]


def parse_time_field(value, field_label):
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        raise ValidationError(f"Captura una hora valida (HH:MM) para {field_label}.")


def update_configuracion_from_post(request):
    config = Configuracion.get_solo()
    config.hora_inicio_pedidos = parse_time_field(
        request.POST.get("hora_inicio_pedidos"), "hora de inicio de pedidos"
    )
    config.hora_fin_pedidos = parse_time_field(
        request.POST.get("hora_fin_pedidos"), "hora de fin de pedidos"
    )
    config.hora_envio_recordatorio = parse_time_field(
        request.POST.get("hora_envio_recordatorio"), "hora de envio de recordatorio"
    )

    dias_seleccionados = request.POST.getlist("dias_recordatorio")
    dias_validos = {str(value) for value, _ in DIAS_SEMANA_CHOICES}
    if not dias_seleccionados or not set(dias_seleccionados).issubset(dias_validos):
        raise ValidationError("Selecciona al menos un dia valido para el recordatorio.")
    config.dias_recordatorio = ",".join(sorted(dias_seleccionados, key=int))

    config.email_remitente = request.POST.get("email_remitente", "").strip()
    config.recordatorios_habilitados = "recordatorios_habilitados" in request.POST
    config.actualizado_por = request.user.username
    config.full_clean()
    config.save()
    cache.delete(CONFIGURACION_CACHE_KEY)
    return "Horarios y recordatorios actualizados."


ADMIN_CONFIG_ACTIONS = {
    "actualizar_productos": update_products_from_post,
    "crear_producto": create_product_from_post,
    "actualizar_precios": update_prices_from_post,
    "actualizar_sucursales": update_branches_from_post,
    "crear_sucursal": create_branch_from_post,
    "actualizar_admin": update_admin_account_from_post,
    "actualizar_configuracion": update_configuracion_from_post,
}


def admin_configuration_context(request):
    products = list(Producto.objects.all())
    branches = list(SucursalCliente.objects.select_related("usuario").all())
    current_prices = {}
    prices = Precio.objects.filter(fecha_vigencia__lte=timezone.localdate()).order_by(
        "producto_id",
        "sucursal_cliente_id",
        "-fecha_vigencia",
    )
    for price in prices:
        current_prices.setdefault(
            (price.producto_id, price.sucursal_cliente_id),
            {
                "value": price.precio_unitario,
                "ticket": price.nombre_ticket,
            },
        )

    price_rows = [
        {
            "product": product,
            "prices": [
                {
                    "branch": branch,
                    "value": current_prices.get((product.id, branch.id), {}).get("value"),
                    "ticket": current_prices.get((product.id, branch.id), {}).get(
                        "ticket",
                        product.nombre_ticket,
                    ),
                }
                for branch in branches
            ],
        }
        for product in products
    ]
    return {
        "productos": products,
        "sucursales": branches,
        "tipos_sucursal": SucursalCliente.Tipo.choices,
        "price_rows": price_rows,
        "admin_user": request.user,
        "configuracion": Configuracion.get_solo(),
        "dias_semana": DIAS_SEMANA_CHOICES,
    }


@admin_required
@require_http_methods(["GET", "POST"])
def admin_configuracion(request):
    if request.method == "POST":
        action = request.POST.get("action", "")
        handler = ADMIN_CONFIG_ACTIONS.get(action)
        if handler is None:
            messages.error(request, "Accion de configuracion no reconocida.")
            return redirect("admin_configuracion")
        try:
            with transaction.atomic():
                success_message = handler(request)
        except ValidationError as error:
            messages.error(request, f"No se guardaron los cambios. {validation_error_text(error)}")
        except IntegrityError:
            messages.error(
                request,
                "No se guardaron los cambios. Ya existe un nombre o usuario con ese valor.",
            )
        else:
            messages.success(request, success_message)
        return redirect("admin_configuracion")

    return render(
        request,
        "pedidos/admin_configuracion.html",
        admin_configuration_context(request),
    )


def format_dashboard_quantity(value):
    decimal_value = Decimal(value or 0)
    if decimal_value <= 0:
        return "/"
    return format_ticket_quantity(decimal_value)


def aguas_print_context():
    source_date = timezone.localdate() - timedelta(days=1)
    branch_names = [name for name, _ in AGUAS_SUCURSALES]
    product_names = [name for _, name in AGUAS_PRODUCTOS]
    totals = defaultdict(Decimal)

    pedidos = (
        Pedido.objects.filter(
            eliminado=False,
            estado__in=ORDER_HISTORY_STATES,
            fecha_confirmacion__date=source_date,
            sucursal_cliente__nombre__in=branch_names,
        )
        .select_related("sucursal_cliente")
        .prefetch_related("items__producto")
    )

    for pedido in pedidos:
        branch_name = pedido.sucursal_cliente.nombre
        for item in pedido.items.all():
            product_name = item.producto.nombre
            if product_name in product_names:
                totals[(branch_name, product_name)] += item.cantidad

    rows = []
    for label, product_name in AGUAS_PRODUCTOS:
        rows.append(
            {
                "label": label,
                "values": [
                    format_dashboard_quantity(totals[(branch_name, product_name)])
                    for branch_name, _ in AGUAS_SUCURSALES
                ],
            }
        )

    return {
        "source_date": source_date,
        "print_date": source_date + timedelta(days=1),
        "branches": [short_name for _, short_name in AGUAS_SUCURSALES],
        "rows": rows,
    }


def pedido_history_queryset():
    return (
        Pedido.objects.filter(eliminado=False, estado__in=ORDER_HISTORY_STATES)
        .select_related("sucursal_cliente")
        .prefetch_related("items__producto")
        .order_by("fecha_confirmacion", "fecha_creacion")
    )


def pedido_local_date(pedido):
    base_date = pedido.fecha_confirmacion or pedido.fecha_creacion
    return timezone.localtime(base_date).date()


def empty_metric():
    return {
        "pedidos": 0,
        "total": Decimal("0.00"),
        "productos": 0,
        "unidades": Decimal("0.000"),
    }


def pct(value, maximum):
    if not maximum:
        return 0
    return int((Decimal(value) / Decimal(maximum)) * 100)


def money_decimal(value):
    return Decimal(value or 0).quantize(Decimal("0.01"))


def admin_datos_context(request):
    sucursales = list(SucursalCliente.objects.filter(activa=True).order_by("tipo", "nombre"))
    selected_sucursal_id = request.GET.get("sucursal", "").strip()
    selected_weekday_raw = request.GET.get("dia", "").strip()

    if not selected_sucursal_id and sucursales:
        selected_sucursal_id = str(sucursales[0].id)
    try:
        selected_weekday = int(selected_weekday_raw or timezone.localdate().isoweekday())
    except ValueError:
        selected_weekday = timezone.localdate().isoweekday()
    if selected_weekday not in WEEKDAY_LABELS:
        selected_weekday = timezone.localdate().isoweekday()

    selected_sucursal = next(
        (sucursal for sucursal in sucursales if str(sucursal.id) == selected_sucursal_id),
        sucursales[0] if sucursales else None,
    )

    pedidos = list(pedido_history_queryset())
    metrics = defaultdict(empty_metric)
    weekday_totals = defaultdict(Decimal)
    branch_totals = defaultdict(Decimal)
    product_mix = defaultdict(
        lambda: {
            "cantidad": Decimal("0.000"),
            "pedidos": set(),
            "ultimo": None,
        }
    )
    selected_order_count = 0
    total_general = Decimal("0.00")

    for pedido in pedidos:
        local_date = pedido_local_date(pedido)
        weekday = local_date.isoweekday()
        branch = pedido.sucursal_cliente
        items = list(pedido.items.all())
        item_count = len(items)
        units = sum((item.cantidad for item in items), Decimal("0.000"))
        key = (branch.id, weekday)

        metrics[key]["pedidos"] += 1
        metrics[key]["total"] += pedido.total
        metrics[key]["productos"] += item_count
        metrics[key]["unidades"] += units
        weekday_totals[weekday] += pedido.total
        branch_totals[branch.id] += pedido.total
        total_general += pedido.total

        if selected_sucursal and branch.id == selected_sucursal.id and weekday == selected_weekday:
            selected_order_count += 1
            for item in items:
                mix = product_mix[item.producto.nombre]
                mix["cantidad"] += item.cantidad
                mix["pedidos"].add(pedido.id)
                if mix["ultimo"] is None or local_date > mix["ultimo"]:
                    mix["ultimo"] = local_date

    average_rows = []
    for sucursal in sucursales:
        for weekday, weekday_label in WEEKDAY_LABELS.items():
            metric = metrics[(sucursal.id, weekday)]
            pedidos_count = metric["pedidos"]
            average_rows.append(
                {
                    "sucursal": sucursal.nombre,
                    "weekday": weekday_label,
                    "pedidos": pedidos_count,
                    "avg_total": money_decimal(metric["total"] / pedidos_count)
                    if pedidos_count
                    else Decimal("0.00"),
                    "avg_products": metric["productos"] / pedidos_count
                    if pedidos_count
                    else 0,
                    "avg_units": metric["unidades"] / pedidos_count
                    if pedidos_count
                    else Decimal("0.000"),
                }
            )

    max_weekday_total = max(weekday_totals.values(), default=Decimal("0.00"))
    weekday_chart = [
        {
            "label": label,
            "total": money_decimal(weekday_totals[weekday]),
            "bar": pct(weekday_totals[weekday], max_weekday_total),
        }
        for weekday, label in WEEKDAY_LABELS.items()
    ]

    branch_chart_raw = [
        {
            "label": sucursal.nombre,
            "total": money_decimal(branch_totals[sucursal.id]),
        }
        for sucursal in sucursales
    ]
    max_branch_total = max((row["total"] for row in branch_chart_raw), default=Decimal("0.00"))
    branch_chart = [
        {
            **row,
            "bar": pct(row["total"], max_branch_total),
        }
        for row in branch_chart_raw
    ]

    prediction_rows = []
    max_prediction_quantity = max(
        (data["cantidad"] for data in product_mix.values()),
        default=Decimal("0.000"),
    )
    for product_name, data in sorted(
        product_mix.items(),
        key=lambda item: (-item[1]["cantidad"], item[0]),
    )[:12]:
        pedidos_con_producto = len(data["pedidos"])
        prediction_rows.append(
            {
                "producto": product_name,
                "cantidad_promedio": data["cantidad"] / selected_order_count
                if selected_order_count
                else Decimal("0.000"),
                "frecuencia": int((pedidos_con_producto / selected_order_count) * 100)
                if selected_order_count
                else 0,
                "ultimo": data["ultimo"],
                "bar": pct(data["cantidad"], max_prediction_quantity),
            }
        )

    total_orders = len(pedidos)
    return {
        "sucursales": sucursales,
        "weekdays": list(WEEKDAY_LABELS.items()),
        "selected_sucursal": selected_sucursal,
        "selected_sucursal_id": str(selected_sucursal.id) if selected_sucursal else "",
        "selected_weekday": selected_weekday,
        "selected_weekday_label": WEEKDAY_LABELS[selected_weekday],
        "total_orders": total_orders,
        "total_revenue": money_decimal(total_general),
        "avg_ticket": money_decimal(total_general / total_orders) if total_orders else Decimal("0.00"),
        "average_rows": average_rows,
        "weekday_chart": weekday_chart,
        "branch_chart": branch_chart,
        "prediction_rows": prediction_rows,
        "selected_order_count": selected_order_count,
        "aguas_print": aguas_print_context(),
    }


@dashboard_required
def admin_dashboard(request):
    pedidos = (
        Pedido.objects.filter(eliminado=False)
        .select_related("sucursal_cliente")
        .prefetch_related("items__producto")
        .order_by("-fecha_creacion")
    )

    estado = request.GET.get("estado", "").strip()
    sucursal_id = request.GET.get("sucursal", "").strip()
    desde = request.GET.get("desde", "").strip()
    hasta = request.GET.get("hasta", "").strip()
    q = request.GET.get("q", "").strip()

    if estado:
        pedidos = pedidos.filter(estado=estado)
    if sucursal_id:
        pedidos = pedidos.filter(sucursal_cliente_id=sucursal_id)
    if desde:
        pedidos = pedidos.filter(fecha_creacion__date__gte=desde)
    if hasta:
        pedidos = pedidos.filter(fecha_creacion__date__lte=hasta)
    if q:
        filtro = Q(sucursal_cliente__nombre__icontains=q) | Q(usuario_nombre__icontains=q)
        fecha_busqueda = parse_filter_date(q)
        if fecha_busqueda:
            filtro |= Q(fecha_creacion__date=fecha_busqueda) | Q(
                fecha_confirmacion__date=fecha_busqueda
            )
        pedidos = pedidos.filter(filtro)

    pedidos_list = list(pedidos)
    for pedido in pedidos_list:
        pedido.items_json = json.dumps(
            [serializar_item(item, incluir_precios=True) for item in pedido.items.all()]
        )
        pedido.print_context = ticket_context(pedido)

    hoy = timezone.localdate()
    stats_base = Pedido.objects.filter(eliminado=False)
    stats = {
        "pendientes": stats_base.filter(estado=Pedido.Estado.CONFIRMADO).count(),
        "total_hoy": stats_base.filter(fecha_creacion__date=hoy).aggregate(total=Sum("total"))["total"]
        or Decimal("0.00"),
        "pedidos_hoy": stats_base.filter(fecha_creacion__date=hoy).count(),
    }

    context = {
        "pedidos": pedidos_list,
        "sucursales": SucursalCliente.objects.filter(activa=True),
        "estados": Pedido.Estado.choices,
        "stats": stats,
        "filters": {
            "estado": estado,
            "sucursal": sucursal_id,
            "desde": desde,
            "hasta": hasta,
            "q": q,
        },
        "can_manage_pedidos": is_admin_user(request.user),
        "aguas_print": aguas_print_context(),
    }
    return render(request, "pedidos/admin_dashboard.html", context)


@dashboard_required
def imprimir_aguas(request):
    logger.info("Admin %s abrio impresion de aguas", request.user.username)
    context = aguas_print_context()
    context["auto_print"] = request.GET.get("embedded") != "1"
    return render(request, "pedidos/aguas_print.html", context)


@admin_required
def admin_datos(request):
    return render(request, "pedidos/admin_datos.html", admin_datos_context(request))


def excel_response_for_pedido(pedido):
    output = build_ticket_workbook(pedido)
    sucursal = slugify(pedido.sucursal_cliente.nombre) or "pedido"
    filename = f"pedido_{sucursal}_{pedido.folio_archivo}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@admin_required
def descargar_excel(request, pedido_id):
    pedido = get_object_or_404(
        Pedido.objects.select_related("sucursal_cliente").prefetch_related("items__producto"),
        pk=pedido_id,
        eliminado=False,
    )
    logger.info("Admin %s descargó Excel de pedido #%s", request.user.username, pedido.id)
    return excel_response_for_pedido(pedido)


@admin_required
def descargar_y_marcar(request, pedido_id):
    pedido = get_object_or_404(
        Pedido.objects.select_related("sucursal_cliente").prefetch_related("items__producto"),
        pk=pedido_id,
        eliminado=False,
    )
    if pedido.estado == Pedido.Estado.CONFIRMADO:
        pedido.estado = Pedido.Estado.ENVIADO
        pedido.save(update_fields=["estado"])
        logger.info("Pedido #%s marcado enviado por descarga de %s", pedido.id, request.user.username)
    return excel_response_for_pedido(pedido)


@dashboard_required
def imprimir_pedido(request, pedido_id):
    pedido = get_object_or_404(
        Pedido.objects.select_related("sucursal_cliente").prefetch_related("items__producto"),
        pk=pedido_id,
        eliminado=False,
    )
    logger.info("Admin %s abrio impresion de pedido #%s", request.user.username, pedido.id)
    context = ticket_context(pedido)
    context["auto_print"] = request.GET.get("embedded") != "1"
    return render(request, "pedidos/ticket_print.html", context)


@require_POST
@admin_required
def marcar_enviado(request, pedido_id):
    pedido = get_object_or_404(Pedido, pk=pedido_id, eliminado=False)
    pedido.estado = Pedido.Estado.ENVIADO
    pedido.save(update_fields=["estado"])
    logger.info("Pedido #%s marcado enviado por %s", pedido.id, request.user.username)
    return redirect(f"{reverse('admin_dashboard')}?estado={Pedido.Estado.ENVIADO}")


@require_POST
@admin_required
def eliminar_pedido(request, pedido_id):
    pedido = get_object_or_404(Pedido, pk=pedido_id, eliminado=False)
    pedido.eliminado = True
    pedido.save(update_fields=["eliminado"])
    logger.info("Pedido #%s eliminado suavemente por %s", pedido.id, request.user.username)
    messages.success(request, f"Pedido {pedido.folio_fecha} eliminado.")
    return redirect("admin_dashboard")
