"""API de solo lectura para sincronizar pedidos confirmados hacia el POS."""

import hashlib
import hmac
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
from django.db.models import Max, Prefetch, Q
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import ItemPedido, Pedido, PedidoPurgado, PosApiCredential, SucursalCliente


logger = logging.getLogger(__name__)

CURSOR_SALT = "pedidos.api_pos.v1.cursor"
CURSOR_SALT_V2 = "pedidos.api_pos.v2.cursor"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RetentionGap(Exception):
    """El historial pedido ya no puede entregarse como una secuencia completa."""


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

    # compare_digest con str rechaza caracteres no ASCII. Comparar hashes de
    # longitud fija también evita filtrar la longitud del token configurado.
    candidate_bytes = candidate.encode("utf-8")
    candidate_digest = hashlib.sha256(candidate_bytes).digest()
    matched = False
    for configured in tokens:
        configured_digest = hashlib.sha256(configured.encode("utf-8")).digest()
        matched |= secrets.compare_digest(candidate_digest, configured_digest)
    if not matched:
        return None, "unauthorized"
    return candidate_digest.hex()[:24], None


def _authenticate_v2(request):
    """Resuelve un bearer v2 a un Edge y una sucursal, sin usar la allowlist v1."""
    if not PosApiCredential.objects.exists():
        return None, (), "service_not_configured"

    authorization = request.headers.get("Authorization", "")
    if len(authorization) > 1024:
        return None, (), "unauthorized"
    scheme, separator, candidate = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not candidate:
        return None, (), "unauthorized"
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    credential = (
        PosApiCredential.objects.select_related("sucursal_cliente")
        .filter(token_sha256=digest)
        .first()
    )
    if credential is None:
        return None, (), "unauthorized"
    now = timezone.now()
    if not credential.active or credential.revoked_at or (
        credential.expires_at and credential.expires_at <= now
    ):
        return None, (), "unauthorized"
    if (
        not isinstance(credential.scopes, list)
        or "orders:v2:read" not in credential.scopes
        or credential.sucursal_cliente.tipo != SucursalCliente.Tipo.SUCURSAL
        or not credential.sucursal_cliente.activa
    ):
        return None, (), "forbidden"
    PosApiCredential.objects.filter(pk=credential.pk).update(last_used_at=now)
    return digest[:24], (credential.sucursal_cliente_id,), None


