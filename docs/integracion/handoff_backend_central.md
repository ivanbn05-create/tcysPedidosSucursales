# Handoff: Pedidos, POS y backend central

Estado: **documento de coordinación, no contrato definitivo ni autorización de despliegue**. Los hechos del backend central proceden de `CONTRATO_EDGE_CENTRAL_BORRADOR_2026-09-23.md` y `ESTADO_STAGING_2026-09-23.md`, consultados el 2026-09-23. El primero identifica expresamente sus rutas y esquemas como propuestas. Hay que contrastarlos con el agente POS y con el estado posterior de ambos repositorios antes de fijar un contrato.

## Límites entre productos

- `tcysPedidosSucursales` conserva pedidos y expone `GET /api/v1/pos/pedidos/` y, en esta rama aún sin desplegar, `GET /api/v2/pos/pedidos/`. Ambas APIs son de **sólo lectura**; no reciben ventas ni clientes del POS.
- `tcysBackendCentral` es otro producto, con **base PostgreSQL propia**. Le correspondería recibir clientes y consolidaciones de ventas del Edge/POS. Sus políticas de retención y backup se aplican a su propia base, no convierten la base de Pedidos en una base compartida.
- `SucursalCliente` en Pedidos representa sucursales y clientes mayoristas. No es el modelo `ventas.Cliente` del POS. Tampoco un `Pedido` equivale a una venta de caja.

## Identidad y correspondencia origen → destino

| Origen | Destino/uso | Identidad comprobada o propuesta | Condición antes de integrar |
| --- | --- | --- | --- |
| Pedidos `SucursalCliente` de tipo `sucursal` | Filtro y sucursal en la API de pedidos | `SucursalCliente.id` entero, permitido explícitamente mediante `POS_API_ALLOWED_SUCURSAL_IDS` | Crear y verificar correspondencia con sucursal POS UUID y clave; no empatar por nombre. Rechazar IDs sin mapeo, duplicados o ambiguos. Versionar cambios de clave y conservar historial de mapeos. Excluir `cliente_mayorista`. |
| Pedidos `Pedido` | Pedido descargado por el POS | La API v1 devuelve `Pedido.id` entero; la v2 propuesta en esta rama añade `codigo_publico` UUID único sin alterar v1. | Acordar con el agente POS la identidad remota persistida y probar v2 de extremo a extremo. No sustituir v1 en producción sólo por existir el código. El folio basado en fecha no es identidad. |
| Pedidos `ItemPedido` | Renglón del pedido descargado | La API v1 devuelve `ItemPedido.id` entero | El POS debe aplicar operación local idempotente y restricción única para cada identidad acordada antes de avanzar el cursor. |
| POS sucursal/Edge | Central: sujeto autenticado, clientes y agregado mensual | UUID de sucursal más clave; la identidad de Edge y su vínculo autenticado requieren validación | Registrar correspondencia explícita con la sucursal de Pedidos. Un nombre igual no demuestra que sea la misma sucursal. Resolver cambios de clave, reinstalación y dos Edges que pretendan la misma sucursal. |
| POS `ventas.Cliente` | Central: cliente permanente | Propuesta: `(sucursal UUID, cliente UUID de origen)` | No fusionar por teléfono, nombre ni UUID local aislado de la sucursal. Definir campos admitidos y consentimiento/propósito de notas, teléfonos y domicilios. |
| POS teléfonos y domicilios del cliente | Central: relaciones permanentes | UUID propio por relación, bajo el cliente y la sucursal de origen | Un snapshot más nuevo reemplazaría las relaciones sólo según el contrato ratificado; no aplicar purga de ventas. |
| POS consolidación mensual | Central: agregado de ventas por periodo | `idempotencia` UUID por envío; unicidad durable propuesta para `(edge, idempotencia)` y `(sucursal, periodo)` | Comprobar que una restauración o reinstalación no reintroduce un periodo ya purgado con otro UUID. El agregado no contiene líneas de venta ni clientes. |

La correspondencia de sucursales necesita un registro administrado fuera de las respuestas de estas APIs, con revisión de unicidad en ambos sentidos, vigencia y evidencia de la validación. No configurar una sucursal en la allowlist de Pedidos únicamente porque su nombre coincida con uno del POS. Un cambio de clave no debe crear una nueva identidad ni reasignar datos anteriores sin conciliación.

## Pedidos → POS: continuidad frente a retención

El contrato desplegado por el trabajo anterior es `GET /api/v1/pos/pedidos/`, con Bearer token, HTTPS en producción, ventana `[desde, hasta)` de hasta 31 días por defecto, allowlist de sucursales y orden `(fecha_confirmacion, Pedido.id)`. Sólo devuelve pedidos `confirmado`, no eliminados y asociados a una sucursal. El cursor está firmado y ligado a los filtros, pero v1 no distingue una página genuinamente vacía de un intervalo cuyos pedidos ya fueron purgados. Esta rama implementa **sin desplegar** v2 con UUID público, cursor versionado y `410 retention_gap` conservador; véanse [contrato, OpenAPI y fixtures sintéticos](api_pos_v2.md).

Antes de aplicar la retención de Pedidos, acordar y probar **v2 con el agente POS**, incluido un Edge desconectado más de 30 días y la conciliación de un 410. La idempotencia local del POS debe sobrevivir a que Pedidos deje de conservar un pedido; no debe depender de consultar indefinidamente el mismo registro. V1 se conserva para no romper consumidores anteriores; la adopción de v2 exige prueba de contrato cruzada y mapa de sucursales validado.

