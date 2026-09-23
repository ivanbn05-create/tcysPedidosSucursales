# Los Tocayos - Gestión de pedidos

App Django tipo SPA para que sucursales y clientes mayoristas capturen pedidos y matriz los revise e imprima.

## Setup local

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Abrir `http://127.0.0.1:8000/`.

`seed_demo` se reserva para bases locales desechables. Crea cuentas propias para
desarrollo y nunca ejecutes ese comando contra Supabase o una base productiva.

## Flujos

- `/pedidos/`: captura de pedidos con Fetch API, calculadora, resumen responsivo y progreso diario de 5 segmentos. Cada sucursal puede confirmar hasta cinco pedidos por día.
- `/pedidos/historial/`: historial agrupado por macropedido diario, con resumen acumulado y pedidos individuales desplegables (detalle y hora de confirmación).
- `/api/pedidos/crear-item/`: guarda o reemplaza la cantidad del producto en el pedido pendiente.
- `/api/pedidos/eliminar-item/`: elimina item del pedido pendiente.
- `/api/pedidos/confirmar/`: confirma con transacción atómica, agrega el pedido al macropedido de la fecha local, impone el máximo diario de cinco, conserva el rate limit de 1 minuto y aplica la restricción horaria.
- `/api/horarios/`: informa el horario vigente de pedidos (sin auth), mostrado en la pantalla de login.
- `/api/v1/pos/pedidos/`: API HTTPS autenticada y de sólo lectura para que el POS consulte pedidos confirmados mediante cursor estable. El contrato está en [`docs/api_pos_v1.md`](docs/api_pos_v1.md).
- `/api/sesion/heartbeat/`: renueva el arrendamiento de sesión única mientras la página permanece visible.
- `/admin/`: dashboard propio de matriz. Cada fila es un macropedido diario, muestra la hora de su última confirmación y una barra verde-amarillo-rojo de cinco segmentos; la flecha despliega los pedidos que lo integran. El estado, la impresión acumulada, el envío, su reversión y el borrado operan sobre el macropedido completo. El usuario `juanmanuel` solo puede ver e imprimir.
- `/admin/diagnostico/`: sesiones activas y bitácora persistente que correlaciona toques, ejecución JavaScript y respuesta del servidor por ID de intento.
- `/admin/configuracion/`: productos, precios, sucursales/clientes (incluye correo de recordatorios), horarios de pedidos y recordatorios, cuenta admin.
- `/admin/sucursales/imprimir/`: imprime barbacoa, tortilla y consomé acumulados del macropedido confirmado más reciente de las últimas 24 horas por sucursal; los macropedidos enviados se omiten para permitir tomar el confirmado anterior.
- `/django-admin/`: admin nativo de Django.

## Recordatorios por correo (legado)

```bash
python manage.py enviar_recordatorios            # respeta día configurado + recordatorios_habilitados
python manage.py enviar_recordatorios --test      # simula, no manda correos reales
python manage.py enviar_recordatorios --sucursal "Aguilas"
python manage.py enviar_recordatorios --fuerza    # ignora día/recordatorios_habilitados
```

El scheduler embebido fue retirado y ningún worker web inicia envíos automáticos.
El comando se conserva temporalmente para compatibilidad y ejecución manual; no
hay cron ni timer recomendado porque la funcionalidad ya no está activa.
`SCHEDULER_ENABLED` debe permanecer en `False` en todos los entornos.

## Render

Crear un Web Service con PostgreSQL externo (por ejemplo Supabase) y configurar variables de entorno:

```env
DJANGO_ENV=production
DEBUG=False
SECRET_KEY=<REEMPLAZAR_CON_UN_VALOR_ALEATORIO_NUEVO>
ALLOWED_HOSTS=tu-app.render.com
CSRF_TRUSTED_ORIGINS=https://tu-app.render.com
DATABASE_URL=postgresql://<USUARIO>:<PASSWORD>@<HOST>:<PUERTO>/<BASE>?sslmode=require
EMAIL_HOST_USER=<USUARIO_SMTP_OPCIONAL>
EMAIL_HOST_PASSWORD=<PASSWORD_SMTP_OPCIONAL>
DEFAULT_FROM_EMAIL=<REMITENTE_OPCIONAL>
SCHEDULER_ENABLED=False
ACTIVE_SESSION_TTL_SECONDS=600
```

Cada cuenta admite un dispositivo activo. La sesión se respalda en PostgreSQL,
no en la memoria de Render: una segunda conexión se bloquea mientras el
arrendamiento esté vigente, puede tomar control después de validar la contraseña
y lo adopta automáticamente cuando lleva 10 minutos sin actividad. El navegador
renueva cada minuto mientras la página está visible. El valor configurable se
limita de forma segura al rango de 120–840 segundos.

Build command:

```bash
pip install -r requirements.txt && python manage.py collectstatic --noinput
```

Ejecuta migraciones en un paso de release separado, con respaldo previo y sólo
cuando el commit incluya migraciones nuevas:

```bash
python manage.py migrate
```

No ejecutes `seed_demo` como parte de un build o release productivo.

Start command:

```bash
gunicorn proyecto.wsgi
```

## Verificación local

```bash
python manage.py check
python manage.py test pedidos
python manage.py collectstatic --noinput
```

El ticket sólo imprime una fila por producto pedido; no agrega filas vacías de relleno.

La configuración reproducible del VPS, el lock exacto y los procedimientos de
release/rollback están en [`deploy/vps/`](deploy/vps/README.md).

La siguiente etapa, aún sin desplegar, prepara PostgreSQL productivo local del
VPS, exportación manual y retención física de pedidos. La política usa el
primero entre descarga verificada/confirmada y 30 días desde la primera
recepción en el VPS; no incluye maestros ni clientes del POS. Véanse la
[matriz de datos](docs/retencion/matriz_datos.md), el
[runbook de retención](deploy/vps/runbooks/retencion.md) y el
[runbook de migración](deploy/vps/runbooks/migracion_postgresql_local.md).
Ventas y clientes del POS pertenecen al backend central separado; el
[handoff](docs/integracion/handoff_backend_central.md) no declara ese servicio
implementado ni autorizado.
