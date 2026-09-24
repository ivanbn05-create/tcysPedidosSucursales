# API POS v2: identidad pública y señal de retención

Estado: candidato de integración privado sobre `8fad568`; la ruta v1 permanece disponible y sin cambios. El consumidor debe adoptar v2 y pasar las pruebas de contrato antes de depender de ella. La existencia de esta ruta en Git no significa que esté desplegada ni que la purga esté activada.

## Endpoint

```text
GET /api/v2/pos/pedidos/
Authorization: Bearer <TOKEN_POS>
```

HTTPS es obligatorio cuando `POS_API_REQUIRE_HTTPS=True`. V2 usa credenciales propias, guardadas sólo por SHA-256 y ligadas a un UUID de instalación Edge, una `SucursalCliente.id` de tipo sucursal y el scope `orders:v2:read`. Un bearer v1 no concede acceso v2. El parámetro `sucursal_id` es obligatorio: una sucursal ajena a la credencial devuelve 403 `forbidden`; un bearer inválido, vencido o revocado devuelve 401. La allowlist global de [v1](../api_pos_v1.md) permanece sólo para v1. `desde` es inclusivo y `hasta` exclusivo; ambos son timestamps ISO 8601 con zona. La ventana solicitada tiene máximo configurable de 31 días por defecto. Sólo se entregan pedidos `confirmado`, sin borrado lógico.

La respuesta conserva los campos v1 y añade `codigo_publico` (UUID único del modelo `Pedido`) a cada pedido. El POS debe acordar y persistir este UUID como identidad remota estable tras migrar su consumidor; `id` entero se conserva por compatibilidad de la forma de respuesta, y el folio derivado de fecha no es una clave de identidad. Los ítems conservan `id` entero en esta versión. Es necesaria una correspondencia verificada entre `SucursalCliente.id` entero de Pedidos y el UUID/clave de sucursal del POS; no inferirla por nombre.

```json
{
  "version": "v2",
  "request_id": "ejemplo-001",
  "data": [
    {
      "id": 12001,
      "codigo_publico": "d8fdb924-0f4c-4270-a6f5-eedfdc522aa7",
      "fecha_confirmacion": "2026-09-22T15:30:00.123456Z",
      "total": "245.50",
      "sucursal": {"id": 3, "nombre": "Sucursal Ejemplo", "tipo": "sucursal"},
      "items": []
    }
  ],
  "page": {"has_more": false, "limit": 100, "next_cursor": null, "returned": 1}
}
```

Los importes son cadenas decimales. En una respuesta real, `items` contiene los campos documentados en v1.

## Orden, cursor y brechas

El orden sigue siendo `(fecha_confirmacion, Pedido.id)` ascendente, de modo que dos pedidos con la misma marca temporal no se pierden. El cursor v2 está firmado y ligado a filtros y versión. Su época de purga es un HMAC opaco del último recibo de purga relevante para las sucursales consultadas: **no** expone `RegistroPurga.id` ni debe ser interpretado por el POS. El consumidor debe guardar y reenviar `next_cursor` sin decodificarlo. No se acepta un cursor v1 en v2 ni viceversa.

La v2 responde HTTP **410 Gone** con código estable `retention_gap` si:

1. la ventana `[desde, hasta)` incluye la `fecha_confirmacion` exacta de un `PedidoPurgado` de una sucursal consultada y retornable por la API; o
2. la época del cursor v2 difiere de la época vigente para esa sucursal, aunque la purga haya ocurrido fuera de la ventana solicitada. Un cursor v2 anterior, todavía firmado pero con época numérica, y el cursor legado numérico usado por el contrato POS también reciben 410 y requieren conciliación.