## POS → central: consolidación mensual observada

La versión pública POS inspeccionada en el borrador central (`codex/candidata-0.4.0-dev.9`) envía un cuerpo con `version_contrato: 1`, `idempotencia` UUID, `sucursal` (UUID, clave, nombre), `periodo` mensual y un objeto `totales`. Envía también `Idempotency-Key` igual al UUID y, si está configurado, Bearer token. **No envía ventas individuales.** La ruta `POST /api/v1/edge/consolidaciones-mensuales/` es propuesta; la URL efectiva la controla `VPS_CONSOLIDACION_URL` en el Edge.

El POS acepta como éxito HTTP 2xx con JSON `recibido: true` y `acuse` no vacío, y después borra el detalle mensual local. Por ello, el central sólo debe emitir ese ACK tras un commit durable. Una repetición idéntica debe devolver el mismo acuse. Un mismo ID o `(sucursal, periodo)` con otro contenido debe dar 409 sin ACK de éxito. Después de la purga, el recibo técnico mínimo debe impedir rehidratación del agregado y conservar el acuse estable sin restaurar su payload. No se deben inventar renglones de venta a partir de `totales` ni sumarlos de nuevo cuando se añada un futuro flujo de detalle.

Para ventas individuales futuras quedan sin resolver: UUID estable por venta y lote incluso tras restore local, instante y zona horaria, correcciones/cancelaciones, ACK parcial, límites y compresión, y reconciliación con el resumen mensual para evitar doble conteo. Requieren contrato versionado y pruebas con el agente POS antes de implementarse.

## POS → central: clientes permanentes propuestos

El borrador central propone un snapshot completo de `ventas.Cliente`, con `sucursal` UUID/clave, `cliente` UUID, clave corta, nombre, notas, activo, marcas `creado_en`/`actualizado_en`, teléfonos y domicilios con UUID propios. La ruta candidata `POST /api/v1/edge/clientes/` está desactivada por defecto mediante `CENTRAL_ENABLE_DRAFT_CUSTOMER_API`; el POS publicado inspeccionado aún no envía ese mensaje. La función POS `cliente_payload` observada no incluye todavía `activo`, `creado_en` y `actualizado_en`.

Propuesta pendiente de ratificación: autenticar un Edge ligado a la sucursal; upsert atómico por `(sucursal UUID, cliente UUID)`; ACK sólo después de commit; repetición exacta con acuse estable; versión más antigua sin sobrescritura; igual `actualizado_en` UTC con hash distinto como conflicto 409. Una versión monotónica de origen sería preferible al timestamp. No registrar payload, token ni datos personales en logs. Clientes y sus relaciones quedan en el conjunto permanente y en backup de largo plazo separado de ventas. Definir expresamente si se conservarán notas, teléfonos y domicilios antes de activar la ruta.

## Retención, exportación y restauración en el central

La política solicitada para datos transaccionales es elegibilidad al primero entre exportación manual verificada/confirmada y 30 días desde `first_received_at` fijado por el VPS en el primer commit. Reintentos no renuevan esa marca. Generar un archivo, iniciar descarga o recibir HTTP 200 no prueba que un equipo autorizado lo guardó y verificó su hash. El recibo técnico posterior a purga puede conservar sólo identificadores opacos, fechas, conteos/hash técnico y motivo; no cifras de venta, datos de cliente ni cuerpo original.

Los backups permanentes han de excluir snapshots transaccionales; una restauración temporal con ventas debe quedar aislada hasta reconciliar las purgas, incluidas las ocurridas antes de 30 días por descarga confirmada. Los clientes no entran en esa purga. Estos son requisitos de diseño y prueba, **no evidencia de que el timer o el flujo de exportación estén activos**.

## Estado comprobado del central y decisiones pendientes

El registro de staging del 2026-09-23 informa un release `a931c462fefcfed021e035ea6aaf367e2d15b1d1` con servicio privado en `127.0.0.1:8010` y base PostgreSQL de staging propia. Sus checks y 23 pruebas pasaron; se ensayó backup selectivo y restauración con datos sintéticos. Tras limpiar las pruebas no quedaron sucursales, clientes, recibos ni snapshots. La API candidata de clientes respondía 404 porque estaba desactivada. No se había conectado un Edge real ni publicado el servicio por Nginx/DNS. Consultar el estado real más reciente antes de cualquier ensayo.

Handoff pendiente entre responsables de Pedidos, POS y central:

1. Aprobar y probar la correspondencia de sucursales y la identidad externa de pedidos; publicar una versión compatible de la API de Pedidos si cambia `Pedido.id` por `codigo_publico` o se añade señal de retención.
2. Ratificar el esquema, autenticación, versionado e idempotencia de clientes; actualizar el emisor POS antes de habilitar el receptor central.
3. Ratificar la ruta y las respuestas de consolidación mensual; probar primera recepción, repetición, conflicto, caída antes/después de commit y reintento después de purga.
4. Acordar el contrato futuro de ventas detalladas y su relación contable con el agregado mensual; no afirmar que el flujo actual ya transporta detalle.
5. Validar exportación a equipo autorizado, hash/confirmación, política de backups y restauración sin reintroducción antes de activar purga.

No crear endpoints de clientes o ventas en `tcysPedidosSucursales` como sustituto del central. El handoff describe lo que debe implementarse y verificarse en `tcysBackendCentral` y en el agente POS; no declara esos flujos productivos.
