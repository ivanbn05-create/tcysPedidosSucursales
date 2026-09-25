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

## Timeout tras commit y reinicio del POS

El agente POS ejecutó un harness aislado con una base SQLite clonada, sin tocar el checkpoint compartido de LAB01 ni hacer solicitudes de red. En el primer proceso aplicó una página sintética con origen 910001, confirmó el cursor en la misma transacción y simuló pérdida de la respuesta con `ErrorTransportePedidos`: quedó un importado, cursor persistido y estado `error_transitorio`. Un segundo proceso independiente reanudó desde el cursor confirmado, recibió de nuevo el mismo UUID y cerró la ventana en estado `listo`, con **cero importados adicionales y cero duplicados**. Harness: `scripts/e2e/e2e_pos_lab01_orders_restart.py` del checkout POS dev.10; base aislada `runtime/e2e/checkpoint_crash.sqlite3`. Esto acredita la recuperación local ante timeout/reinicio en la lógica POS; la ruta HTTP real de 8003 no participó en ese caso. La prueba de purga no alteró el checkpoint compartido del POS LAB01.

## Rotación y revocación de credencial v2 en 8003

Se ejecutó una prueba adicional con un Edge sintético distinto del que ya usa LAB01: `90366a5c-da82-4519-ab0c-6462786b7bab`, ligado sólo a `SucursalCliente.id=17` y `orders:v2:read`. El harness privado `.release/e2e_pedidos_credential_rotation.py` tenía SHA-256 `d2fcd9d94ff2935da38df020077f580b883f646d54c684b475ec961cc2e99e72`, se verificó tras transferirlo y se retiró de `/tmp` después. Usó los comandos reales `issue_pos_v2_credential` y `revoke_pos_v2_credential`; bearer sólo en archivos privados `0600`, jamás en stdout ni en la evidencia.

| Secuencia | Resultado HTTP real |
| --- | --- |
| Primera emisión, página 1 LAB01 | 200, pedido ID 1 y cursor firmado |
| Rotación de la primera, bearer anterior | 401 `unauthorized` |
| Bearer rotado con cursor firmado previo | 200, pedido ID 2 |
| Revocación del rotado, ese bearer | 401 `unauthorized` |
| Reemisión final activa con el mismo Edge y cursor previo | 200, pedido ID 2 |
| Credencial final intentando LAB02 | 403 `forbidden` |

Credenciales públicas: primera `1277df00-faad-4b1c-861b-6c7315df80ca`, rotada y luego revocada `26f75f07-d539-4f57-8295-0be3fce3919c`, final activa `be6e68e2-092e-47a9-8c33-eb4e078cb576`. El bearer final vigente hasta su vencimiento está exclusivamente en `/srv/tcysPedidosSucursales-e2e/shared/credentials/lab01-rotation-e2e-current.token`, dueño `tcyswebe2e`, modo `0600`; su contenido no se incluye aquí. Los pedidos 1–4 quedaron idénticos y el bearer LAB01 original no se modificó. El cursor firmado siguió válido tras rotación porque mantiene filtros y época de purga, sin ampliar el alcance.

La sincronización de un POS aislado con esta credencial final quedó pendiente: la revisión automática de permisos rechazó **antes de conectar por SSH** la transferencia del bearer activo del VPS al archivo local del harness, porque esa salida de credencial y destino no tenían autorización explícita del usuario. La revisión indicó no eludir el rechazo por otra vía. No se leyó ni copió el bearer para este paso. La lectura GET de API demuestra autorización y paginación tras la rotación; el harness POS con transporte simulado demuestra checkpoint e idempotencia locales, pero la combinación POS→API con bearer rotado todavía requiere un método de entrega aprobado. La credencial final activa permanece en staging para esa prueba futura.

## Apéndice del 25 de septiembre de 2026: cruce POS real de laboratorio tras rotación

El dueño autorizó expresamente transferir **sólo una credencial E2E de Pedidos** desde el VPS al archivo privado de la instalación POS de laboratorio para completar el cruce. La autorización no abarca `tcysweb-prod`/8002, otros scopes ni la instalación POS productiva. La denegación anterior sigue documentada arriba como antecedente; esta autorización posterior permitió cerrar la prueba.

La credencial `be6e68e2-092e-47a9-8c33-eb4e078cb576` **no se transfirió**: pertenece al Edge sintético `90366a5c-da82-4519-ab0c-6462786b7bab`, distinto del `instalacion_id` de POS LAB01 `21111111-1111-4111-8111-111111111111`. Para conservar la identidad auditada se rotó en la base aislada `tcysweb_e2e` la credencial original LAB01 `d990995a-977b-4837-8b6e-aa886f0322df`. La nueva credencial pública es `70fd7baf-abd5-480b-b901-e8bd4d234acf`: Edge `21111111-1111-4111-8111-111111111111`, `SucursalCliente.id=17`, único scope `orders:v2:read`. La anterior quedó revocada y la nueva activa exclusivamente para E2E. El servicio consultado fue `tcysweb-e2e` en `127.0.0.1:8003`; no se usó ni modificó el servicio productivo 8002.

El bearer nuevo se generó en un archivo de origen `0600` dentro de un directorio privado `0700` del laboratorio y se entregó directamente a `runtime/e2e/secrets/orders.token` del POS aislado, con su ACL restringida. Se eliminó el archivo temporal de tránsito. Ningún bearer se incluyó en esta evidencia, salida de consola, historial ni Git; no se reutilizó para Central, catálogo, clientes o ventas.

La comprobación cruzada observó `401` al usar desde el POS el bearer anterior ya revocado. Con la nueva credencial, el POS pasó `manage.py check`, autenticó contra Pedidos E2E y sincronizó por HTTP `200`: estado `listo`, agua alta `2026-09-25T13:39:01.557585+00:00`, pedidos de origen `1`, `2` y `3` presentes una sola vez cada uno, sin estado de conciliación. Esto cierra la prueba POS→API de rotación y reinicio; **no** resuelve el procedimiento pendiente de conciliación supervisada después de un `410 retention_gap`. La nueva credencial se conserva activa sólo para pruebas posteriores del laboratorio y deberá revocarse al retirar ese entorno.
