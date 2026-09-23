import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth import authenticate, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .models import (
    CONFIGURACION_CACHE_KEY,
    MAX_PEDIDOS_POR_DIA,
    Configuracion,
    EventoCliente,
    ItemPedido,
    Pedido,
    PedidoPurgado,
    MacroPedido,
    Precio,
    Producto,
    SucursalCliente,
    SesionActiva,
)
from .sesiones import (
    DEVICE_COOKIE_NAME,
    SESSION_TOKEN_KEY,
    direccion_ip,
    hash_sesion,
    intentar_iniciar_sesion,
    liberar_sesion,
    sesion_esta_vigente,
)
from .tickets import format_ticket_quantity, ticket_context, ticket_context_from_rows

logger = logging.getLogger(__name__)

CONFIGURACION_CACHE_TIMEOUT = 300  # 5 minutos
PRINT_GROUP_NAME = "Operador de impresion"
ADMIN_DASHBOARD_PAGE_SIZE = 50
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
SUCURSALES_REPORTE_BRANCHES = (
    ("Estancia", "ESTANCIA"),
    ("Aguilas", "AGUILAS"),
    ("Eventos MO", "EVENTO MO"),
    ("Fortin", "FORTIN"),
    ("Brot CAT", "BROT CAT"),
    ("Brot Nueva Galicia", "BROT NVA G"),
    ("Rakebela", "RAKEBELA"),
    ("Eventos Edgar", "EDGAR"),
    ("Santa Anita", "STA ANITA"),
    ("Plaza del Sol", "PZA SOL"),
)
SUCURSALES_REPORTE_PRODUCTOS = (
    ("BBQ", "LITRO DE BARBACOA"),
    ("GORDA", "TORTILLA ESPECIAL"),
    ("CONSO", "CONSOMÉ"),
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


@never_cache
def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if can_view_admin_dashboard(request.user):
        return redirect("admin_dashboard")
    return redirect("pedidos")


def privacidad_view(request):
    return render(request, "pedidos/privacidad.html")


@never_cache
@require_http_methods(["GET", "POST"])
def login_view(request):
    """Login compatible con nombres visibles y usernames internos sin espacios."""

    if request.user.is_authenticated:
        return redirect("home")

    context = horario_login_context()
    if request.method == "GET" and request.GET.get("sesion") == "reemplazada":
        messages.error(
            request,
            "Tu sesión se cerró porque la cuenta se inició en otro dispositivo.",
        )

    if request.method == "POST" and request.POST.get("force_takeover"):
        pendiente = request.session.get("pedidos_takeover", {})
        creado_en = pendiente.get("creado_en", 0)
        vigente = timezone.now().timestamp() - creado_en <= 300
        user = User.objects.filter(pk=pendiente.get("user_id")).first() if vigente else None
        if user is not None:
            iniciada, _ = intentar_iniciar_sesion(request, user, forzar=True)
            request.session.pop("pedidos_takeover", None)
            if iniciada:
                messages.info(request, "Se cerró la sesión del otro dispositivo.")
                return redirect("admin_dashboard" if can_view_admin_dashboard(user) else "pedidos")
        messages.error(request, "La autorización para reemplazar la sesión expiró. Ingresa de nuevo.")

    elif request.method == "POST":
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
            iniciada, sesion_conflictiva = intentar_iniciar_sesion(request, user)
            if iniciada:
                request.session.pop("pedidos_takeover", None)
                return redirect("admin_dashboard" if can_view_admin_dashboard(user) else "pedidos")

            request.session["pedidos_takeover"] = {
                "user_id": user.pk,
                "creado_en": timezone.now().timestamp(),
            }
            context.update(
                {
                    "sesion_conflictiva": True,
                    "dispositivo_conflictivo": sesion_conflictiva.dispositivo,
                    "ultima_actividad_conflictiva": sesion_conflictiva.ultima_actividad,
                }
            )
            messages.error(request, "Esta cuenta ya tiene una sesión iniciada en otro dispositivo.")
        else:
            request.session.pop("pedidos_takeover", None)
            messages.error(request, "Usuario o contraseña incorrectos.")

    return render(request, "pedidos/login.html", context)


@never_cache
@require_http_methods(["GET", "POST"])
def logout_view(request):
    liberar_sesion(request)
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
            producto_id=producto.id,
            sucursal_cliente_id=sucursal.id,
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


def progreso_pedidos_diario(sucursal, fecha=None):
    fecha = fecha or timezone.localdate()
    macro = MacroPedido.objects.filter(
        sucursal_cliente=sucursal,
        fecha_pedido=fecha,
        eliminado=False,
    ).first()
    cantidad = macro.cantidad_pedidos if macro else 0
    return {
        "cantidad": cantidad,
        "maximo": MAX_PEDIDOS_POR_DIA,
        "disponibles": max(MAX_PEDIDOS_POR_DIA - cantidad, 0),
        "limite_alcanzado": cantidad >= MAX_PEDIDOS_POR_DIA,
    }


def productos_disponibles_para_pedido(sucursal, pedido=None):
    productos_query = Producto.objects.filter(
        activo=True,
        precios__sucursal_cliente=sucursal,
        precios__fecha_vigencia__lte=timezone.localdate(),
    )
    return list(productos_query.distinct())


def productos_data_para_sucursal(sucursal, productos):
    fecha = timezone.localdate()
    productos = list(productos)
    precios_por_producto = {}
    for precio in (
        Precio.objects.filter(
            producto_id__in=[producto.id for producto in productos],
            sucursal_cliente_id=sucursal.id,
            fecha_vigencia__lte=fecha,
        )
        .only("producto_id", "nombre_ticket", "fecha_vigencia")
        .order_by("producto_id", "-fecha_vigencia")
    ):
        precios_por_producto.setdefault(precio.producto_id, precio)

    productos_data = []
    for producto in productos:
        precio = precios_por_producto.get(producto.id)
        productos_data.append(
            {
                "id": producto.id,
                "nombre": producto.nombre,
                "nombre_ticket": etiqueta_desde_precio(precio, producto)
                if precio
                else producto.etiqueta_ticket,
                "unidad": producto.unidad_corta,
            }
        )
    return productos_data


def pedido_page_context(sucursal, pedido, admin_order_mode=False, sucursales=None):
    productos = productos_disponibles_para_pedido(sucursal, pedido)
    progreso_diario = progreso_pedidos_diario(sucursal)
    api_urls = {
        "crear_item": reverse("api_crear_item"),
        "eliminar_item": reverse("api_eliminar_item"),
        "limpiar_pedido": reverse("api_limpiar_pedido"),
        "confirmar_pedido": reverse("api_confirmar_pedido"),
        "log_cliente": reverse("api_log_cliente"),
        "horarios": reverse("info_horarios"),
    }
    if admin_order_mode:
        api_urls = {
            "crear_item": reverse("admin_api_crear_item"),
            "eliminar_item": reverse("admin_api_eliminar_item"),
            "limpiar_pedido": reverse("admin_api_limpiar_pedido"),
            "confirmar_pedido": reverse("admin_api_confirmar_pedido"),
            "log_cliente": reverse("api_log_cliente"),
            "horarios": reverse("info_horarios"),
        }

    return {
        "sucursal": sucursal,
        "productos": productos,
        "admin_order_mode": admin_order_mode,
        "admin_sucursales": sucursales or [],
        "initial_data": {
            "productos": productos_data_para_sucursal(sucursal, productos),
            "pedido": serializar_pedido(pedido),
            "sucursal_id": sucursal.id,
            "admin_order_mode": admin_order_mode,
            "api_urls": api_urls,
            "progreso_diario": progreso_diario,
            "horario": horario_pedidos_data(aplica=not admin_order_mode),
        },
        "progreso_diario": progreso_diario,
        "segmentos_diarios": [
            {"numero": numero, "activo": numero <= progreso_diario["cantidad"]}
            for numero in range(1, MAX_PEDIDOS_POR_DIA + 1)
        ],
    }


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


def horario_pedidos_data(aplica=True):
    config = get_configuracion()
    ahora_dt = timezone.localtime()
    dentro_horario, mensaje = validar_horario_pedidos()
    return {
        "aplica": aplica,
        "hora_inicio": config.hora_inicio_pedidos.strftime("%H:%M"),
        "hora_fin": config.hora_fin_pedidos.strftime("%H:%M"),
        "hora_actual": ahora_dt.strftime("%H:%M"),
        "dentro_horario": dentro_horario,
        "mensaje": mensaje,
    }


def horario_login_context():
    return {"horario_pedidos": horario_pedidos_data()}


@never_cache
def info_horarios(request):
    """Endpoint público (sin auth) que informa el horario vigente de aceptación
    de pedidos, para que el frontend explique cualquier bloqueo antes de confirmar."""

    return JsonResponse(horario_pedidos_data())


def decimal_to_str(value, places="0.01"):
    return str(Decimal(value).quantize(Decimal(places)))


def parse_filter_date(value):
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    return None


def etiqueta_desde_precio(precio, producto):
    return (precio.nombre_ticket.strip() or producto.etiqueta_ticket)[:24]


def etiqueta_ticket_para_item(item, fecha=None):
    fecha = fecha or timezone.localdate()
    pedido = item.pedido
    precio = (
        Precio.objects.filter(
            producto_id=item.producto_id,
            sucursal_cliente_id=pedido.sucursal_cliente_id,
            fecha_vigencia__lte=fecha,
        )
        .order_by("-fecha_vigencia")
        .first()
    )
    if precio is not None:
        return etiqueta_desde_precio(precio, item.producto)
    return item.producto.etiqueta_ticket


def etiquetas_ticket_para_item_refs(item_refs):
    item_refs = list(item_refs)
    if not item_refs:
        return {}

    producto_ids = {item.producto_id for item, _, _ in item_refs}
    sucursal_ids = {pedido.sucursal_cliente_id for _, pedido, _ in item_refs}
    fecha_maxima = max(fecha for _, _, fecha in item_refs)
    precios_por_clave = defaultdict(list)

    precios = (
        Precio.objects.filter(
            producto_id__in=producto_ids,
            sucursal_cliente_id__in=sucursal_ids,
            fecha_vigencia__lte=fecha_maxima,
        )
        .only("producto_id", "sucursal_cliente_id", "nombre_ticket", "fecha_vigencia")
        .order_by("producto_id", "sucursal_cliente_id", "-fecha_vigencia")
    )
    for precio in precios:
        precios_por_clave[(precio.producto_id, precio.sucursal_cliente_id)].append(precio)

    etiquetas = {}
    for item, pedido, fecha in item_refs:
        precios_item = precios_por_clave.get((item.producto_id, pedido.sucursal_cliente_id), [])
        precio_vigente_item = next(
            (precio for precio in precios_item if precio.fecha_vigencia <= fecha),
            None,
        )
        etiquetas[item.id] = (
            etiqueta_desde_precio(precio_vigente_item, item.producto)
            if precio_vigente_item
            else item.producto.etiqueta_ticket
        )
    return etiquetas


def fecha_referencia_pedido(pedido):
    base = pedido.fecha_confirmacion or pedido.fecha_creacion
    return timezone.localtime(base).date()


def serializar_item(item, incluir_precios=False, nombre_ticket=None, fecha_pedido=None):
    fecha_pedido = fecha_pedido or fecha_referencia_pedido(item.pedido)
    data = {
        "id": item.id,
        "producto_id": item.producto_id,
        "producto": item.producto.nombre,
        "nombre_ticket": nombre_ticket or etiqueta_ticket_para_item(item, fecha_pedido),
        "unidad": item.producto.unidad_corta,
        "cantidad": decimal_to_str(item.cantidad, "0.001"),
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
    items = list(pedido.items.select_related("producto").all())
    fecha_pedido = fecha_referencia_pedido(pedido)
    etiquetas = etiquetas_ticket_para_item_refs(
        (item, pedido, fecha_pedido)
        for item in items
    )
    return {
        "items": [
            serializar_item(
                item,
                incluir_precios=incluir_precios,
                nombre_ticket=etiquetas.get(item.id),
                fecha_pedido=fecha_pedido,
            )
            for item in items
        ],
        "total": decimal_to_str(pedido.total),
    }


def pedidos_historial_usuario(sucursal):
    return (
        Pedido.objects.filter(
            sucursal_cliente=sucursal,
            eliminado=False,
            estado__in=ORDER_HISTORY_STATES,
        )
        .select_related("sucursal_cliente")
        .prefetch_related("items__producto")
        .order_by("-fecha_confirmacion", "-fecha_creacion", "-id")
    )


def historial_pedido_context(pedido):
    fecha_base = pedido.fecha_confirmacion or pedido.fecha_creacion
    items = [
        {
            "producto": item.producto.nombre,
            "cantidad": format_ticket_quantity(item.cantidad),
            "unidad": item.producto.unidad_corta,
        }
        for item in pedido.items.select_related("producto").all()
    ]
    return {
        "pedido": pedido,
        "sucursal": pedido.sucursal_cliente,
        "fecha": timezone.localtime(fecha_base),
        "folio": pedido.folio_fecha,
        "codigo_publico": pedido.codigo_publico,
        "codigo_corto": str(pedido.codigo_publico).split("-")[0].upper(),
        "items": items,
        "total": decimal_to_str(pedido.total),
    }


def segmentos_macropedido(cantidad):
    return [
        {"numero": numero, "activo": numero <= cantidad}
        for numero in range(1, MAX_PEDIDOS_POR_DIA + 1)
    ]


def items_agrupados_pedidos(pedidos, etiquetas_por_item=None):
    etiquetas_por_item = etiquetas_por_item or {}
    agrupados = {}
    for pedido in pedidos:
        for item in pedido.items.all():
            agrupado = agrupados.setdefault(
                item.producto_id,
                {
                    "producto_id": item.producto_id,
                    "producto": item.producto.nombre,
                    "nombre_ticket": etiquetas_por_item.get(item.id)
                    or item.producto.etiqueta_ticket,
                    "unidad": item.producto.unidad_corta,
                    "orden": item.producto.orden,
                    "cantidad_decimal": Decimal("0.000"),
                    "subtotal_decimal": Decimal("0.00"),
                },
            )
            agrupado["cantidad_decimal"] += item.cantidad
            agrupado["subtotal_decimal"] += item.subtotal

    rows = []
    for agrupado in sorted(agrupados.values(), key=lambda row: (row["orden"], row["producto"])):
        rows.append(
            {
                **agrupado,
                "cantidad": format_ticket_quantity(agrupado["cantidad_decimal"]),
                "subtotal": decimal_to_str(agrupado["subtotal_decimal"]),
            }
        )
    return rows


def ticket_context_macropedido(macropedido, items_agrupados):
    return ticket_context_from_rows(
        macropedido,
        [
            {
                "producto": item["nombre_ticket"].upper(),
                "cantidad": f'{item["cantidad"]} {item["unidad"]}'.strip(),
            }
            for item in items_agrupados
        ],
    )


def macropedidos_historial_usuario(sucursal):
    pedidos_activos = (
        Pedido.objects.filter(eliminado=False)
        .exclude(estado=Pedido.Estado.PENDIENTE)
        .select_related("sucursal_cliente")
        .prefetch_related("items__producto")
        .order_by("fecha_confirmacion", "fecha_creacion", "id")
    )
    return (
        MacroPedido.objects.filter(
            sucursal_cliente=sucursal,
            eliminado=False,
        )
        .select_related("sucursal_cliente")
        .prefetch_related(Prefetch("pedidos", queryset=pedidos_activos, to_attr="pedidos_activos"))
        .order_by("-fecha_pedido", "-ultima_confirmacion", "-id")
    )


def macropedido_historial_context(macropedido):
    pedidos = getattr(macropedido, "pedidos_activos", None)
    if pedidos is None:
        pedidos = list(macropedido.pedidos.all())
    else:
        pedidos = list(pedidos)
    items_agrupados = items_agrupados_pedidos(pedidos)
    cantidad = len(pedidos)
    return {
        "pedido": macropedido,
        "sucursal": macropedido.sucursal_cliente,
        "fecha": timezone.localtime(macropedido.ultima_confirmacion),
        "folio": macropedido.folio_fecha,
        "codigo_publico": macropedido.codigo_publico,
        "codigo_corto": str(macropedido.codigo_publico).split("-")[0].upper(),
        "items": items_agrupados,
        "total": decimal_to_str(macropedido.total),
        "cantidad_pedidos": cantidad,
        "segmentos": segmentos_macropedido(cantidad),
        "pedidos": [historial_pedido_context(pedido) for pedido in pedidos],
    }


def parse_json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return None


def normalizar_detalle_evento(detalle):
    if not isinstance(detalle, dict):
        return {}
    normalizado = {}
    for clave, valor in list(detalle.items())[:20]:
        clave_limpia = str(clave)[:50]
        if isinstance(valor, (str, int, float, bool)) or valor is None:
            normalizado[clave_limpia] = str(valor)[:500] if isinstance(valor, str) else valor
        else:
            normalizado[clave_limpia] = str(valor)[:500]
    return normalizado


def ocultar_referencia_pedido_purgado(detalle):
    """Un evento tardío nunca debe reintroducir el detalle de un pedido purgado."""

    pedido_id = detalle.get("pedido_id")
    try:
        pedido_id = int(pedido_id) if pedido_id is not None else None
    except (TypeError, ValueError):
        pedido_id = None
    codigo = detalle.get("codigo_publico") or detalle.get("pedido_uuid")
    try:
        codigo = uuid.UUID(str(codigo)) if codigo else None
    except (TypeError, ValueError, AttributeError):
        codigo = None
    if ((pedido_id is not None and PedidoPurgado.objects.filter(pedido_id_origen=pedido_id).exists())
            or (codigo is not None and PedidoPurgado.objects.filter(codigo_publico=codigo).exists())):
        return {"retencion": "pedido_purgado"}
    return detalle


def parse_fecha_cliente(value):
    fecha = parse_datetime(str(value or ""))
    if fecha is None:
        return None
    if timezone.is_naive(fecha):
        fecha = timezone.make_aware(fecha, timezone.get_current_timezone())
    return fecha


def registrar_evento_persistente(request, evento, detalle=None, payload=None):
    """Guarda evidencia de cliente/servidor sin interrumpir el flujo de pedido."""

    payload = payload if isinstance(payload, dict) else {}
    try:
        evento_id = uuid.UUID(str(payload.get("evento_id"))) if payload.get("evento_id") else uuid.uuid4()
    except (ValueError, TypeError, AttributeError):
        evento_id = uuid.uuid4()

    intento_id = str(
        payload.get("intento_id") or request.headers.get("X-Order-Attempt-ID", "")
    )[:64]
    dispositivo_id = str(
        payload.get("dispositivo_id")
        or request.headers.get("X-Client-Device", "")
        or request.COOKIES.get(DEVICE_COOKIE_NAME, "")
    )[:64]
    user_agent = str(
        payload.get("ua") or request.META.get("HTTP_USER_AGENT", "")
    )[:300]
    sucursal = sucursal_para_usuario(request.user) if request.user.is_authenticated else None
    token = request.session.get(SESSION_TOKEN_KEY, "")

    try:
        detalle_normalizado = normalizar_detalle_evento(detalle or payload.get("detalle") or {})
        with transaction.atomic():
            # Comparte el bloqueo del pedido con aplicar_purga. Un evento que
            # llegue durante la purga espera el COMMIT y verá el tombstone.
            pedido_id = detalle_normalizado.get("pedido_id")
            try:
                pedido_id = int(pedido_id) if pedido_id is not None else None
            except (TypeError, ValueError):
                pedido_id = None
            if pedido_id is not None:
                list(Pedido.objects.select_for_update().filter(pk=pedido_id).values_list("pk", flat=True))
            codigo = detalle_normalizado.get("codigo_publico") or detalle_normalizado.get("pedido_uuid")
            try:
                codigo = uuid.UUID(str(codigo)) if codigo else None
            except (TypeError, ValueError, AttributeError):
                codigo = None
            if codigo is not None:
                list(Pedido.objects.select_for_update().filter(codigo_publico=codigo).values_list("pk", flat=True))
            registro, _ = EventoCliente.objects.get_or_create(
                evento_id=evento_id,
                defaults={
                    "usuario": request.user if request.user.is_authenticated else None,
                    "sucursal_cliente": sucursal,
                    "evento": str(evento or "desconocido")[:80],
                    "intento_id": intento_id,
                    "dispositivo_id": dispositivo_id,
                    "sesion_hash": hash_sesion(token),
                    "ocurrido_en": parse_fecha_cliente(payload.get("ocurrido_en")),
                    "detalle": ocultar_referencia_pedido_purgado(detalle_normalizado),
                    "user_agent": user_agent,
                    "direccion_ip": direccion_ip(request),
                },
            )
        return registro
    except Exception:
        # La auditoría nunca debe impedir capturar o confirmar un pedido.
        logger.exception("No se pudo persistir el evento de auditoría %s", evento)
        return None


def parse_cantidad(value):
    try:
        cantidad = Decimal(str(value)).quantize(Decimal("0.001"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if cantidad <= 0 or cantidad > Decimal("999.999"):
        return None
    return cantidad


def sucursal_desde_payload(payload):
    sucursal_id = payload.get("sucursal_id") if payload else None
    return get_object_or_404(SucursalCliente, pk=sucursal_id, activa=True)


def guardar_item_pedido(sucursal, payload):
    producto = get_object_or_404(Producto, pk=payload.get("producto_id"))
    cantidad = parse_cantidad(payload.get("cantidad"))
    if cantidad is None:
        return JsonResponse({"success": False, "mensaje": "Cantidad inválida."}, status=400)

    precio = precio_vigente(producto, sucursal)
    if precio is None:
        return JsonResponse({"success": False, "mensaje": "No hay precio vigente."}, status=400)

    with transaction.atomic():
        sucursal_bloqueada = SucursalCliente.objects.select_for_update().get(pk=sucursal.pk)
        progreso = progreso_pedidos_diario(sucursal_bloqueada)
        if progreso["limite_alcanzado"]:
            return JsonResponse(
                {
                    "success": False,
                    "mensaje": "Ya se confirmaron los 5 pedidos permitidos para hoy.",
                    "progreso_diario": progreso,
                },
                status=409,
            )

        pedido = pedido_pendiente(sucursal_bloqueada, crear=True)
        item, created = ItemPedido.objects.select_for_update().get_or_create(
            pedido=pedido,
            producto=producto,
            defaults={
                "cantidad": cantidad,
                "precio_unitario": precio.precio_unitario,
            },
        )
        if not created:
            item.cantidad = cantidad
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
            "progreso_diario": progreso,
        }
    )


def eliminar_item_pedido(sucursal, payload):
    with transaction.atomic():
        sucursal_bloqueada = SucursalCliente.objects.select_for_update().get(pk=sucursal.pk)
        pedido = pedido_pendiente(sucursal_bloqueada)
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


def limpiar_pedido_sucursal(sucursal):
    with transaction.atomic():
        sucursal_bloqueada = SucursalCliente.objects.select_for_update().get(pk=sucursal.pk)
        pedido = pedido_pendiente(sucursal_bloqueada)
        if pedido:
            pedido.items.all().delete()
            pedido.recalcular_total()
    return JsonResponse({"success": True, "pedido": {"items": [], "total": "0.00"}})


def confirmar_pedido_sucursal(sucursal, validar_horario=True, validar_espera=True):
    if validar_horario:
        es_valido, mensaje_horario = validar_horario_pedidos()
        if not es_valido:
            logger.warning(
                "Confirmacion rechazada sucursal=%s motivo=fuera_horario hora=%s mensaje=%s",
                sucursal.nombre,
                timezone.localtime().strftime("%H:%M:%S"),
                mensaje_horario,
            )
            return JsonResponse(
                {
                    "success": False,
                    "codigo": "fuera_horario",
                    "mensaje": mensaje_horario,
                    "horario": horario_pedidos_data(),
                },
                status=400,
            )

    fecha_confirmacion = timezone.now()
    fecha_pedido = timezone.localdate(fecha_confirmacion)
    with transaction.atomic():
        sucursal_bloqueada = SucursalCliente.objects.select_for_update().get(pk=sucursal.pk)

        if validar_espera:
            limite = fecha_confirmacion - timedelta(seconds=60)
            if Pedido.objects.filter(
                sucursal_cliente=sucursal_bloqueada,
                fecha_confirmacion__gte=limite,
                estado__in=ORDER_HISTORY_STATES,
                eliminado=False,
            ).exists():
                logger.warning(
                    "Confirmacion rechazada sucursal=%s motivo=espera_minima",
                    sucursal_bloqueada.nombre,
                )
                return JsonResponse(
                    {
                        "success": False,
                        "mensaje": "Espera un minuto antes de confirmar otro pedido.",
                    },
                    status=429,
                )

        pedido = (
            Pedido.objects.select_for_update()
            .filter(
                sucursal_cliente=sucursal_bloqueada,
                estado=Pedido.Estado.PENDIENTE,
                eliminado=False,
            )
            .prefetch_related("items")
            .order_by("-fecha_creacion")
            .first()
        )
        if pedido is None or not pedido.items.exists():
            logger.warning(
                "Confirmacion rechazada sucursal=%s motivo=pedido_vacio",
                sucursal_bloqueada.nombre,
            )
            return JsonResponse(
                {"success": False, "mensaje": "No hay productos en el pedido."},
                status=400,
            )

        macropedido = (
            MacroPedido.objects.select_for_update()
            .filter(
                sucursal_cliente=sucursal_bloqueada,
                fecha_pedido=fecha_pedido,
            )
            .first()
        )
        pedidos_confirmados = macropedido.cantidad_pedidos if macropedido else 0
        if pedidos_confirmados >= MAX_PEDIDOS_POR_DIA:
            logger.warning(
                "Confirmacion rechazada sucursal=%s motivo=limite_diario pedidos=%s",
                sucursal_bloqueada.nombre,
                pedidos_confirmados,
            )
            progreso = {
                "cantidad": pedidos_confirmados,
                "maximo": MAX_PEDIDOS_POR_DIA,
                "disponibles": 0,
                "limite_alcanzado": True,
            }
            return JsonResponse(
                {
                    "success": False,
                    "mensaje": "Ya se confirmaron los 5 pedidos permitidos para hoy.",
                    "progreso_diario": progreso,
                },
                status=409,
            )

        if macropedido is None:
            macropedido = MacroPedido.objects.create(
                sucursal_cliente=sucursal_bloqueada,
                fecha_pedido=fecha_pedido,
                ultima_confirmacion=fecha_confirmacion,
            )

        pedido.recalcular_total()
        pedido.estado = Pedido.Estado.CONFIRMADO
        pedido.fecha_confirmacion = fecha_confirmacion
        pedido.macropedido = macropedido
        pedido.save(update_fields=["estado", "fecha_confirmacion", "macropedido"])

        macropedido.eliminado = False
        macropedido.estado = MacroPedido.Estado.CONFIRMADO
        macropedido.ultima_confirmacion = fecha_confirmacion
        macropedido.save(
            update_fields=[
                "eliminado",
                "estado",
                "ultima_confirmacion",
                "fecha_actualizacion",
            ]
        )
        macropedido.pedidos.filter(eliminado=False).exclude(
            estado=Pedido.Estado.PENDIENTE
        ).update(estado=Pedido.Estado.CONFIRMADO)
        macropedido.recalcular_resumen()
        pedidos_confirmados += 1

    logger.info("Pedido confirmado #%s por %s", pedido.id, sucursal.nombre)
    return JsonResponse(
        {
            "success": True,
            "pedido_id": pedido.id,
            "pedido_folio": pedido.folio_fecha,
            "macropedido_id": macropedido.id,
            "macropedido_folio": macropedido.folio_fecha,
            "pedidos_del_dia": pedidos_confirmados,
            "max_pedidos_dia": MAX_PEDIDOS_POR_DIA,
            "progreso_diario": {
                "cantidad": pedidos_confirmados,
                "maximo": MAX_PEDIDOS_POR_DIA,
                "disponibles": MAX_PEDIDOS_POR_DIA - pedidos_confirmados,
                "limite_alcanzado": pedidos_confirmados >= MAX_PEDIDOS_POR_DIA,
            },
            "total": decimal_to_str(pedido.total),
            "mensaje": (
                f"Pedido {pedidos_confirmados} de {MAX_PEDIDOS_POR_DIA} agregado al "
                f"macropedido {macropedido.folio_dia}."
            ),
        }
    )


@never_cache
@login_required
def pedidos_view(request):
    if can_view_admin_dashboard(request.user):
        return redirect("admin_dashboard")

    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        messages.error(request, "Tu usuario no tiene una sucursal o cliente activo.")
        return redirect("login")

    pedido = pedido_pendiente(sucursal)
    return render(request, "pedidos/pedidos.html", pedido_page_context(sucursal, pedido))


@never_cache
@admin_required
def admin_crear_pedido_view(request):
    sucursales = list(SucursalCliente.objects.filter(activa=True).order_by("tipo", "nombre"))
    if not sucursales:
        messages.error(request, "No hay sucursales o clientes activos para levantar pedidos.")
        return redirect("admin_dashboard")

    sucursal_id = request.GET.get("sucursal", "").strip()
    selected = next((item for item in sucursales if str(item.id) == sucursal_id), sucursales[0])
    pedido = pedido_pendiente(selected)
    context = pedido_page_context(
        selected,
        pedido,
        admin_order_mode=True,
        sucursales=sucursales,
    )
    return render(request, "pedidos/pedidos.html", context)


@never_cache
@login_required
def historial_pedidos(request):
    if can_view_admin_dashboard(request.user):
        return redirect("admin_dashboard")

    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        messages.error(request, "Tu usuario no tiene una sucursal o cliente activo.")
        return redirect("login")

    macropedidos = list(macropedidos_historial_usuario(sucursal))
    context = {
        "sucursal": sucursal,
        "history_macros": [
            macropedido_historial_context(macropedido) for macropedido in macropedidos
        ],
    }
    return render(request, "pedidos/historial_pedidos.html", context)


@never_cache
@login_required
def imprimir_historial_pedido(request, codigo_publico):
    if can_view_admin_dashboard(request.user):
        return redirect("admin_dashboard")

    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        messages.error(request, "Tu usuario no tiene una sucursal o cliente activo.")
        return redirect("login")

    pedido = get_object_or_404(pedidos_historial_usuario(sucursal), codigo_publico=codigo_publico)
    context = {
        "order": historial_pedido_context(pedido),
        "pedido": pedido,
        "auto_print": request.GET.get("embedded") != "1",
    }
    return render(request, "pedidos/historial_pedido_print.html", context)


@never_cache
@login_required
def imprimir_historial_macropedido(request, codigo_publico):
    if can_view_admin_dashboard(request.user):
        return redirect("admin_dashboard")

    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        messages.error(request, "Tu usuario no tiene una sucursal o cliente activo.")
        return redirect("login")

    macropedido = get_object_or_404(
        macropedidos_historial_usuario(sucursal),
        codigo_publico=codigo_publico,
    )
    context = {
        "order": macropedido_historial_context(macropedido),
        "pedido": macropedido,
        "auto_print": request.GET.get("embedded") != "1",
    }
    return render(request, "pedidos/historial_pedido_print.html", context)


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

    return guardar_item_pedido(sucursal, payload)


@require_POST
@login_required
def eliminar_item(request):
    sucursal = sucursal_para_usuario(request.user)
    payload = parse_json_body(request)
    if sucursal is None or payload is None:
        return JsonResponse({"success": False, "mensaje": "Solicitud inválida."}, status=400)

    return eliminar_item_pedido(sucursal, payload)


@require_POST
@login_required
def limpiar_pedido(request):
    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        return JsonResponse({"success": False, "mensaje": "Usuario sin sucursal activa."}, status=403)
    return limpiar_pedido_sucursal(sucursal)


@require_POST
@login_required
def confirmar_pedido(request):
    if is_admin_user(request.user):
        return JsonResponse({"success": False, "mensaje": "Admin no puede confirmar pedidos."}, status=403)

    sucursal = sucursal_para_usuario(request.user)
    if sucursal is None:
        return JsonResponse({"success": False, "mensaje": "Usuario sin sucursal activa."}, status=403)

    intento_id = str(request.headers.get("X-Order-Attempt-ID") or uuid.uuid4())[:64]
    pedido = pedido_pendiente(sucursal)
    registrar_evento_persistente(
        request,
        "confirmar_solicitud_servidor",
        {
            "pedido_id": pedido.pk if pedido else None,
            "items": pedido.items.count() if pedido else 0,
        },
        {"intento_id": intento_id},
    )
    try:
        response = confirmar_pedido_sucursal(sucursal)
    except Exception:
        registrar_evento_persistente(
            request,
            "confirmar_error_servidor",
            {"tipo": "excepcion_no_controlada"},
            {"intento_id": intento_id},
        )
        raise

    try:
        respuesta = json.loads(response.content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        respuesta = {}
    exito = response.status_code < 400 and respuesta.get("success") is True
    registrar_evento_persistente(
        request,
        "confirmar_exito_servidor" if exito else "confirmar_rechazo_servidor",
        {
            "http_status": response.status_code,
            "codigo": respuesta.get("codigo", ""),
            "mensaje": respuesta.get("mensaje", ""),
            "pedido_id": respuesta.get("pedido_id"),
        },
        {"intento_id": intento_id},
    )
    response["X-Order-Attempt-ID"] = intento_id
    return response


@require_POST
@login_required
def log_cliente(request):
    """Persiste una cola idempotente de eventos del navegador."""
    if len(request.body) > 64 * 1024:
        return JsonResponse({"success": False, "mensaje": "Bitácora demasiado grande."}, status=413)

    payload = parse_json_body(request) or {}
    if not isinstance(payload, dict):
        payload = {}

    eventos = payload.get("eventos")
    if not isinstance(eventos, list):
        detalle_legacy = {
            clave: valor
            for clave, valor in payload.items()
            if clave not in ("evento", "evento_id", "ua", "ocurrido_en")
        }
        payload["detalle"] = payload.get("detalle") or detalle_legacy
        eventos = [payload]

    guardados = 0
    fallidos = 0
    for entrada in eventos[:50]:
        if not isinstance(entrada, dict):
            continue
        evento = str(entrada.get("evento") or "desconocido")[:80]
        registro = registrar_evento_persistente(request, evento, payload=entrada)
        if registro is not None:
            guardados += 1
        else:
            fallidos += 1
        logger.info(
            "cliente-evento usuario=%s evento=%s intento=%s dispositivo=%s",
            request.user.username,
            evento,
            str(entrada.get("intento_id") or "")[:64],
            str(entrada.get("dispositivo_id") or "")[:64],
        )
    if fallidos:
        return JsonResponse(
            {
                "success": False,
                "mensaje": "No se pudo guardar toda la bitácora; se puede reintentar.",
                "guardados": guardados,
            },
            status=503,
        )
    return JsonResponse({"success": True, "guardados": guardados})


@require_POST
@login_required
def heartbeat_sesion(request):
    """La validación/renovación real ocurre en SesionUnicaMiddleware."""
    return JsonResponse({"success": True})


@require_POST
@admin_required
def admin_crear_item(request):
    payload = parse_json_body(request)
    if payload is None:
        return JsonResponse({"success": False, "mensaje": "JSON inválido."}, status=400)
    return guardar_item_pedido(sucursal_desde_payload(payload), payload)


@require_POST
@admin_required
def admin_eliminar_item(request):
    payload = parse_json_body(request)
    if payload is None:
        return JsonResponse({"success": False, "mensaje": "Solicitud inválida."}, status=400)
    return eliminar_item_pedido(sucursal_desde_payload(payload), payload)


@require_POST
@admin_required
def admin_limpiar_pedido(request):
    payload = parse_json_body(request)
    if payload is None:
        return JsonResponse({"success": False, "mensaje": "Solicitud inválida."}, status=400)
    return limpiar_pedido_sucursal(sucursal_desde_payload(payload))


@require_POST
@admin_required
def admin_confirmar_pedido(request):
    payload = parse_json_body(request)
    if payload is None:
        return JsonResponse({"success": False, "mensaje": "Solicitud inválida."}, status=400)
    return confirmar_pedido_sucursal(
        sucursal_desde_payload(payload),
        validar_horario=False,
        validar_espera=False,
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


@never_cache
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


def latest_report_macros(branch_names, generated_at=None):
    generated_at = generated_at or timezone.now()
    cutoff = generated_at - timedelta(hours=24)
    macropedidos = (
        MacroPedido.objects.filter(
            eliminado=False,
            estado=MacroPedido.Estado.CONFIRMADO,
            ultima_confirmacion__gte=cutoff,
            ultima_confirmacion__lte=generated_at,
            sucursal_cliente__nombre__in=branch_names,
        )
        .select_related("sucursal_cliente")
        .prefetch_related(
            Prefetch(
                "pedidos",
                queryset=pedidos_activos_para_macropedido_queryset(),
                to_attr="pedidos_activos",
            )
        )
        .order_by("sucursal_cliente__nombre", "-ultima_confirmacion", "-id")
    )

    latest_macros = {}
    for macropedido in macropedidos:
        branch_name = macropedido.sucursal_cliente.nombre
        if branch_name not in latest_macros:
            latest_macros[branch_name] = macropedido

    return latest_macros, generated_at, cutoff


def aguas_print_context():
    branch_names = [name for name, _ in AGUAS_SUCURSALES]
    product_names = [name for _, name in AGUAS_PRODUCTOS]
    totals = defaultdict(Decimal)
    latest_macros, generated_at, cutoff = latest_report_macros(branch_names)

    for branch_name, macropedido in latest_macros.items():
        for pedido in macropedido.pedidos_activos:
            for item in pedido.items.all():
                product_name = item.producto.nombre
                if product_name in product_names:
                    totals[(branch_name, product_name)] += item.cantidad

    rows = []
    for label, product_name in AGUAS_PRODUCTOS:
        values = [
            format_dashboard_quantity(totals[(branch_name, product_name)])
            for branch_name, _ in AGUAS_SUCURSALES
        ]
        total = sum(totals[(branch_name, product_name)] for branch_name, _ in AGUAS_SUCURSALES)
        rows.append(
            {
                "label": label,
                "values": values,
                "total": format_dashboard_quantity(total),
            }
        )

    return {
        "source_date": timezone.localtime(cutoff).date(),
        "source_since": cutoff,
        "print_date": timezone.localtime(generated_at).date(),
        "branches": [short_name for _, short_name in AGUAS_SUCURSALES],
        "rows": rows,
    }


def sucursales_print_context():
    branch_names = [name for name, _ in SUCURSALES_REPORTE_BRANCHES]
    product_names = [name for _, name in SUCURSALES_REPORTE_PRODUCTOS]
    totals = defaultdict(Decimal)
    latest_macros, generated_at, cutoff = latest_report_macros(branch_names)

    for branch_name, macropedido in latest_macros.items():
        for pedido in macropedido.pedidos_activos:
            for item in pedido.items.all():
                product_name = item.producto.nombre
                if product_name in product_names:
                    totals[(branch_name, product_name)] += item.cantidad

    rows = []
    for branch_name, short_name in SUCURSALES_REPORTE_BRANCHES:
        rows.append(
            {
                "label": short_name,
                "values": [
                    format_dashboard_quantity(totals[(branch_name, product_name)])
                    for _, product_name in SUCURSALES_REPORTE_PRODUCTOS
                ],
            }
        )

    total_values = []
    for _, product_name in SUCURSALES_REPORTE_PRODUCTOS:
        product_total = sum(
            totals[(branch_name, product_name)]
            for branch_name, _ in SUCURSALES_REPORTE_BRANCHES
        )
        total_values.append(format_dashboard_quantity(product_total))

    return {
        "source_date": timezone.localtime(cutoff).date(),
        "source_since": cutoff,
        "print_date": timezone.localtime(generated_at).date(),
        "headers": [label for label, _ in SUCURSALES_REPORTE_PRODUCTOS],
        "rows": rows,
        "total_values": total_values,
    }


def pedido_history_queryset():
    return (
        MacroPedido.objects.filter(eliminado=False)
        .select_related("sucursal_cliente")
        .prefetch_related(
            Prefetch(
                "pedidos",
                queryset=pedidos_activos_para_macropedido_queryset(),
                to_attr="pedidos_activos",
            )
        )
        .order_by("fecha_pedido", "ultima_confirmacion")
    )


def pedido_local_date(pedido):
    if hasattr(pedido, "fecha_pedido"):
        return pedido.fecha_pedido
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
        items = items_agrupados_pedidos(pedido.pedidos_activos)
        item_count = len(items)
        units = sum((item["cantidad_decimal"] for item in items), Decimal("0.000"))
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
                mix = product_mix[item["producto"]]
                mix["cantidad"] += item["cantidad_decimal"]
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
        "sucursales_print": sucursales_print_context(),
    }


def pedidos_activos_para_macropedido_queryset():
    return (
        Pedido.objects.filter(eliminado=False)
        .exclude(estado=Pedido.Estado.PENDIENTE)
        .select_related("sucursal_cliente")
        .prefetch_related("items__producto")
        .order_by("fecha_confirmacion", "fecha_creacion", "id")
    )


def preparar_macropedidos_para_interfaz(macropedidos):
    macropedidos = list(macropedidos)
    item_refs = []
    for macropedido in macropedidos:
        for pedido in macropedido.pedidos_activos:
            fecha_pedido = fecha_referencia_pedido(pedido)
            for item in pedido.items.all():
                item_refs.append((item, pedido, fecha_pedido))

    etiquetas_por_item = etiquetas_ticket_para_item_refs(item_refs)
    inline_print_orders = []
    for macropedido in macropedidos:
        pedidos = list(macropedido.pedidos_activos)
        for indice, pedido in enumerate(pedidos, start=1):
            pedido_items = list(pedido.items.all())
            fecha_pedido = fecha_referencia_pedido(pedido)
            pedido.detalle_items = [
                serializar_item(
                    item,
                    incluir_precios=True,
                    nombre_ticket=etiquetas_por_item.get(item.id),
                    fecha_pedido=fecha_pedido,
                )
                for item in pedido_items
            ]
            pedido.items_count = len(pedido.detalle_items)
            pedido.numero_en_dia = indice
            pedido.hora_confirmacion = timezone.localtime(
                pedido.fecha_confirmacion or pedido.fecha_creacion
            )
            pedido.print_context = ticket_context(
                pedido,
                items=pedido_items,
                etiquetas_por_item=etiquetas_por_item,
            )
            pedido.print_template_id = f"print-pedido-{pedido.id}"
            inline_print_orders.append(pedido)

        macropedido.items_agrupados = items_agrupados_pedidos(pedidos, etiquetas_por_item)
        macropedido.pedidos_count = len(pedidos)
        macropedido.segmentos = segmentos_macropedido(len(pedidos))
        macropedido.items_count = len(macropedido.items_agrupados)
        macropedido.print_context = ticket_context_macropedido(
            macropedido,
            macropedido.items_agrupados,
        )
        macropedido.print_template_id = f"print-macro-{macropedido.id}"
        inline_print_orders.append(macropedido)

    return macropedidos, inline_print_orders


def preparar_pedidos_en_curso_para_interfaz():
    pedidos = list(
        Pedido.objects.filter(
            estado=Pedido.Estado.PENDIENTE,
            eliminado=False,
            items__isnull=False,
        )
        .select_related("sucursal_cliente")
        .prefetch_related("items__producto")
        .distinct()
        .order_by("-fecha_creacion", "-id")
    )
    for pedido in pedidos:
        items = list(pedido.items.all())
        pedido.detalle_items = [
            {
                "producto": item.producto.nombre,
                "cantidad": decimal_to_str(item.cantidad, "0.001"),
                "unidad": item.producto.unidad_corta,
            }
            for item in items
        ]
        pedido.items_count = len(items)
        pedido.fecha_creacion_local = timezone.localtime(pedido.fecha_creacion)
    return pedidos


@never_cache
@admin_required
def admin_diagnostico(request):
    sucursal_id = request.GET.get("sucursal", "").strip()
    evento_nombre = request.GET.get("evento", "").strip()
    eventos = EventoCliente.objects.select_related("usuario", "sucursal_cliente")
    if sucursal_id:
        eventos = eventos.filter(sucursal_cliente_id=sucursal_id)
    if evento_nombre:
        eventos = eventos.filter(evento=evento_nombre)

    sesiones = list(
        SesionActiva.objects.select_related("usuario").order_by("usuario__username")
    )
    for sesion in sesiones:
        sesion.vigente = sesion_esta_vigente(sesion)

    context = {
        "eventos": list(eventos[:250]),
        "sesiones": sesiones,
        "sucursales": SucursalCliente.objects.filter(activa=True).order_by("nombre"),
        "tipos_evento": EventoCliente.objects.order_by("evento")
        .values_list("evento", flat=True)
        .distinct(),
        "filters": {"sucursal": sucursal_id, "evento": evento_nombre},
    }
    return render(request, "pedidos/admin_diagnostico.html", context)


@never_cache
@dashboard_required
def admin_dashboard(request):
    macropedidos = (
        MacroPedido.objects.filter(eliminado=False)
        .select_related("sucursal_cliente")
        .prefetch_related(
            Prefetch(
                "pedidos",
                queryset=pedidos_activos_para_macropedido_queryset(),
                to_attr="pedidos_activos",
            )
        )
        .order_by("-fecha_pedido", "-ultima_confirmacion", "-id")
    )

    estado = request.GET.get("estado", "").strip()
    sucursal_id = request.GET.get("sucursal", "").strip()
    desde = request.GET.get("desde", "").strip()
    hasta = request.GET.get("hasta", "").strip()
    q = request.GET.get("q", "").strip()

    if estado:
        macropedidos = macropedidos.filter(estado=estado)
    if sucursal_id:
        macropedidos = macropedidos.filter(sucursal_cliente_id=sucursal_id)
    if desde:
        macropedidos = macropedidos.filter(fecha_pedido__gte=desde)
    if hasta:
        macropedidos = macropedidos.filter(fecha_pedido__lte=hasta)
    if q:
        filtro = Q(sucursal_cliente__nombre__icontains=q) | Q(
            pedidos__usuario_nombre__icontains=q
        )
        fecha_busqueda = parse_filter_date(q)
        if fecha_busqueda:
            filtro |= Q(fecha_pedido=fecha_busqueda)
        macropedidos = macropedidos.filter(filtro).distinct()

    paginator = Paginator(macropedidos, ADMIN_DASHBOARD_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    macropedidos_list, inline_print_orders = preparar_macropedidos_para_interfaz(
        page_obj.object_list
    )
    pedidos_en_curso = preparar_pedidos_en_curso_para_interfaz()

    pagination_query = request.GET.copy()
    pagination_query.pop("page", None)

    hoy = timezone.localdate()
    stats_base = MacroPedido.objects.filter(eliminado=False)
    stats = {
        "pendientes": stats_base.filter(estado=MacroPedido.Estado.CONFIRMADO).count(),
        "en_curso": len(pedidos_en_curso),
        "total_hoy": stats_base.filter(fecha_pedido=hoy).aggregate(total=Sum("total"))["total"]
        or Decimal("0.00"),
        "pedidos_hoy": stats_base.filter(fecha_pedido=hoy).count(),
    }

    context = {
        "macropedidos": macropedidos_list,
        "pedidos_en_curso": pedidos_en_curso,
        "inline_print_orders": inline_print_orders,
        "page_obj": page_obj,
        "pagination_query": pagination_query.urlencode(),
        "page_size": ADMIN_DASHBOARD_PAGE_SIZE,
        "sucursales": SucursalCliente.objects.filter(activa=True),
        "estados": MacroPedido.Estado.choices,
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
        "sucursales_print": sucursales_print_context(),
    }
    return render(request, "pedidos/admin_dashboard.html", context)


@never_cache
@dashboard_required
def imprimir_aguas(request):
    logger.info("Admin %s abrio impresion de aguas", request.user.username)
    context = aguas_print_context()
    context["auto_print"] = request.GET.get("embedded") != "1"
    return render(request, "pedidos/aguas_print.html", context)


@never_cache
@dashboard_required
def imprimir_sucursales(request):
    logger.info("Admin %s abrio impresion de reporte sucursales", request.user.username)
    context = sucursales_print_context()
    context["auto_print"] = request.GET.get("embedded") != "1"
    return render(request, "pedidos/sucursales_print.html", context)


@never_cache
@admin_required
def admin_datos(request):
    return render(request, "pedidos/admin_datos.html", admin_datos_context(request))


@never_cache
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


@never_cache
@dashboard_required
def imprimir_macropedido(request, macropedido_id):
    macropedido = get_object_or_404(
        MacroPedido.objects.filter(eliminado=False)
        .select_related("sucursal_cliente")
        .prefetch_related(
            Prefetch(
                "pedidos",
                queryset=pedidos_activos_para_macropedido_queryset(),
                to_attr="pedidos_activos",
            )
        ),
        pk=macropedido_id,
    )
    macropedidos, _ = preparar_macropedidos_para_interfaz([macropedido])
    logger.info(
        "Admin %s abrio impresion de macropedido #%s",
        request.user.username,
        macropedido.id,
    )
    context = macropedidos[0].print_context
    context["auto_print"] = request.GET.get("embedded") != "1"
    return render(request, "pedidos/ticket_print.html", context)


def cambiar_estado_macropedido(macropedido_id, estado_actual, estado_nuevo):
    with transaction.atomic():
        macropedido = get_object_or_404(
            MacroPedido.objects.select_for_update(),
            pk=macropedido_id,
            eliminado=False,
        )
        if macropedido.estado != estado_actual:
            return macropedido, False
        macropedido.estado = estado_nuevo
        macropedido.save(update_fields=["estado", "fecha_actualizacion"])
        macropedido.pedidos.filter(eliminado=False).exclude(
            estado=Pedido.Estado.PENDIENTE
        ).update(estado=estado_nuevo)
    return macropedido, True


@require_POST
@admin_required
def marcar_macropedido_enviado(request, macropedido_id):
    macropedido, actualizado = cambiar_estado_macropedido(
        macropedido_id,
        MacroPedido.Estado.CONFIRMADO,
        MacroPedido.Estado.ENVIADO,
    )
    if actualizado:
        logger.info(
            "Macropedido #%s marcado enviado por %s",
            macropedido.id,
            request.user.username,
        )
        messages.success(request, f"Macropedido {macropedido.folio_fecha} marcado como enviado.")
    else:
        messages.error(request, "Sólo se pueden enviar macropedidos confirmados.")
    return redirect(f"{reverse('admin_dashboard')}?estado={MacroPedido.Estado.ENVIADO}")


@require_POST
@admin_required
def revertir_macropedido_enviado(request, macropedido_id):
    macropedido, actualizado = cambiar_estado_macropedido(
        macropedido_id,
        MacroPedido.Estado.ENVIADO,
        MacroPedido.Estado.CONFIRMADO,
    )
    if actualizado:
        logger.info(
            "Macropedido #%s devuelto a confirmado por %s",
            macropedido.id,
            request.user.username,
        )
        messages.success(request, f"Macropedido {macropedido.folio_fecha} devuelto a confirmado.")
    else:
        messages.error(request, "Sólo se pueden revertir macropedidos enviados.")
    return redirect(f"{reverse('admin_dashboard')}?estado={MacroPedido.Estado.CONFIRMADO}")


@require_POST
@admin_required
def eliminar_macropedido(request, macropedido_id):
    with transaction.atomic():
        macropedido = get_object_or_404(
            MacroPedido.objects.select_for_update(),
            pk=macropedido_id,
            eliminado=False,
        )
        macropedido.eliminado = True
        macropedido.save(update_fields=["eliminado", "fecha_actualizacion"])
        macropedido.pedidos.filter(eliminado=False).update(eliminado=True)
    logger.info("Macropedido #%s eliminado suavemente por %s", macropedido.id, request.user.username)
    messages.success(request, f"Macropedido {macropedido.folio_fecha} eliminado.")
    return redirect("admin_dashboard")


@require_POST
@admin_required
def marcar_enviado(request, pedido_id):
    pedido = get_object_or_404(Pedido, pk=pedido_id, eliminado=False)
    if pedido.macropedido_id:
        macropedido, actualizado = cambiar_estado_macropedido(
            pedido.macropedido_id,
            MacroPedido.Estado.CONFIRMADO,
            MacroPedido.Estado.ENVIADO,
        )
        if actualizado:
            messages.success(
                request,
                f"Macropedido {macropedido.folio_fecha} marcado como enviado.",
            )
        else:
            messages.error(request, "Sólo se pueden enviar macropedidos confirmados.")
        return redirect(f"{reverse('admin_dashboard')}?estado={MacroPedido.Estado.ENVIADO}")
    if pedido.estado == Pedido.Estado.CONFIRMADO:
        pedido.estado = Pedido.Estado.ENVIADO
        pedido.save(update_fields=["estado"])
        logger.info("Pedido #%s marcado enviado por %s", pedido.id, request.user.username)
        messages.success(request, f"Pedido {pedido.folio_fecha} marcado como enviado.")
    else:
        messages.error(request, "Sólo se pueden marcar como enviados los pedidos confirmados.")
    return redirect(f"{reverse('admin_dashboard')}?estado={Pedido.Estado.ENVIADO}")


@require_POST
@admin_required
def revertir_enviado(request, pedido_id):
    pedido = get_object_or_404(Pedido, pk=pedido_id, eliminado=False)
    if pedido.macropedido_id:
        macropedido, actualizado = cambiar_estado_macropedido(
            pedido.macropedido_id,
            MacroPedido.Estado.ENVIADO,
            MacroPedido.Estado.CONFIRMADO,
        )
        if actualizado:
            messages.success(request, f"Macropedido {macropedido.folio_fecha} devuelto a confirmado.")
        else:
            messages.error(request, "Sólo se pueden revertir macropedidos enviados.")
        return redirect(f"{reverse('admin_dashboard')}?estado={MacroPedido.Estado.CONFIRMADO}")
    if pedido.estado == Pedido.Estado.ENVIADO:
        pedido.estado = Pedido.Estado.CONFIRMADO
        pedido.save(update_fields=["estado"])
        logger.info("Pedido #%s devuelto a confirmado por %s", pedido.id, request.user.username)
        messages.success(request, f"Pedido {pedido.folio_fecha} devuelto a confirmado.")
    else:
        messages.error(request, "Sólo se pueden revertir pedidos que estén enviados.")
    return redirect(f"{reverse('admin_dashboard')}?estado={Pedido.Estado.CONFIRMADO}")


@require_POST
@admin_required
def eliminar_pedido(request, pedido_id):
    pedido = get_object_or_404(Pedido, pk=pedido_id, eliminado=False)
    pedido.eliminado = True
    pedido.save(update_fields=["eliminado"])
    if pedido.macropedido_id:
        macropedido = pedido.macropedido
        restantes = macropedido.pedidos.filter(eliminado=False).exclude(
            estado=Pedido.Estado.PENDIENTE
        )
        if restantes.exists():
            macropedido.recalcular_resumen()
        else:
            macropedido.eliminado = True
            macropedido.total = Decimal("0.00")
            macropedido.save(
                update_fields=["eliminado", "total", "fecha_actualizacion"]
            )
    logger.info("Pedido #%s eliminado suavemente por %s", pedido.id, request.user.username)
    messages.success(request, f"Pedido {pedido.folio_fecha} eliminado.")
    return redirect("admin_dashboard")
