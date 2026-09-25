# Handoff Pedidos → agentes 1 (integrador) y 3 (POS)

Estado: candidata privada, **no fusionar, publicar ni desplegar** sin gates de
Production 1.0. La API productiva actual no fue modificada.

## Reconciliación exacta

Base propia `8fad56815f856b4286a2f60f488480960e34cde5`. Se leyó el
checkout aislado limpio de agente1 en
`C:/Users/Srv1/Documents/ChatGPT/VPS Los Tocayos/.release/pedidos-8fad-integration`.
Su historia es lineal: `16a9cc0` (credencial v2 Edge/sucursal), `b7233cc`
(locks/fixtures PostgreSQL), `625ce98` (evidencia Linux y ajuste de test),
`63f69d5` (E2E 410 sintético), `bbe9963` (evidencia credenciales/recuperación)
y `2c7c2ac` (rotación LAB01 y cruce POS real). La rama nueva parte **de
`2c7c2ac`**, sin cherry-picks repetidos ni cambios en el checkout de agente1.
`625ce98` está incluido por ascendencia; `2c7c2ac` sólo añade evidencia.

## Contrato definitivo candidato

- `GET /api/v2/pos/pedidos/` mantiene forma de página, UUID de pedido y
  cursor firmado. `410 retention_gap` sigue siendo error, no 200 vacío.
- Credenciales nuevas: bearer aleatorio sólo hash SHA-256 en DB, Edge UUID,
  **POS branch UUID**, `SucursalCliente.id` exacta, scope `orders:v2:read`,
  expiración/revocación/rotación. Headers `X-POS-Edge-ID` y
  `X-POS-Branch-ID` obligatorios para las nuevas; sólo cotejan la identidad
  declarada, no reemplazan el bearer. Producción rechaza credenciales v2
  antiguas sin UUID POS. V1 conserva su allowlist únicamente para rollback.
- Arboledas es la única sucursal inicial. Agente1 debe aprobar la matriz
  `POS UUID ↔ Edge UUID ↔ sucursal Central ↔ SucursalCliente.id` con datos
  reales leídos en modo sólo lectura; este repo **no inventa** IDs productivos.
  La fixture de Central leída sin cambios identifica Arboledas como Branch
  `e1d9a253-7b96-4c32-8376-00fb8f6d8bdb`, pero conserva
  `pedidos_sucursal_id=null`; ese UUID **no** se supone igual al UUID de
  sucursal POS ni sustituye el ID pendiente de Pedidos.
- Recuperación: comandos `preparar_recuperacion_pos_v2` y
  `cerrar_recuperacion_pos_v2`, acta `PosRetentionRecovery`, ZIP privado,
  schemas/fixture y [procedimiento](retention_gap_recovery_v1.md). No hay
  endpoint web de confirmación ni avance automático del cursor. Un tombstone
  sin contenido custodio o presencia previa verificable queda
  `intervencion_manual`.
- Migraciones nuevas: `pedidos.0017_pos_branch_binding` (nullable para E2E
  anterior; exigido por credencial nueva/perfil productivo) y
  `pedidos.0018_pos_retention_recovery` (metadatos/hashes, no cuerpos).

## Tareas para agente1

1. Revisar y aprobar el protocolo de freeze, el canal privado del ZIP/actas,
   la custodia de exportaciones y el mecanismo de autenticidad del acuse Edge.
   SHA de archivo y UUID prueban integridad/cobertura, **no** identidad del
   firmante ni existencia física de un pedido purgado.
2. Aportar ID real `SucursalCliente.id` de Arboledas y completar matriz con
   UUID POS/Edge/Central, sin inferir por nombre. No emitir bearer real aún.
3. Ejecutar ensayo Linux PostgreSQL aislado de `0017`/`0018`, tests POSIX
   nuevos, restore/conteos/FK/índices/secuencias y prueba de cambio durante
   freeze. El ensayo E2E de `625ce98`/`2c7c2ac` **precede** estas migraciones
   y no sustituye su validación.
4. Cerrar la separación OS/sudo del usuario web/operador y el rate limit
   perimetral multiproceso; la plantilla actual corre web como `deploy`.

## Tareas para agente3

1. Enviar los headers Edge/POS para credenciales nuevas; conservar exactamente
   el mismo cursor al rotar bearer o ante 401/timeout. No derivar sucursal
   desde nombres ni probar IDs alternos.
2. Implementar importación idempotente del ZIP privado por
   `Pedido.codigo_publico`, validar hashes de ZIP y órdenes, incorporar desde
   archivo custodio los purgados ausentes, y producir acuse de UUID presentes
   e irresueltos. El fixture JSON es sintético/ilustrativo; sus hashes no
   corresponden a un ZIP real.
3. Sólo con acta `completada` y autorización humana, persistir en **una**
   transacción local el nuevo `desde=hasta` anterior, `cursor=null`, ID de
   recuperación y SHA del snapshot. Si no, mantener `RECONCILIACION` y el
   cursor anterior. Ensayar retry, reinicio, ack perdido y payload manipulado.

