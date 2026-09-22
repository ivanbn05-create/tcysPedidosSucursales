"""API de solo lectura para sincronizar pedidos confirmados hacia el POS."""

import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db.models import Prefetch, Q
from django.http import JsonResponse
from django.utils.dateparse import parse_datetime

from .models import ItemPedido, Pedido, SucursalCliente


logger = logging.getLogger(__name__)

CURSOR_SALT = "pedidos.api_pos.v1.cursor"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _request_id(request):
    supplied = request.headers.get("X-Request-ID", "").strip()
    if REQUEST_ID_PATTERN.fullmatch(supplied):
        return supplied
    return str(uuid.uuid4())


def _json_response(payload, *, status, request_id, headers=None):
    response = JsonResponse(
        payload,
        status=status,
        json_dumps_params={"ensure_ascii": False, "separators": (",", ":")},
    )
    response["Cache-Control"] = "no-store"
    response["X-Request-ID"] = request_id
    for name, value in (headers or {}).items():
        response[name] = str(value)
    return response


def _error(code, message, *, status, request_id, headers=None):
    return _json_response(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        },
        status=status,
        request_id=request_id,
        headers=headers,
    )


def _configured_tokens():
    return tuple(getattr(settings, "POS_API_TOKENS", ()))


def _authenticate(request):
    tokens = _configured_tokens()
    if not tokens:
        return None, "service_not_configured"

    authorization = request.headers.get("Authorization", "")
    if len(authorization) > 1024:
        return None, "unauthorized"
    scheme, separator, candidate = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not candidate:
        return None, "unauthorized"

    matched = False
    for configured in tokens:
        matched |= secrets.compare_digest(candidate, configured)
    if not matched:
        return None, "unauthorized"
    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:24], None


def _rate_limit(token_fingerprint):
    limit = settings.POS_API_RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return None, {}

    now = time.time()
    bucket = int(now // 60)
    key = f"pos-api:v1:{token_fingerprint}:{bucket}"
    if cache.add(key, 1, timeout=70):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=70)
            count = 1

    remaining = max(0, limit - count)
    headers = {
        "X-RateLimit-Limit": limit,
        "X-RateLimit-Remaining": remaining,
    }
    if count > limit:
        headers["Retry-After"] = max(1, 60 - int(now % 60))
        return "rate_limited", headers
    return None, headers