La comprobación usa tombstones por pedido, sucursal y fecha; una purga de otra sucursal, de un cliente mayorista o en un hueco entre dos fechas purgadas **no** produce 410 para esta consulta. Una purga sólo de eventos, sin pedido con `fecha_confirmacion`, tampoco altera la época POS. La época se comprueba de nuevo antes de responder para detectar purgas que terminen durante la consulta. La API no entrega una página vacía como sustituto de un 410 conocido. Sin tombstones que cubran la ventana, una página vacía significa que no hay pedidos *actualmente consultables* bajo esos filtros; no certifica historial completo anterior a la introducción del registro de purgas.

Los recibos `RegistroPurga` y tombstones `PedidoPurgado` son parte del estado necesario para recuperar el servicio: una restauración no puede omitirlos ni reiniciar la época de cursores sin conciliación y reemisión controlada. El HMAC evita revelar el ID secuencial, pero no sustituye el ledger técnico ni la reconciliación. Restaurar sólo filas de pedidos desde una copia antigua tampoco debe reintroducir contenido purgado.

```json
{
  "error": {
    "code": "retention_gap",
    "message": "El historial solicitado ya no puede entregarse completo; requiere conciliacion.",
    "request_id": "ejemplo-001"
  }
}
```

La API mantiene `Cache-Control: no-store` y `X-Request-ID` también en errores. Un cursor malformado o manipulado que no sea el legado numérico continúa dando 400 `invalid_parameter`; uno firmado pero obsoleto por purga o por el cambio a época opaca da 410. La v1 conserva su semántica histórica y **no detecta** estas brechas.

## Reanudación y conciliación del POS

El POS debe guardar cada página completa como operaciones locales idempotentes y confirmar su transacción local antes de persistir `next_cursor`. Debe tener restricción única para la identidad remota acordada. Repetir una página antes de avanzar el cursor no debe crear duplicados.

Ante 410, el Edge debe detener la sincronización de esa ventana, conservar su cursor y último punto local confirmado, registrar sólo metadatos no sensibles y solicitar conciliación supervisada. No debe saltar automáticamente al presente, convertir 410 en «sin pedidos» ni borrar datos locales. El operador compara identificadores de pedidos locales con el manifiesto/export verificado fuera del VPS o con el registro autorizado de recepción, resuelve la brecha y sólo entonces fija una nueva ventana/cursor. La API no puede reconstruir pedidos purgados.

La política de retención permite que un pedido salga del VPS tras descarga manual confirmada **antes** de cumplir 30 días, o al llegar a 30 días desde su primera recepción. Por eso 31 días es sólo el máximo de tamaño de una consulta, **no** una promesa de disponibilidad histórica durante 31 días ni de 30 días completos. Un Edge sin conexión más de 30 días necesita el procedimiento de conciliación aun cuando vuelva a autenticarse correctamente. La idempotencia local no debe depender de que el servidor conserve pedidos indefinidamente.

## Pruebas de contrato antes de adoptar v2

- Comparar una página v1/v2 con fixtures sintéticos: mismos pedidos e ítems; `codigo_publico` sólo en v2.
- Paginar pedidos con `fecha_confirmacion` idéntica y repetir páginas sin duplicados.
- Crear cursor, registrar purga fuera de ventana y comprobar 410 al reanudar; una consulta fresca de ventana no afectada puede continuar.
- Consultar ventana que cubre la fecha exacta de un pedido purgado de la sucursal autorizada aunque ya no existan filas: 410, no 200 vacío.
- Purgar una sucursal ajena o un mayorista: no filtrar esa actividad por 410 ni invalidar un cursor de la sucursal consultada. Un hueco entre dos fechas purgadas tampoco es una brecha conocida.
- Presentar un cursor v2 anterior con época numérica: 410 y conciliación, sin interpretar el contenido del token en el POS.
- Simular Edge desconectado más de 30 días y completar el procedimiento de conciliación sin pérdida ni duplicación.
- Verificar sucursal sin mapeo, mapeo duplicado y cambio de clave antes de abrir acceso productivo.

No activar purga ni sustituir el consumidor v1 por v2 en producción sin coordinar estos cambios con el agente POS y comprobar el contrato extremo a extremo.