def _rate_limit(token_fingerprint, *, version=1):
    limit = settings.POS_API_RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return None, {}

    now = time.time()
    bucket = int(now // 60)
    key = f"pos-api:v{version}:{token_fingerprint}:{bucket}"
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


def _parse_sucursal_ids(raw_value, *, allowed_ids=None, version=1):
    allowed = set(settings.POS_API_ALLOWED_SUCURSAL_IDS if allowed_ids is None else allowed_ids)
    if not allowed:
        raise RuntimeError("service_not_configured")
    if version == 2 and raw_value in (None, ""):
        raise ValueError("sucursal_id es obligatorio.")
    if raw_value in (None, ""):
        return tuple(sorted(allowed))
    if version == 2 and not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", raw_value):
        raise ValueError("sucursal_id debe contener IDs enteros positivos separados por coma.")
    try:
        requested = {int(part.strip()) for part in raw_value.split(",") if part.strip()}
    except ValueError as exc:
        raise ValueError("sucursal_id debe contener IDs enteros separados por coma.") from exc
    if not requested:
        raise ValueError("sucursal_id no puede estar vacio.")
    if any(value <= 0 for value in requested):
        raise ValueError("sucursal_id contiene un identificador no permitido.")
    if not requested.issubset(allowed):
        if version == 2:
            raise PermissionError("forbidden")
        raise ValueError("sucursal_id contiene un identificador no permitido.")
    return tuple(sorted(requested))


def _filter_fingerprint(desde, hasta, sucursal_ids, *, version=1):
    canonical = json.dumps(
        {
            "desde": _iso_utc(desde),
            "hasta": _iso_utc(hasta),
            "sucursal_ids": list(sucursal_ids),
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_cursor(pedido, fingerprint, *, version=1, purge_epoch=None):
    payload = {
        "f": fingerprint,
        "id": pedido.id,
        "ts": _iso_utc(pedido.fecha_confirmacion),
        "v": version,
    }
    if version == 2:
        payload["purge_epoch"] = purge_epoch
    return signing.dumps(
        payload,
        salt=CURSOR_SALT_V2 if version == 2 else CURSOR_SALT,
        compress=True,
    )


def _decode_cursor(
    raw_cursor, fingerprint, desde, hasta, *, version=1, purge_epoch=None
):
    if not raw_cursor:
        return None
    if version == 2 and re.fullmatch(r"[0-9]{1,20}", raw_cursor):
        # El consumidor POS identifica este cursor legado como una brecha que
        # exige conciliación, no como una solicitud malformada ordinaria.
        raise RetentionGap
    if len(raw_cursor) > 2048:
        raise ValueError("cursor invalido.")
    try:
        payload = signing.loads(
            raw_cursor,
            salt=CURSOR_SALT_V2 if version == 2 else CURSOR_SALT,
        )
        cursor_id = int(payload["id"])
        cursor_timestamp = _parse_aware_datetime(payload["ts"], "cursor")
        valid = (
            payload.get("v") == version
            and payload.get("f") == fingerprint
            and cursor_id > 0
            and desde <= cursor_timestamp < hasta
        )
    except (AttributeError, KeyError, TypeError, ValueError, signing.BadSignature) as exc:
        raise ValueError("cursor invalido.") from exc
    if not valid:
        raise ValueError("cursor invalido para los filtros solicitados.")
    if version == 2:
        cursor_epoch = payload.get("purge_epoch")
        if type(cursor_epoch) is int and cursor_epoch >= 0:
            # Cursores v2 previos al epoch opaco: requieren volver a conciliar.
            raise RetentionGap
        if not isinstance(cursor_epoch, str) or not re.fullmatch(r"[0-9a-f]{64}", cursor_epoch):
            raise ValueError("cursor invalido.")
        if cursor_epoch != purge_epoch:
            raise RetentionGap
    return cursor_timestamp, cursor_id


def _serialize_pedido(pedido, *, version=1):
    serialized = {
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
    if version == 2:
        serialized["codigo_publico"] = str(pedido.codigo_publico)
    return serialized


def _latest_purge_epoch(sucursal_ids):
    # El cursor sólo depende de purgas del alcance solicitado, no de otras sedes.
    ultimo_id = (
        PedidoPurgado.objects.filter(
            sucursal_cliente_id__in=sucursal_ids,
            fecha_confirmacion__isnull=False,
        ).aggregate(epoch=Max("registro_id"))["epoch"]
        or 0
    )
    # signing.dumps firma, pero NO cifra la carga del cursor. No exponer el ID
    # global de RegistroPurga, cuyos saltos revelarían actividad de otras sedes.
    alcance = ",".join(str(sucursal_id) for sucursal_id in sorted(sucursal_ids))
    mensaje = f"pedidos-pos-v2:{alcance}:{ultimo_id}".encode("ascii")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), mensaje, hashlib.sha256).hexdigest()


def _purge_range_intersects(desde, hasta, sucursal_ids):
    # Los tombstones permiten consultar la fecha exacta y evitar que un 410
    # revele una purga de otra sucursal o de un hueco entre min/max del lote.
    return PedidoPurgado.objects.filter(
        sucursal_cliente_id__in=sucursal_ids,
        fecha_confirmacion__gte=desde,
        fecha_confirmacion__lt=hasta,
    ).exists()


def _pedidos_pos(request, *, version):
    """Entrega pedidos confirmados mediante paginación keyset de sólo lectura."""

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

    if version == 2:
        token_fingerprint, allowed_sucursal_ids, auth_error = _authenticate_v2(request)
    else:
        token_fingerprint, auth_error = _authenticate(request)
        allowed_sucursal_ids = None
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
            "forbidden" if auth_error == "forbidden" else "unauthorized",
            "El acceso solicitado no esta autorizado." if auth_error == "forbidden"
            else "Credencial de servicio ausente o invalida.",
            status=403 if auth_error == "forbidden" else 401,
            request_id=request_id,
            headers={} if auth_error == "forbidden" else {"WWW-Authenticate": "Bearer"},
        )

    rate_error, rate_headers = _rate_limit(token_fingerprint, version=version)
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
        sucursal_ids = _parse_sucursal_ids(
            request.GET.get("sucursal_id"),
            allowed_ids=allowed_sucursal_ids,
            version=version,
        )
        fingerprint = _filter_fingerprint(
            desde, hasta, sucursal_ids, version=version
        )
        purge_epoch = None
        purge_scope_ids = ()
        if version == 2:
            # La API sólo entrega sucursales, aunque la allowlist incluya por
            # error el ID de un cliente mayorista.
            purge_scope_ids = tuple(SucursalCliente.objects.filter(
                pk__in=sucursal_ids,
                tipo=SucursalCliente.Tipo.SUCURSAL,
            ).values_list("pk", flat=True))
            purge_epoch = _latest_purge_epoch(purge_scope_ids)
            if _purge_range_intersects(desde, hasta, purge_scope_ids):
                raise RetentionGap
        cursor = _decode_cursor(
            request.GET.get("cursor"),
            fingerprint,
            desde,
            hasta,
            version=version,
            purge_epoch=purge_epoch,
        )
    except RetentionGap:
        return _error(
            "retention_gap",
            "El historial solicitado ya no puede entregarse completo; requiere conciliacion.",
            status=410,
            request_id=request_id,
            headers=rate_headers,
        )
    except RuntimeError:
        return _error(
            "service_unavailable",
            "La API POS no esta configurada.",
            status=503,
            request_id=request_id,
            headers=rate_headers,
        )
    except PermissionError:
        return _error(
            "forbidden",
            "El acceso solicitado no esta autorizado.",
            status=403,
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
    if version == 2 and (
        _latest_purge_epoch(purge_scope_ids) != purge_epoch
        or _purge_range_intersects(desde, hasta, purge_scope_ids)
    ):
        return _error(
            "retention_gap",
            "El historial cambio durante la consulta; requiere conciliacion.",
            status=410,
            request_id=request_id,
            headers=rate_headers,
        )
    has_more = len(page) > limit
    page = page[:limit]
    next_cursor = None
    if has_more and page:
        next_cursor = _encode_cursor(
            page[-1], fingerprint, version=version, purge_epoch=purge_epoch
        )
    payload = {
        "version": f"v{version}",
        "request_id": request_id,
        "data": [_serialize_pedido(pedido, version=version) for pedido in page],
        "page": {
            "has_more": has_more,
            "limit": limit,
            "next_cursor": next_cursor,
            "returned": len(page),
        },
    }
    logger.info(
        "pos_api_v%s request_id=%s resultado=ok pedidos=%s limite=%s cursor=%s",
        version,
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


def pedidos_pos_v1(request):
    """Contrato v1 conservado para consumidores existentes."""
    return _pedidos_pos(request, version=1)


def pedidos_pos_v2(request):
    """Contrato v2 con UUID público y señal explícita de brecha de retención."""
    return _pedidos_pos(request, version=2)