def _iso_utc(value):
    return value.astimezone(datetime_timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _decimal_string(value):
    return format(value, "f")


def _parse_aware_datetime(raw_value, parameter):
    value = parse_datetime(raw_value or "")
    if value is None or value.tzinfo is None:
        raise ValueError(
            f"{parameter} debe ser un timestamp ISO 8601 con zona horaria."
        )
    return value


def _parse_limit(raw_value):
    if raw_value in (None, ""):
        return settings.POS_API_DEFAULT_PAGE_SIZE
    try:
        limit = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limite debe ser un entero positivo.") from exc
    if limit < 1 or limit > settings.POS_API_MAX_PAGE_SIZE:
        raise ValueError(
            f"limite debe estar entre 1 y {settings.POS_API_MAX_PAGE_SIZE}."
        )
    return limit


def _parse_sucursal_ids(raw_value):
    allowed = set(settings.POS_API_ALLOWED_SUCURSAL_IDS)
    if not allowed:
        raise RuntimeError("service_not_configured")
    if raw_value in (None, ""):
        return tuple(sorted(allowed))
    try:
        requested = {int(part.strip()) for part in raw_value.split(",") if part.strip()}
    except ValueError as exc:
        raise ValueError("sucursal_id debe contener IDs enteros separados por coma.") from exc
    if not requested:
        raise ValueError("sucursal_id no puede estar vacio.")
    if any(value <= 0 for value in requested) or not requested.issubset(allowed):
        raise ValueError("sucursal_id contiene un identificador no permitido.")
    return tuple(sorted(requested))


def _filter_fingerprint(desde, hasta, sucursal_ids):
    canonical = json.dumps(
        {
            "desde": _iso_utc(desde),
            "hasta": _iso_utc(hasta),
            "sucursal_ids": list(sucursal_ids),
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_cursor(pedido, fingerprint):
    return signing.dumps(
        {
            "f": fingerprint,
            "id": pedido.id,
            "ts": _iso_utc(pedido.fecha_confirmacion),
            "v": 1,
        },
        salt=CURSOR_SALT,
        compress=True,
    )


def _decode_cursor(raw_cursor, fingerprint, desde, hasta):
    if not raw_cursor:
        return None
    if len(raw_cursor) > 2048:
        raise ValueError("cursor invalido.")
    try:
        payload = signing.loads(raw_cursor, salt=CURSOR_SALT)
        cursor_id = int(payload["id"])
        cursor_timestamp = _parse_aware_datetime(payload["ts"], "cursor")
        valid = (
            payload.get("v") == 1
            and payload.get("f") == fingerprint
            and cursor_id > 0
            and desde <= cursor_timestamp < hasta
        )
    except (AttributeError, KeyError, TypeError, ValueError, signing.BadSignature) as exc:
        raise ValueError("cursor invalido.") from exc
    if not valid:
        raise ValueError("cursor invalido para los filtros solicitados.")
    return cursor_timestamp, cursor_id


def _serialize_pedido(pedido):
    return {
        "id": pedido.id,
        "fecha_confirmacion": _iso_utc(pedido.fecha_confirmacion),
        "total": _decimal_string(pedido.total),
        "sucursal": {
            "id": pedido.sucursal_cliente_id,
            "nombre": pedido.sucursal_cliente.nombre,
            "tipo": pedido.sucursal_cliente.tipo,
        },
        "items": [
            {
                "id": item.id,
                "pedido_id": pedido.id,
                "producto": {
                    "id": item.producto_id,
                    "nombre": item.producto.nombre,
                    "nombre_ticket": item.producto.nombre_ticket,
                    "unidad_medida": item.producto.unidad_medida,
                    "unidad_abreviatura": item.producto.unidad_abreviatura,
                    "cantidad_por_precio": _decimal_string(
                        item.producto.cantidad_por_precio
                    ),
                },
                "cantidad": _decimal_string(item.cantidad),
                "precio_unitario": _decimal_string(item.precio_unitario),
                "subtotal": _decimal_string(item.subtotal),
            }
            for item in pedido.items.all()
        ],
    }


def pedidos_pos_v1(request):
    """Entrega pedidos confirmados mediante paginacion keyset de solo lectura."""

    request_id = _request_id(request)
    if request.method != "GET":
        return _error(
            "method_not_allowed",
            "Este endpoint admite unicamente GET.",
            status=405,
            request_id=request_id,
            headers={"Allow": "GET"},
        )
    if settings.POS_API_REQUIRE_HTTPS and not request.is_secure():
        return _error(
            "https_required",
            "La API POS requiere HTTPS.",
            status=426,
            request_id=request_id,
        )

    token_fingerprint, auth_error = _authenticate(request)
    if auth_error == "service_not_configured":
        logger.error("pos_api request_id=%s resultado=no_configurada", request_id)
        return _error(
            "service_unavailable",
            "La API POS no esta configurada.",
            status=503,
            request_id=request_id,
        )
    if auth_error:
        logger.warning("pos_api request_id=%s resultado=no_autorizado", request_id)
        return _error(
            "unauthorized",
            "Credencial de servicio ausente o invalida.",
            status=401,
            request_id=request_id,
            headers={"WWW-Authenticate": "Bearer"},
        )

    rate_error, rate_headers = _rate_limit(token_fingerprint)
    if rate_error:
        logger.warning("pos_api request_id=%s resultado=rate_limit", request_id)
        return _error(
            "rate_limited",
            "Se excedio el limite temporal de solicitudes.",
            status=429,
            request_id=request_id,
            headers=rate_headers,
        )

    unknown_parameters = set(request.GET) - {
        "cursor",
        "desde",
        "hasta",
        "limite",
        "sucursal_id",
    }
    if unknown_parameters:
        return _error(
            "invalid_parameter",
            "La solicitud contiene un parametro no reconocido.",
            status=400,
            request_id=request_id,
            headers=rate_headers,
        )

    try:
        desde = _parse_aware_datetime(request.GET.get("desde"), "desde")
        hasta = _parse_aware_datetime(request.GET.get("hasta"), "hasta")
        if hasta <= desde:
            raise ValueError("hasta debe ser posterior a desde.")
        if hasta - desde > settings.POS_API_MAX_WINDOW:
            raise ValueError(
                "La ventana solicitada excede el maximo permitido de "
                f"{settings.POS_API_MAX_WINDOW.days} dias."
            )
        limit = _parse_limit(request.GET.get("limite"))
        sucursal_ids = _parse_sucursal_ids(request.GET.get("sucursal_id"))
        fingerprint = _filter_fingerprint(desde, hasta, sucursal_ids)
        cursor = _decode_cursor(
            request.GET.get("cursor"), fingerprint, desde, hasta
        )
    except RuntimeError:
        return _error(
            "service_unavailable",
            "La API POS no esta configurada.",
            status=503,
            request_id=request_id,
            headers=rate_headers,
        )
    except ValueError as exc:
        return _error(
            "invalid_parameter",
            str(exc),
            status=400,
            request_id=request_id,
            headers=rate_headers,
        )

    item_queryset = ItemPedido.objects.select_related("producto").order_by("id")
    pedidos = (
        Pedido.objects.filter(
            eliminado=False,
            estado=Pedido.Estado.CONFIRMADO,
            fecha_confirmacion__isnull=False,
            fecha_confirmacion__gte=desde,
            fecha_confirmacion__lt=hasta,
            sucursal_cliente_id__in=sucursal_ids,
            sucursal_cliente__tipo=SucursalCliente.Tipo.SUCURSAL,
        )
        .select_related("sucursal_cliente")
        .prefetch_related(Prefetch("items", queryset=item_queryset))
        .order_by("fecha_confirmacion", "id")
    )
    if cursor:
        cursor_timestamp, cursor_id = cursor
        pedidos = pedidos.filter(
            Q(fecha_confirmacion__gt=cursor_timestamp)
            | Q(fecha_confirmacion=cursor_timestamp, id__gt=cursor_id)
        )

    page = list(pedidos[: limit + 1])
    has_more = len(page) > limit
    page = page[:limit]
    next_cursor = (
        _encode_cursor(page[-1], fingerprint) if has_more and page else None
    )
    payload = {
        "version": "v1",
        "request_id": request_id,
        "data": [_serialize_pedido(pedido) for pedido in page],
        "page": {
            "has_more": has_more,
            "limit": limit,
            "next_cursor": next_cursor,
            "returned": len(page),
        },
    }
    logger.info(
        "pos_api request_id=%s resultado=ok pedidos=%s limite=%s cursor=%s",
        request_id,
        len(page),
        limit,
        bool(request.GET.get("cursor")),
    )
    return _json_response(
        payload,
        status=200,
        request_id=request_id,
        headers=rate_headers,
    )