## Pruebas y límites de esta ronda

- Windows, Python **3.13.12** exacto: `manage.py test pedidos` detectó 185:
  180 pasaron, 5 omitidas por POSIX; dirigidas de API/credencial/recovery sin fallos.
- `manage.py check`, `makemigrations --check --dry-run`, `pip check` y
  `verify_lock.py` pasan; lock con 13 paquetes en Windows.
- `check --deploy` con valores productivos **ficticios**: sólo `security.W004`
  por HSTS=0. Bandit de código nuevo sin hallazgos; `pip-audit` del lock sin
  vulnerabilidades conocidas. No se usaron secretos productivos.
- **No ejecutado en esta rama:** tests POSIX de recuperación, migraciones
  `0017`/`0018` en PostgreSQL real del VPS, E2E nuevo con POS dev.10,
  inventario/dump/restore de la base externa real ni corte. La evidencia
  Linux previa `625ce98` sólo cubre hasta `0016`.

Validación Linux pendiente, **sólo** sobre copia aislada y rol PostgreSQL de
ensayo con permiso temporal de crear la base de test (revocarlo después):

```bash
LAB_RELEASE='<RUTA_RELEASE_PRIVADO_SIN_ENV>'
LAB_ENV='<ENV_LAB_POSTGRESQL_0600_NO_PRODUCTIVO>'
test -d "$LAB_RELEASE" && test -x "$LAB_RELEASE/.venv/bin/python"
test -f "$LAB_ENV" && test -r "$LAB_ENV"
sudo systemd-run --unit=tcys-pedidos-p10-tests --wait --pipe --collect \
  --property=Type=oneshot --property=User=tcyswebe2e \
  --property="WorkingDirectory=$LAB_RELEASE" \
  --property="EnvironmentFile=$LAB_ENV" \
  /usr/bin/env DJANGO_ENV=test DEBUG=True \
  EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend \
  RETENTION_PURGE_ENABLED=False SCHEDULER_ENABLED=False \
  "$LAB_RELEASE/.venv/bin/python" manage.py test \
  pedidos.test_pos_recovery pedidos.test_pos_v2_credentials \
  pedidos.test_api_pos pedidos.test_retencion --noinput
```

Antes de ejecutar, comprobar que `LAB_ENV` apunta **inequívocamente** a la
base/rol de ensayo y no a `tcysweb-prod` ni al origen externo, sin imprimir
su URL; no usar `.env`/overrides productivos ni `source`. El ensayo completo
del corte sigue el runbook PostgreSQL y añade E2E POS real, restore y freeze.

## Corte y rollback

Seguir [migración PostgreSQL local](../../deploy/vps/runbooks/migracion_postgresql_local.md)
para inventario, dump consistente, restore a base exclusiva, manifest de
tablas/constraints/secuencias, ventana de freeze comprobada, ensayo con
escrituras concurrentes, pruebas de aplicación/POS y aprobación humana. La
fase 6 de `deploy.md` por sí sola **no** realiza un corte seguro de base.
Antes de abrir escrituras locales, un fallo permite volver al código/unidad y
al origen congelado. Después de la primera escritura local, no se vuelve a
`DATABASE_URL` externo sin delta bidireccional completo: se congela y repara
hacia delante sobre la base local. Conservar origen y snapshots durante la
ventana aprobada, sin dos bases escritoras.

Permanecen por transición/rollback: API POS v1, configuración Supabase legacy,
checkout/unidad previa y ledger de retención. Candidatas a retirar **después**
del corte coordinado y validación del agente3: allowlist v1, ruta v1 y
configuración de base externa, nunca antes. HSTS requiere TLS productivo
definitivo, redirecciones y rollback probados. Revisar además Nginx activo,
`ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, IP real, cuentas demo reales y alcance
del grupo de impresión; estos son gates, no pruebas ya cerradas.

Hallazgos concretos de seguridad a cerrar en VPS: `CACHES` sigue siendo
`LocMemCache`, por lo que el límite de la API por token no es un control
compartido entre los tres workers; añadir
`limit_req` perimetral o equivalente compartido y ensayar 429 desde varios
workers/IP. `SECURE_PROXY_SSL_HEADER` confía en `X-Forwarded-Proto`; Gunicorn
debe seguir sólo en loopback y Nginx debe sobrescribir ese header, no pasarlo
desde el cliente. La plantilla Nginx versionada aún es HTTP/80 y no acredita
TLS/HSTS activo. `ALLOWED_HOSTS` y `CSRF_TRUSTED_ORIGINS` exigen valores en
producción, pero su lista real requiere revisión sin revelar secretos. El
grupo de impresión puede abrir dashboard e imprimir pedidos de **todas** las
sucursales por `@dashboard_required`; no tiene mutaciones `@admin_required`,
pero esa visibilidad global debe ser aprobada o acotada antes de usarlo.
Inventariar cuentas de `seed_demo` directamente en producción con consulta
read-only autorizada y deshabilitarlas si existen; no ejecutar `seed_demo`.
