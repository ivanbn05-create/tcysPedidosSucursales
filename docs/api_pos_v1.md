# Contrato API POS v1

La API permite que el POS lea pedidos confirmados sin conectarse directamente
a PostgreSQL. Django conserva la responsabilidad de elegir la base mediante
`DATABASE_URL`; el contrato no cambia si el servidor pasa de Supabase a otra
instancia PostgreSQL.

## Endpoint y autenticación

```text
GET /api/v1/pos/pedidos/
Authorization: Bearer <TOKEN_POS>
X-Request-ID: <identificador-opcional-del-cliente>
```

Producción prevista:

```text
https://tcysweb.lostocayos-lostcys.com.mx/api/v1/pos/pedidos/
```

Ejemplo con valores ficticios:

```bash
curl --get --fail --silent --show-error \
  -H 'Authorization: Bearer <TOKEN_POS>' \
  --data-urlencode 'desde=2026-09-22T14:00:00Z' \
  --data-urlencode 'hasta=2026-09-22T15:00:00Z' \
  --data-urlencode 'sucursal_id=3' \
  --data-urlencode 'limite=100' \
  https://tcysweb.lostocayos-lostcys.com.mx/api/v1/pos/pedidos/
```

Sólo se admite HTTPS cuando `POS_API_REQUIRE_HTTPS=True`. El token vive fuera
de Git en los secretos del servidor y del POS. Debe tener al menos 32
caracteres aleatorios. La comparación es constante y el token nunca se incluye
en logs o respuestas.

Para rotarlo sin interrupción:

1. Configurar `POS_API_TOKENS=<nuevo>,<anterior>` y desplegar/reiniciar de forma
   controlada.
2. Actualizar el secreto del agente POS y verificar una sincronización.
3. Dejar `POS_API_TOKENS=<nuevo>` y retirar el anterior.

Si no hay tokens o IDs permitidos configurados, la API falla cerrada con 503.

## Filtros

| Parámetro | Obligatorio | Semántica |
| --- | --- | --- |
| `desde` | Sí | Timestamp ISO 8601 con zona, inclusivo. |
| `hasta` | Sí | Timestamp ISO 8601 con zona, exclusivo. |
| `sucursal_id` | No | Uno o varios IDs separados por coma; deben pertenecer a la allowlist. |
| `limite` | No | Tamaño de página; default 100, máximo configurable (500 por defecto). |
| `cursor` | No | Cursor opaco devuelto por la página anterior. |

La ventana máxima es 31 días por defecto. Se rechazan parámetros desconocidos.
Sólo salen filas de `pedidos_pedido` con estado exacto `confirmado`, sin borrado
lógico y con sucursal de tipo `sucursal`. Además, el ID debe estar en
`POS_API_ALLOWED_SUCURSAL_IDS`; no se infiere un rango ni se incluyen clientes
mayoristas accidentalmente.

## Orden y cursor

El orden es ascendente por la clave compuesta:

```text
(fecha_confirmacion, pedido.id)
```

El ID resuelve empates de timestamp. El cursor está firmado, versionado y
ligado a `desde`, `hasta` y los IDs de sucursal. No depende sólo del reloj y no
puede reutilizarse con otros filtros.

El agente debe persistir `next_cursor` únicamente después de convertir toda la
página en operaciones locales idempotentes. La identidad remota recomendada es
`pedido.id`; para renglones, `item.id`. Si una caída ocurre antes de persistir
el cursor, repetir la misma solicitud devuelve la misma página mientras el
origen no cambie. El POS debe aplicar un `upsert` o una restricción única sobre
esos IDs para evitar duplicados.

## Respuesta

Los decimales son cadenas para evitar pérdida de precisión. `subtotal` es el
valor almacenado en `pedidos_itempedido`; la API no lo recalcula.

```json
{
  "version": "v1",
  "request_id": "pos-sync-000123",
  "data": [
    {
      "id": 12001,
      "fecha_confirmacion": "2026-09-22T15:30:00.123456Z",
      "total": "245.50",
      "sucursal": {
        "id": 3,
        "nombre": "Sucursal Ejemplo",
        "tipo": "sucursal"
      },
      "items": [
        {
          "id": 45001,
          "pedido_id": 12001,
          "producto": {
            "id": 8,
            "nombre": "Producto Ejemplo",
            "nombre_ticket": "PROD EJ",
            "unidad_medida": "PIEZA (PZA)",
            "unidad_abreviatura": "PZA",
            "cantidad_por_precio": "1.000"
          },
          "cantidad": "2.000",
          "precio_unitario": "122.75",
          "subtotal": "245.50"
        }
      ]
    }
  ],
  "page": {
    "has_more": true,
    "limit": 100,
    "next_cursor": "<CURSOR_OPACO>",
    "returned": 1
  }
}
```

El payload cubre los identificadores y campos operativos de
`pedidos_pedido`, `pedidos_sucursalcliente`, `pedidos_itempedido` y
`pedidos_producto`, sin usuarios, correos, credenciales ni configuración
administrativa.

## Errores estables

```json
{
  "error": {
    "code": "invalid_parameter",
    "message": "hasta debe ser posterior a desde.",
    "request_id": "pos-sync-000123"
  }
}
```

| HTTP | `code` | Significado |
| --- | --- | --- |
| 400 | `invalid_parameter` | Filtro, ventana, límite o cursor inválido. |
| 401 | `unauthorized` | Bearer ausente o incorrecto. |
| 405 | `method_not_allowed` | Se intentó un método distinto de GET. |
| 426 | `https_required` | Se intentó HTTP cuando HTTPS es obligatorio. |
| 429 | `rate_limited` | Se excedió el límite temporal. |
| 503 | `service_unavailable` | Tokens o allowlist no configurados. |

Todas las respuestas llevan `Cache-Control: no-store` y `X-Request-ID`. El
rate limit usa el caché local existente y no agrega Redis; con varios workers
es una defensa por proceso, no un límite distribuido global. Nginx puede añadir
un límite global en una fase posterior si la operación lo requiere.

## Recomendaciones para el agente POS

- Timeout de conexión: 5 segundos; timeout total: 15 segundos.
- Reintentos con backoff y jitter para 429, 502, 503 y 504.
- Respetar `Retry-After` cuando exista.
- No avanzar el cursor si falla una sola operación local de la página.
- Guardar IDs remotos con restricción única y usar transacciones locales.
- Nunca registrar el header `Authorization` ni respuestas completas con datos
  operativos.
