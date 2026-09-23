# Despliegue reproducible en VPS

Esta carpeta define el runtime de `tcysPedidosSucursales` sin modificar el
checkout activo ni depender de `git pull`. El primer despliegue documentado
mantiene la base externa que use la producción real; el cambio a PostgreSQL
local tiene un runbook aparte y requiere autorización. No abre PostgreSQL
local a Internet ni retira Render por sí mismo.

## Perfil de produccion

El VPS usa `DJANGO_ENV=production` (tambien se acepta `vps`). Django falla antes
de arrancar si `SECRET_KEY` falta, es debil, parece un placeholder, si
`DEBUG=True`, si faltan `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS` o si se desactiva
la redireccion HTTPS. Tambien exige `DATABASE_URL`, evitando una caida
silenciosa a SQLite. `SECURE_PROXY_SSL_HEADER` permanece configurado para Nginx.
Cookies de sesion y CSRF son seguras.

HSTS empieza en `0`. Se debe aumentar gradualmente solo despues de validar TLS,
renovacion, subdominios y rollback. `includeSubDomains` y `preload` tambien son
opt-in.

El scheduler embebido y APScheduler fueron retirados. El comando manual de
recordatorios, sus modelos y pantallas se conservan por compatibilidad; su
retirada completa requiere una migracion de esquema y una revision de avisos de
privacidad separadas. No se configura cron ni timer.

## Estructura en el servidor

```text
/home/deploy/apps/tcysPedidosSucursales/
|-- repo.git/                 # cache Git bare; nunca es el checkout servido
|-- releases/
|   |-- <commit-a>/           # codigo inmutable + .venv propio
|   `-- <commit-b>/
|-- shared/
|   |-- .env                  # chmod 600; nunca dentro del release
|   `-- previous_release      # ruta usada por rollback
`-- current -> releases/<commit>
```

Cada release se extrae de un commit SHA-1 completo, instala el lock exacto en
su propio venv y conserva los releases anteriores. El cambio de `current` es
atomico; systemd y Nginx apuntan siempre al symlink estable.

## Archivos canonicos

- `env.example`: inventario de variables, sin secretos reales.
- `requirements.lock`: resolucion exacta validada para CPython 3.13.12.
- `systemd/tcysweb.service`: plantilla canonica para sustituir la unidad activa
  `tcysweb-prod.service` solo durante una activacion autorizada; Gunicorn queda
  en `127.0.0.1:8002` como `deploy`.
- `nginx/tcysweb.conf`: proxy local y cabeceras para HTTPS tras Nginx.
- `runbooks/provision.md`: preparacion no destructiva.
- `runbooks/deploy.md`: build, prueba paralela y activacion autorizada.
- `runbooks/rollback.md`: vuelta a un release ya presente, sin GitHub.
- `runbooks/api_pos.md`: staging, despliegue y rollback de la API HTTPS para el POS.
- `runbooks/retencion.md`: exportación confirmada, dry-run, purga manual aún
  deshabilitada y reconciliación de restores.
- `runbooks/migracion_postgresql_local.md`: ensayo y corte propuesto hacia una
  base PostgreSQL local exclusiva de Pedidos, con rollback dependiente de datos.
- `scripts/ensayar_copia_postgresql.sh` y `scripts/manifesto_postgresql.sql`:
  herramientas de ensayo aislado e inventario técnico sin secretos.

Estos procedimientos no cambian por sí solos la producción actual ni activan
purga. La política de retención requiere aprobación, ensayo en PostgreSQL local,
resolución de pedidos abiertos y auditoría de backups/snapshots antes de
habilitar una tarea destructiva. Los clientes del POS son permanentes y se
almacenan en el backend central separado; no se migran a esta base.

## Dependencias

`requirements.txt` conserva dependencias directas y rangos compatibles.
`requirements.lock` contiene la resolucion desplegable exacta. APScheduler y
`tzlocal` ya no forman parte del runtime.

Comando de regeneracion, ejecutado desde la raiz con `uv` y revisando siempre
el diff antes de aceptar una actualizacion:

```bash
uv pip compile requirements.txt --python-version 3.13.12 --output-file deploy/vps/requirements.lock
```

Verificacion en un venv limpio:

```bash
python3.13 -m venv .venv-lock-check
.venv-lock-check/bin/python -m pip install --no-deps -r deploy/vps/requirements.lock
.venv-lock-check/bin/python -m pip check
.venv-lock-check/bin/python deploy/vps/verify_lock.py
```

No se deben actualizar versiones al regenerar el lock sin una revision y una
ejecucion completa de pruebas.
