# Recuperación supervisada de `410 retention_gap` — Pedidos Production 1.0

Estado: **candidato, no activado en producción**. Este procedimiento extiende
el contrato v2 sin cambiar el `GET /api/v2/pos/pedidos/`. No hay endpoint web
de confirmación: preparar/cerrar son comandos OS restringidos y deliberados.
Un 410 nunca se traduce a página vacía ni a salto automático del cursor.

## Identidades y precondiciones

Sólo Arboledas se habilita inicialmente, después de aprobar la matriz exacta
`POS branch UUID ↔ Edge UUID ↔ SucursalCliente.id ↔ sucursal Central`; **no
se usan nombres**. La credencial debe estar activa, con scope
`orders:v2:read` y `pos_branch_id` no nulo. La rotación conserva las cuatro
identidades y no altera UUID de pedido, cursor ni estado de recuperación.

El operador detiene la sincronización Edge y congela **todos** los escritores
y la purga de Pedidos antes de preparar el snapshot. Debe probar que el freeze
es efectivo, registrar su acta privada y SHA-256 y mantenerlo hasta cerrar.
`--writers-frozen` es una declaración explícita auditada, **no** un segundo
factor ni prueba técnica de que los escritores estén detenidos. Sin esa prueba
independiente no se cierra una recuperación productiva.

El Edge conserva su cursor previo, filtros `[desde,hasta)`, high-water y pedidos
locales; no los borra. El operador entrega el cursor en un archivo privado
`0600`, incluso si es vacío cuando el 410 proviene de tombstones de ventana.
La API debe reproducir el 410 para ese alcance; cursor firmado de otra
sucursal/filtros se rechaza. El registro `PosRetentionRecovery` guarda sólo
hash del cursor, identidad, ventana, época de purga opaca, hashes/conteos,
referencias y usuario OS; nunca cuerpos ni bearer.

## Preparar y entregar la baseline

Ejecutar como operador autorizado por sudoers **sólo en PostgreSQL aislado en
ensayo** hasta aprobar corte. No conceder shell interactivo o permiso general
de `manage.py` al usuario web. El directorio de artefactos debe estar fuera del
release/Git, ser del operador `0700`; cursor y archivos de acta `0600`.

```bash
python manage.py preparar_recuperacion_pos_v2 \
  --recovery-id '<UUID_NUEVO_DEL_TICKET>' \
  --edge-id '<EDGE_UUID_ARBOLEDAS>' \
  --pos-branch-id '<POS_BRANCH_UUID_ARBOLEDAS>' \
  --sucursal-id '<SUCURSAL_CLIENTE_ID_APROBADO>' \
  --desde '<UTC_INCLUSIVO>' --hasta '<UTC_EXCLUSIVO>' \
  --cursor-file '<RUTA_PRIVADA_0600>' \
  --archivo '<SNAPSHOT_NUEVO_PRIVADO_0600.zip>' \
  --referencia '<TICKET_NO_SENSIBLE>' --writers-frozen --confirm
```

El ZIP contiene `manifest.json`, `orders.jsonl` (payload v2 de pedidos aún
válidos de la ventana) y `tombstones.json` (UUID de **todos** los pedidos
purgados de esa sucursal con fecha de confirmación, incluso fuera de la
ventana: un cursor puede quedar obsoleto por una purga externa a ella). El
manifest da SHA-256 de ambos contenidos, conteos, Edge/sucursal exactos,
SHA-256 del cursor previo, época opaca y siguiente límite `next_desde=hasta`.
El ZIP entero tiene SHA-256 persistido. La baseline se entrega por canal
privado al Edge y se coteja allí por hash antes de importar. Un mismo
`recovery_id` y parámetros/archivo idénticos devuelve el acta existente; un
retry con datos distintos falla. Se limita a 5 000 pedidos por ventana: si
excede, dividir sólo tras rediseñar y aprobar ventanas contiguas; nunca
truncar ni avanzar cursor por límite.

El snapshot **no puede reconstruir** cuerpos purgados. Para cada UUID de
`tombstones.json`, el operador verifica un export custodio fuera del VPS o que
ya existe en el Edge, y obtiene un acta de custodia. Si falta contenido o no
hay prueba de recepción, estado `intervencion_manual`; no hay punto nuevo de
continuidad. El Edge importa `orders.jsonl` idempotentemente por
`codigo_publico`, restaura desde archivo los purgados faltantes y emite un
acuse privado después de confirmar la transacción local. El acuse enumera los
UUID finales presentes y los irresueltos; la especificación de archivos y
fixtures sintéticos está en `docs/integracion/schemas/`.

