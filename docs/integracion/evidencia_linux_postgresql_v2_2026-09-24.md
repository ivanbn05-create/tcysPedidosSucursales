# Validación privada de Pedidos POS v2

Fecha: 2026-09-24. Base candidata `8fad56815f856b4286a2f60f488480960e34cde5`. La rama `codex/integracion-pedidos-v2-scoped` añade scopes por Edge/sucursal y corrige el cursor numérico legado y el bloqueo de retención en PostgreSQL. Ningún cambio se aplicó a `tcysweb-prod` ni a 8002.

## Laboratorio

- Copia saneada, sin `.env` ni archivos sin seguimiento, en `/home/deploy/integration-private/pedidos-v2-8fad-lab/source-b7233cc`. El fixture `test_retencion.py` recibió un ajuste de transacción en esa copia para la segunda ejecución PostgreSQL; queda versionado en esta rama.
- Python 3.13.12 y venv aislado en `/home/deploy/integration-private/pedidos-v2-8fad-lab/.venv`. Lock: 12 paquetes exactos en Linux; `pip check` sin conflictos.
- PostgreSQL sólo local: base `tcysweb_e2e_pedidos_check`, rol exclusivo `tcysweb_e2e_pedidos_role`. Archivo `.env` privado `0600`, secreto no documentado. Tras las pruebas, el rol quedó `NOCREATEDB`, la base de test temporal se eliminó y `RETENTION_LOCAL_DB_CONFIRMED=False` en los entornos persistentes. La base sintética migrada se conserva para reproducibilidad.
- No se inició Gunicorn ni se abrió un puerto para esta copia. El laboratorio E2E de 8003 se administra por separado.

## Pruebas

| Comprobación | Resultado |
| --- | --- |
| Prueba POSIX previamente omitida `pedidos.test_mfa.MFASeguridadTests.test_provisionamiento_offline_privado_no_imprime_secretos` | 1/1 en Linux. Archivo `0600`; stdout sin secreto. |
| Comando nuevo de emisión, rotación y revocación `pedidos.test_pos_v2_credentials` | 1/1 en Linux. Bearer sólo en archivo `0600`; hash en DB; revocación durable. |
| `manage.py migrate --noinput` en PostgreSQL exclusivo | Migraciones completas hasta `pedidos.0016` sin error. |
| `manage.py test pedidos.test_retencion --noinput` en PostgreSQL | 21/21. |
| `manage.py test pedidos --noinput` en PostgreSQL | **175/175**, sin omisiones; `check` sin hallazgos. |
| `manage.py makemigrations --check --dry-run` | Sin cambios pendientes. |
| Contrato ejecutable POS `contracts/pedidos-v2/test_contract.py` | 7/7 en Windows, contra fixtures congelados de dev.10. |
| `systemd-analyze verify deploy/vps/systemd/tcysweb.service` | Salida 0; sólo avisos de unidades XFS preexistentes ajenas. |
| `nginx -t` con wrapper aislado que incluye `deploy/vps/nginx/tcysweb.conf` | Sintaxis y test exitosos; no se instaló ni recargó configuración. `sudo nginx -t` de configuración activa también pasó. |
| `manage.py check --deploy` con variables productivas ficticias | Sólo `security.W004`: HSTS 0, decisión pendiente del corte TLS. |

La primera ejecución PostgreSQL detectó diez errores `FOR UPDATE` con `macropedido` nullable, cinco fallos por fixtures de recepción histórica que el trigger PG reemplazaba correctamente, y un restore que intentaba reinsertar un UUID todavía no purgado por ese mismo fixture. La consulta ahora bloquea sólo `Pedido` (`of=("self",)`); las pruebas de retención usan transacciones completas y preparan explícitamente marcas históricas sintéticas antes de probar el trigger real. La segunda suite PostgreSQL pasó completa. El comportamiento de retención no se relajó.

## Contrato y pendientes

V2 sólo acepta un bearer distinto del v1, ligado a Edge UUID, `SucursalCliente.id` y `orders:v2:read`; otra sucursal o scope ausente devuelve 403. Un cursor legado numérico devuelve `410 retention_gap`; un cursor manipulado devuelve 400. V1 y Supabase legacy permanecen. El rate limit de aplicación continúa por worker y requiere control perimetral antes de exposición. La matriz POS UUID/clave ↔ Edge UUID ↔ `SucursalCliente.id` ↔ Branch central aún debe aprobarse; no crear credenciales reales sólo por coincidencia de nombres.

Esto acredita la preparación Linux/PostgreSQL del candidato, no el E2E POS–VPS ni un corte productivo.
