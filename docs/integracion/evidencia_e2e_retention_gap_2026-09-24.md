# E2E privado: purga sintética y `retention_gap` de Pedidos v2

Fecha: 2026-09-24. Candidato de código `625ce988caac31bfdcf643ce06564c02aac4acc2`, servicio privado `tcysweb-e2e.service` en `127.0.0.1:8003`, release `/srv/tcysPedidosSucursales-e2e/releases/625ce98`, PostgreSQL `tcysweb_e2e`. No se tocó 8002, `tcysweb-prod`, Supabase ni el POS productivo.

## Preparación y resguardo

El POS LAB01 ya había importado los tres pedidos reales de laboratorio de Pedidos IDs 1, 2 y 3 desde tres páginas y repetido la sincronización con cero altas. El pedido 4 de LAB02 también existía. Se tomó una instantánea de identidad, sucursal, estado y total de los cuatro antes del ensayo. La purga permanente y la automática permanecían deshabilitadas. No había tombstones.

El harness temporal privado, guardado en el workspace como `.release/e2e_pedidos_retention_gap.py` y transferido sólo a `/tmp` durante la prueba, tenía SHA-256 `8c568fbef824e3d4a2591535b661ce8879aac13ebb4c32320d0876e295e741aa`. El hash se verificó en ambos extremos. Se ejecutó como `tcyswebe2e` con el EnvironmentFile del lab; no contenía bearer ni secretos, los leía de los archivos `0600` del servicio. Se eliminó de `/tmp` al terminar.

El script afirmó que la conexión apuntaba exactamente a `tcysweb_e2e`, que los cuatro pedidos base existían y correspondían a LAB01/LAB02, y que `RETENTION_PURGE_ENABLED=False` y `RETENTION_LOCAL_DB_CONFIRMED=False` en la configuración persistente. Ante cualquier discrepancia debía detenerse antes de purgar.

## Secuencia ejecutada

1. Se capturó por HTTP real de 8003 una primera página de LAB01 con `limite=1` y cursor firmado, dentro de la ventana que contiene los pedidos 1–3.
2. Se creó sólo el pedido sintético ID 5 de LAB01, confirmado con fecha de negocio cuatro días anterior, un ítem y primera recepción fijada por el trigger PostgreSQL del VPS. La API v2 lo devolvió 200 antes de la purga.
3. El pedido sintético pasó a `recibido` para cerrar su operación. Se generó un ZIP privado que contenía sólo ID 5, se copió a un segundo directorio privado del laboratorio, se comparó su SHA-256 con el ticket, se confirmó el lote y se verificó que el ZIP de origen se eliminó.
4. El dry-run de purga seleccionó exclusivamente ID 5 por exportación confirmada; no mostró otros pedidos elegibles ni bloqueados. Se ejecutó `aplicar_purga()` con `RETENTION_PURGE_ENABLED=True` **sólo como override en ese proceso**. El lote quedó confirmado, el pedido desapareció, y apareció un tombstone del ID 5 con motivo `exportacion`. La copia verificada sintética se retiró al finalizar el ensayo; esto prueba el flujo de código, no custodia externa de exportaciones de negocio.
5. Se consultó otra vez la API por HTTP real en 8003. Una comprobación adicional de LAB02 con su propia credencial y Django Client devolvió 200 para su pedido 4.

| Caso | Resultado |
| --- | --- |
| Ventana que incluye fecha confirmada del ID 5 ya purgado | `410 retention_gap`, `Cache-Control: no-store` |
| Cursor LAB01 anterior a la purga, en ventana de IDs 1–3 que no incluye ID 5 | `410 retention_gap` por cambio de época |
| Misma ventana de IDs 1–3, consulta fresca sin cursor | 200; primer pedido ID 1 |
| Cursor legado numérico `1727034600123456` en ventana no purgada | `410 retention_gap` |
| Bearer LAB01 que pide sucursal LAB02 | `403 forbidden` |
| Bearer LAB02 que pide su ventana | 200; pedido ID 4 |

Lote sintético: `5fb24d11-fdac-43b0-9d42-dbf6d1f9900c`; SHA-256 del ZIP verificado: `674dd9a1df23a9d9fc39ab8285553aaf4869a306016c8ef8bdee6603fc8b42ba3`. Comprobación independiente posterior: `Pedido` conserva sólo IDs `[1,2,3,4]`, tombstones de origen `[5]`, ningún otro pedido en el plan de exportación, `RETENTION_PURGE_ENABLED=False`, y el servicio 8003 sigue activo. La instantánea de los cuatro pedidos base resultó idéntica antes y después.

## Recuperación después de 410

El POS dev.10 sí recibe `410 retention_gap`: `ventas/integracion_sucursales.py` captura el error, marca `EstadoSincronizacionPedidos.estado=RECONCILIACION`, conserva cursor/high-water y bloquea el siguiente ciclo automático. La aplicación de página y el avance del checkpoint están en una misma transacción; los importados usan identidad UUID/ID para evitar duplicados. Sus pruebas cubren la conservación del checkpoint ante 410 y la reanudación de cursor tras otra falla.

**Bloqueo operativo:** no existe un comando ni una interfaz implementada para completar la conciliación supervisada y cargar desde un export verificado el contenido faltante antes de fijar una nueva ventana/cursor. El ledger de purga contiene sólo recibos y tombstones técnicos; no es un snapshot de pedidos y la API no puede reconstruir el pedido purgado. La copia del ZIP en este ensayo era sintética y se eliminó; no representa la custodia externa exigida para producción. Por ello no se reinició manualmente el checkpoint del POS LAB01 ni se declaró resuelta una brecha real.

Antes del corte productivo se necesita un procedimiento ejecutable y probado para: custodiar el export fuera del VPS, cotejar UUID/ID de pedidos locales contra manifiesto y recibos, importar de forma idempotente cualquier faltante verificado, registrar aprobación y evidencia, y sólo entonces avanzar o reinicializar de forma controlada el checkpoint. Un Edge desconectado más de 30 días o una exportación temprana puede necesitar este procedimiento incluso con conectividad y credencial válidas.

El ensayo del timeout de respuesta tras commit y reinicio del POS se documentará aparte cuando termine el harness aislado del agente POS; esta prueba de purga no alteró el checkpoint compartido del POS LAB01.