## Cerrar sin pérdida silenciosa

```bash
python manage.py cerrar_recuperacion_pos_v2 \
  --recovery-id '<MISMO_UUID>' \
  --snapshot-file '<SNAPSHOT_PRIVADO.zip>' \
  --snapshot-sha256 '<SHA_OBTENIDO_EN_EL_DESTINO>' \
  --edge-ack-file '<ACUSE_EDGE_PRIVADO.json>' \
  --edge-ack-sha256 '<SHA_DEL_CANAL_INDEPENDIENTE>' \
  --custody-file '<ACTA_CUSTODIA_PRIVADA.json>' \
  --custody-sha256 '<SHA_DEL_CANAL_INDEPENDIENTE>' \
  --freeze-evidence-sha256 '<SHA_DEL_ACTA_DE_FREEZE>' \
  --referencia '<ACTA_APROBADA_NO_SENSIBLE>' \
  --writers-frozen --confirm
```

El comando compara SHA de los tres archivos, identidad Edge/sucursal,
ventana, cursor previo, todos los UUID aún válidos y purgados, acuse Edge,
custodia y estado actual de pedidos/purga. Si falta/sobra un UUID en el Edge,
hay irresueltos, un tombstone sin archivo o prueba de presencia previa, o
cambió la baseline bajo el freeze, **no** habilita continuidad. Discrepancias
de contenido o época exigen nueva baseline; faltantes generan estado durable
`intervencion_manual`. Sólo `completada` autoriza al operador y agente3 a
registrar en el Edge `next_cursor=null`, `desde=hasta` anterior, con el
`recovery_id` y SHA del snapshot en la misma transacción local. La API no
escribe el checkpoint del POS. El cierre repetido con idénticas evidencias es
idempotente; con otras se rechaza.

La prueba de cobertura por UUID y SHA **no autentica criptográficamente** al
firmante de las actas ni prueba por sí sola que una importación externa sea
completa. El canal de entrega, hashes leídos en destino, custodia y aprobación
humana son gates operativos obligatorios. No se permite afirmar recuperación
exitosa con sólo flags o con el ledger de tombstones. Tras el cierre se
retira el ZIP transitorio del VPS y se aplica la política de retención a todas
sus copias; se conserva el acta técnica y el hash en custodia.
Si el caso queda en `intervencion_manual`, el ZIP tampoco puede quedar
indefinidamente en el VPS: se transfiere a custodia autorizada y se retira
según el primero entre confirmación de entrega y el límite de 30 días,
manteniendo el ticket, hashes y cursor en el Edge. No borrar la única copia
necesaria para resolver la pérdida sin un plan de custodia aprobado.

## HTTP → acción Edge

| Respuesta | Acción del Edge |
| --- | --- |
| `200` | Aplicar página y avanzar cursor en **una** transacción local; deduplicar por UUID. |
| `400 invalid_parameter` | Detener esa consulta; corregir filtros/cursor, sin borrar checkpoint. |
| `401 unauthorized` | Detener y pedir rotación/credencial; reintentar **mismo** cursor tras rotar. |
| `403 forbidden` | Detener; revisar matriz Edge/POS/`SucursalCliente.id`, no probar IDs alternos. |
| `410 retention_gap` | Estado `RECONCILIACION`, conservar cursor/high-water/pedidos, congelar avance y abrir ticket. No convertir en 200 vacío. |
| `426 https_required` | Corregir transporte TLS; no usar HTTP ni avanzar. |
| `429 rate_limited` | Respetar `Retry-After`, conservar cursor. |
| `5xx`/timeout | Reintento con backoff y mismo cursor; no asumir ACK ni vaciar pedidos. |

## Gate de corte y rollback

El E2E POS dev.10 anterior probó que entra en `RECONCILIACION`, pero **no**
probó este cierre nuevo. Agente3 debe implementar importación/acuse y cambio
de checkpoint transaccional; agente1 debe aprobar matriz y canal de custodia.
Ensayar en PostgreSQL aislado y POS dev.10 con (1) pedido válido, (2) purgado
presente en Edge, (3) purgado recuperado de archivo, (4) purgado irrecuperable,
(5) acuse manipulado, (6) retry, rotación y cursor previo, (7) escritura/purga
durante freeze que obliga abortar. No habilitar purga ni v2 productivo antes.
Rollback de ensayo: mantener al Edge en `RECONCILIACION` con cursor anterior;
no revertirlo a una página vacía. Si ya se cerró y el Edge abrió escrituras,
reparar hacia delante desde su checkpoint y evidencias, no resetearlo.
