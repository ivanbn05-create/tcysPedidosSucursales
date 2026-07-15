# CLAUDE.md — Los Tocayos / Pedidos Sucursales

Contexto operativo para un agente de IA que va a mantener, actualizar o extender este proyecto. Léelo antes de tocar código. Es un contrato de comportamiento, no documentación general: si una regla no cambia una decisión, no está aquí.

## Qué es esto

App Django monolítica (un solo app `pedidos`) para que sucursales de "Los Tocayos" (negocio de barbacoa/tortillas) y clientes mayoristas capturen pedidos desde el navegador (celular o tablet en sucursal), y un usuario admin (matriz) los revise, filtre, descargue en Excel o los imprima directo en una impresora térmica de tickets (58mm) usando el diálogo de impresión del navegador. No hay frontend framework: es Django + templates + JS vanilla con `fetch`, simulando una SPA solo en la vista `/pedidos/`.

Todo el código, UI y mensajes están en español (México). Mantén ese idioma en cualquier código, commit o texto nuevo.

## Stack y versiones reales

- Django `>=4.2,<6.1` (los comentarios en `settings.py` apuntan a docs de Django 6.0 → asume que corre en 6.0.x salvo que `requirements.txt` diga otra cosa).
- `gunicorn` (server WSGI en prod), `whitenoise` (estáticos, `CompressedManifestStaticFilesStorage`), `dj-database-url` + `psycopg2-binary` (Postgres), `python-decouple` (config por env), `openpyxl` (generación de Excel/ticket), `APScheduler` (disparo del recordatorio diario mientras el proyecto vive en Render, ver sección "Automatización de correos").
- Sin DRF, sin Celery, sin React/Vue. No los introduzcas salvo que se pida explícitamente.
- Python `3.13.12` (`runtime.txt`).

## Mapa de archivos (lo que importa)

```
proyecto/          config del proyecto: settings.py, urls.py (incluye pedidos.urls + django-admin)
pedidos/
  models.py        SucursalCliente, Producto, Precio, Pedido, ItemPedido, Configuracion, LogRecordatorio
  views.py         TODA la lógica de negocio vive aquí (sin services/ separado)
  urls.py          rutas de la app (home, login, pedidos, api/*, admin/*)
  admin.py         Django admin nativo (django-admin/, uso interno/dev, no confundir con /admin/)
  apps.py          arranca el scheduler de recordatorios en ready() (con guardas, ver scheduler.py)
  scheduler.py      APScheduler que dispara enviar_recordatorios (solo mientras viva en Render)
  tickets.py       genera el layout del ticket térmico (Excel vía openpyxl + contexto para HTML)
  seed.py          seed_demo_data() — datos demo idempotentes (usuarios, productos, precios, Configuracion)
  management/commands/seed_demo.py            wrapper de management command sobre seed.py
  management/commands/enviar_recordatorios.py  recordatorio diario por correo (--test, --sucursal, --fuerza)
  tests.py         suite única, cubre el flujo completo end-to-end (ver sección Tests)
  templates/pedidos/
    pedidos.html + static/js/pedidos.js     captura de pedido (SPA-like, fetch), incluye estado de horario
    admin_dashboard.html + static/js/admin.js  panel de matriz (filtros, detalle, descarga)
    admin_configuracion.html                CRUD de productos/precios/sucursales/admin + horarios/recordatorios
    ticket_print.html                       HTML/CSS @page 58mm, window.print() automático
    emails/recordatorio.html + .txt         plantilla del correo de recordatorio (HTML + texto plano)
static/             fuente de CSS/JS/img servidos por WhiteNoise
staticfiles/        salida de collectstatic — generado, NUNCA editar a mano, está en .gitignore
```

No existe capa de "services" ni serializers: las vistas hacen queries, validación y armado de JSON directo. Si el proyecto crece, considera extraer lógica de `views.py` (tiene ~750 líneas), pero no lo hagas de oficio sin que te lo pidan.

## Modelo de datos y reglas de negocio

- **SucursalCliente**: `OneToOneField` a `User` (nullable). `tipo` = `sucursal` | `cliente_mayorista`. El precio **no** se deriva automáticamente del tipo: cada combinación `(producto, sucursal_cliente)` tiene su propio precio en `Precio`. En la demo real hay 38 productos y 222 precios porque algunos productos no aplican a mayoreo.
- **Producto**: además de `nombre` y `nombre_ticket`, guarda `unidad_medida`, `unidad_abreviatura`, `cantidad_por_precio` y `promo_aguilas_martes`. `cantidad_por_precio` permite casos como chile güero: se captura en piezas, pero se cobra con precio por kilo usando 30 piezas por kilo. `promo_aguilas_martes=True` solo aplica a Águilas en martes: por cada 20 capturados agrega 5 extra al ticket sin subir el total.
- **Precio**: vigente = el registro más reciente con `fecha_vigencia <= hoy` para ese `(producto, sucursal_cliente)`. Constraint único por `(producto, sucursal_cliente, fecha_vigencia)`. Actualizar precio desde `/admin/configuracion/` hace `update_or_create` con `fecha_vigencia=hoy` (no crea historial salvo que edites en fechas distintas). `Precio.nombre_ticket` puede sobrescribir el nombre de ticket por sucursal/cliente; se usa para mayoreo (`.M`) en barbacoa, tortilla, grasa, consomé y aguas.
- **Pedido**: estados `pendiente → confirmado → enviado → recibido` (`recibido` no tiene vista que lo dispare todavía, queda para uso futuro/manual). Borrado es lógico (`eliminado=True`), nunca `.delete()` real desde la UI. Solo puede existir **un pedido pendiente por sucursal** (`pedido_pendiente()` lo busca o lo crea). `confirmar_pedido` tiene rate limit de 60s por sucursal para evitar doble confirmación accidental.
- **ItemPedido**: único por `(pedido, producto)` — agregar el mismo producto **reemplaza** la cantidad, no la suma. `subtotal` se recalcula solo en `save()`, usando `cantidad / producto.cantidad_por_precio * precio_unitario`. `cantidad` decimal hasta `999.999`, precios hasta `9999.99`.
- **Producto.etiqueta_ticket / Precio.etiqueta_ticket**: `Precio.nombre_ticket` (si existe para esa sucursal) o `Producto.nombre_ticket` o `Producto.nombre`, truncado a 24 chars — límite real del ancho del ticket térmico, no es capricho.
- **Configuracion**: singleton (usa `Configuracion.get_solo()`, nunca crees un segundo registro a mano). Guarda `hora_inicio_pedidos`/`hora_fin_pedidos` (restricción horaria), `hora_envio_recordatorio`/`dias_recordatorio`/`recordatorios_habilitados`/`email_remitente` (recordatorios). **No** guarda credenciales SMTP — esas viven en variables de entorno (`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`), igual que el resto de secretos del proyecto; ver "Variables de entorno". Se edita desde `/admin/configuracion/` (uso de negocio) y se puede inspeccionar desde `/django-admin/` (solo lectura de facto: no se puede crear un segundo registro ni borrarlo). Cachéada 5 minutos en `django.core.cache` (`pedidos:configuracion`); cualquier código que la modifique debe invalidar `CONFIGURACION_CACHE_KEY`.
- **LogRecordatorio**: bitácora de cada intento de envío del recordatorio diario (`enviado`/`error`/`saltado`), uno por sucursal/cliente por corrida. Solo lectura desde `/django-admin/`, no se crea ni edita a mano.
- **SucursalCliente.email**: opcional; si está vacío esa sucursal/cliente se salta el recordatorio (queda como `saltado` en el log), no truena el comando.

## Autenticación (no es el flujo estándar de Django)

El login (`login_view`) acepta **el nombre visible de la sucursal/cliente** (puede tener espacios y mayúsculas, ej. `"Brot Nueva Galicia"`) buscando `SucursalCliente.nombre__iexact`, y si existe usa el `username` interno real para autenticar. Si no matchea ninguna sucursal, cae a buscar `User.username__iexact` directo (permite entrar con el username técnico, ej. `brot_nueva_galicia`, útil para pruebas). No hay registro público ni recuperación de contraseña.

`is_staff` o `is_superuser` → redirige a `/admin/` (panel propio). Cualquier otro usuario autenticado con perfil de sucursal activo → `/pedidos/`. Un usuario sin `SucursalCliente` activo asociado no puede entrar a capturar pedidos aunque tenga sesión válida.

## Dos "admins" distintos — no los confundas

- `/admin/` y `/admin/configuracion/` → panel **propio** de la app (`admin_dashboard`, `admin_configuracion`), pensado para el usuario de matriz. Aquí se editan productos, precios, sucursales/clientes (incluyendo su correo de recordatorios), horarios de pedidos/recordatorios y la cuenta admin.
- `/django-admin/` → admin nativo de Django (`pedidos/admin.py`), para uso técnico/depuración, no para el usuario de negocio. `Configuracion` y `LogRecordatorio` también están registrados aquí (útil para depurar sin usar el shell), pero el flujo real de negocio para cambiar horarios es `/admin/configuracion/`, no esta pantalla.

## Impresión térmica — cómo funciona realmente

No hay integración ESC/POS ni driver de impresora: `imprimir_pedido` renderiza `ticket_print.html` con `@page { size: {ancho}mm {alto}mm; }` y un `<script>` que llama `window.print()` a los 350ms de cargar. El usuario elige la impresora térmica en el diálogo nativo del navegador. El alto del ticket es dinámico (se calcula sumando alturas de fila según cantidad de items, mínimo 31 filas).

**Punto crítico de mantenimiento**: el layout vive por partida doble.
- `tickets.py` define constantes en unidades de Excel (`TICKET_COLUMN_WIDTHS`, alturas de fila) para `build_ticket_workbook` (descarga `.xlsx`).
- Las mismas proporciones están replicadas en milímetros (`TICKET_COLUMN_WIDTHS_MM`, `TICKET_*_HEIGHT_MM`) para el HTML de impresión.

Si cambias el tamaño del ticket, ancho de columnas o tipografía, **actualiza ambos juegos de constantes** y revisa `pedidos/tests.py::test_login_crear_confirmar_y_excel`, que hace asserts exactos sobre anchos de columna, alturas de fila y `print_area` del workbook. Un cambio a medias rompe el test o desalinea Excel vs. impresión en papel real.

## Restricción horaria de pedidos

Los pedidos solo se aceptan entre `Configuracion.hora_inicio_pedidos` y `Configuracion.hora_fin_pedidos` (default `08:00`–`16:00`, editable desde `/admin/configuracion/` sin redeploy). La validación es doble:

- **Backend (fuente de verdad)**: `views.validar_horario_pedidos()` se llama al inicio de `confirmar_pedido`; fuera de horario regresa `HTTP 400` con `{"success": false, "mensaje": "..."}` (mismo formato que el resto de errores de la API, no uses la clave `error`). `crear_item`/`eliminar_item`/`limpiar_pedido` **no** están restringidos por horario a propósito — se puede seguir armando el pedido, solo no confirmarlo fuera de horario.
- **Frontend (UX)**: `static/js/pedidos.js` llama a `GET /api/horarios/` (sin auth) al cargar `/pedidos/` y cada 60s, pinta el banner `#scheduleStatus` y deshabilita el botón "Confirmar pedido" (`#confirmOrder`) cuando `dentro_horario` es `false`. Esto es solo cosmético — si el JS falla o se manipula, el backend igual rechaza.
- `Configuracion.clean()` exige `hora_inicio_pedidos < hora_fin_pedidos`; no lo relajes para permitir horarios que crucen medianoche sin ajustar también `validar_horario_pedidos()` (hoy asume que inicio < fin dentro del mismo día).

## Automatización de correos (recordatorios diarios)

**Qué es**: `python manage.py enviar_recordatorios` manda un correo (HTML + texto plano) a cada `SucursalCliente` activa con `email` capturado, recordando que confirme su pedido antes de `hora_fin_pedidos`. Registra cada intento en `LogRecordatorio` (`enviado`/`error`/`saltado`).

**Cómo se ejecuta**:
```bash
python manage.py enviar_recordatorios            # respeta día configurado + recordatorios_habilitados
python manage.py enviar_recordatorios --test      # simula: no manda correos reales, solo imprime en consola
python manage.py enviar_recordatorios --sucursal "Aguilas"   # solo esa sucursal/cliente
python manage.py enviar_recordatorios --fuerza    # ignora día configurado y el flag recordatorios_habilitados
```

**Quién lo dispara automáticamente — Nota: Este proyecto está en Render ahora, irá a VPS después. Usa APScheduler para esta implementación (Render). Cuando migremos a VPS, cambiaremos a cron nativo (mismo management command).**

Concretamente: `pedidos/scheduler.py` arranca un `BackgroundScheduler` de APScheduler desde `pedidos/apps.py::ready()` (con guardas para no duplicarse en el watcher de `runserver` ni en comandos de mantenimiento como `test`/`migrate`). Ese scheduler revisa cada minuto si ya es la `hora_envio_recordatorio` configurada y si no se ha enviado hoy (dedupe vía `LogRecordatorio.fecha_envio__date`), y si aplica, llama a `call_command("enviar_recordatorios")` — es decir, dispara el mismo comando de siempre, no una copia de su lógica. `SCHEDULER_ENABLED` (env var, default `True`) apaga esto por completo si hace falta.

Cuando el proyecto se mueva a un VPS: pon `SCHEDULER_ENABLED=False`, agrega un cron nativo que llame al mismo comando (ejemplo abajo), y `pedidos/scheduler.py`/el arranque en `apps.py` quedan sin uso (se pueden dejar o borrar, no son necesarios en VPS):
```bash
0 14 * * 1-5 cd /ruta/del/proyecto && venv/bin/python manage.py enviar_recordatorios
```
Nota que la hora del cron en VPS debe capturarse a mano según `Configuracion.hora_envio_recordatorio`; a diferencia de APScheduler, cron no relee la configuración dinámicamente — si el negocio cambia la hora de envío desde `/admin/configuracion/`, hay que actualizar también el crontab.

**Caveat de concurrencia**: si `gunicorn proyecto.wsgi` llega a correr con más de un worker, cada worker arrancaría su propio scheduler y el recordatorio se mandaría duplicado. El `Procfile` actual no fija `--workers` (gunicorn usa 1 por defecto), así que hoy no pasa, pero si se agrega concurrencia hay que revisar esto antes.

**Credenciales SMTP**: `EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD`/`EMAIL_HOST`/`EMAIL_PORT`/`DEFAULT_FROM_EMAIL` son variables de entorno (`python-decouple`), **no** campos de `Configuracion` — se decidió así para no guardar una contraseña de Gmail en texto plano en la base de datos, siguiendo el mismo patrón que `SECRET_KEY`/`DATABASE_URL`. `Configuracion.email_remitente` solo controla el nombre visible del remitente (ej. `"Los Tocayos <correos@lostocayos.com>"`); si se deja vacío se usa `DEFAULT_FROM_EMAIL`.

**Troubleshooting**:
- *"El correo no se envía"*: revisa que `EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` estén en el entorno (Gmail requiere una "contraseña de aplicación", no la contraseña normal de la cuenta), que la sucursal tenga `email` capturado, que `Configuracion.recordatorios_habilitados` esté activo y que hoy sea uno de los `dias_recordatorio`. Corre `python manage.py enviar_recordatorios --sucursal "Nombre" --fuerza` para aislar el problema y revisa `LogRecordatorio` (o `/django-admin/pedidos/logrecordatorio/`) para ver el `mensaje_error` exacto.
- *"No se envía a la hora esperada"*: en Render, revisa que `SCHEDULER_ENABLED` no esté en `False` y que los logs de arranque muestren `"APScheduler iniciado..."`. En local con `DEBUG=True`, `EMAIL_BACKEND` cae al backend de consola por default (no manda correos reales aunque todo esté "bien configurado") — está pensado así para no mandar correos de prueba sin querer.
- *"El botón de confirmar pedido está deshabilitado"*: es la restricción horaria (ver sección arriba), no un bug — revisa `/api/horarios/` y `Configuracion.hora_inicio_pedidos`/`hora_fin_pedidos`.

## Deploy (Render free tier) — decisiones ya tomadas, no las deshagas sin motivo

El proyecto corre en el plan gratuito de Render, que tiene dos limitaciones conocidas que este proyecto **ya evita**:
1. El filesystem de los web services gratuitos es efímero (se resetea en cada deploy/restart/spin-down) → SQLite local no sirve en prod.
2. El Postgres gratuito de Render expira a los 30 días.

Por eso `DATABASE_URL` en producción apunta a **Postgres externo (Supabase, vía connection pooler)**, no a SQLite ni al Postgres de Render. `settings.py` solo cae a SQLite si `DATABASE_URL` está vacío (uso local). Si algún día cambian de proveedor de DB, ese es el motivo original — no lo "arregles" apuntando de vuelta a SQLite en prod.

También ten presente el cold start: los servicios gratuitos se duermen tras 15 min de inactividad y tardan 30–60s en responder la siguiente petición. No es un bug si una demo tarda en cargar la primera vez.

Comandos de Render (de `README.md`):
- Build recomendado en plan gratuito: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate && python manage.py seed_demo`
- Si el plan tiene Release/pre-deploy: puedes mover ahi `python manage.py migrate && python manage.py seed_demo`
- Start: `gunicorn proyecto.wsgi` (`Procfile` ya lo define igual)

**Riesgo real a vigilar**: `seed_demo` corre en cada deploy y es idempotente en estructura, pero **resetea la contraseña del admin a `admin123`** y la de cada sucursal a su propio nombre (`set_password` incondicional en `seed.py`). Si en algún momento el negocio cambia contraseñas desde `/admin/configuracion/`, el siguiente deploy las vuelve a pisar. Antes de tocar el pipeline de deploy o `seed.py`, confirma con el usuario si eso sigue siendo intencional (es razonable en fase demo, peligroso si ya hay contraseñas reales de operación).

## Variables de entorno

Vía `python-decouple`, leídas de `.env` (gitignored) o del entorno de Render: `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`, `LOG_LEVEL`, `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`. El `.env` local de este repo trae credenciales reales de Supabase — **nunca las imprimas, loguees, commitees ni las incluyas en respuestas**; si necesitas referirte a ellas, hazlo por nombre de variable, no por valor.

Para recordatorios por correo (ver esa sección arriba): `EMAIL_BACKEND` (default: consola si `DEBUG=True`, SMTP si no), `EMAIL_HOST` (default `smtp.gmail.com`), `EMAIL_PORT` (default `587`), `EMAIL_USE_TLS` (default `True`), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (contraseña de aplicación de Gmail, **nunca** la contraseña normal de la cuenta), `DEFAULT_FROM_EMAIL`. Trátalas con el mismo cuidado que `SECRET_KEY`/`DATABASE_URL`: nunca las imprimas ni las loguees.

Para el scheduler (ver esa sección arriba): `SCHEDULER_ENABLED` (default `True`; ponlo en `False` cuando el proyecto pase a VPS con cron nativo).

## Comandos habituales

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo          # datos demo idempotentes, ver riesgo de contraseñas arriba
python manage.py runserver

python manage.py check
python manage.py test pedidos       # única suite de tests, corre todo el flujo end-to-end
python manage.py collectstatic --noinput

python manage.py enviar_recordatorios --test    # simula el recordatorio diario sin mandar correos
```

No hay linter, `pyproject.toml`, `pre-commit` ni CI configurados en el repo. Si agregas herramientas de calidad, hazlo explícito en el README, no asumas que ya existen.

## Tests — qué cubren y qué no romper

`pedidos/tests.py` es la única fuente de verdad de comportamiento esperado: login por nombre de sucursal, crear/reemplazar item, confirmar pedido, descarga de Excel (con asserts exactos de dimensiones/estilos del ticket), vista de impresión, ocultamiento de precios unitarios en `/pedidos/` (hay un test que verifica explícitamente que `precio_unitario` y montos con `$` **no** aparecen en esa vista — es una regla de negocio, no un detalle visual), control de acceso admin vs. no-admin, y el flujo completo de `/admin/configuracion/` (crear producto, precio, sucursal, cambiar contraseña). Antes de dar por buena cualquier modificación en `views.py`, `models.py` o `tickets.py`, corre `python manage.py test pedidos`.

`PedidoFlowTests.setUp()` deja el horario de pedidos completamente abierto (`abrir_horario_completo()`) para que esos tests no dependan de la hora real en que corre la suite. `RestriccionHorariaTests` prueba explícitamente el bloqueo dentro/fuera de horario y el endpoint `/api/horarios/` usando ventanas de tiempo calculadas respecto a "ahora" (no horas fijas), precisamente para no ser flaky según la hora del día. `EnviarRecordatoriosCommandTests` prueba el management command (`--test` no manda correos reales, `--fuerza` ignora día/`recordatorios_habilitados`, sucursales sin correo quedan `saltado`) usando el backend de correo en memoria de Django (`django.core.mail.outbox`). Si tocas horarios o recordatorios, mantén ese patrón — no hardcodees horas absolutas en aserciones nuevas.

## Convenciones y cosas que no hacer sin que te lo pidan

- No introduzcas Django REST Framework, GraphQL ni un frontend framework — el patrón actual es vistas + `fetch` + JSON a mano.
- No quites el rate limit de 60s en `confirmar_pedido`; es una protección real contra doble clic/doble ticket.
- No expongas `precio_unitario`/`subtotal` en la vista `/pedidos/` (uso de sucursal) — solo el admin ve precios.
- No mezcles `/admin/` (propio) con `/django-admin/` (nativo) al agregar features; son públicos distintos.
- Al tocar `tickets.py` o `ticket_print.html`, cambia ambos y corre los tests de inmediato.
- Hay 4 migraciones (`0001_initial`, `0002_producto_nombre_ticket_activo`, `0003_configuracion_sucursalcliente_email_logrecordatorio`, `0004_producto_unidades_calculo`); revisa el diff de cualquier migración nueva antes de aplicarla, el historial es corto y fácil de romper.
- `staticfiles/` es generado (WhiteNoise + manifest con hashes); si necesitas cambiar CSS/JS/img, edita en `static/` y corre `collectstatic`, nunca edites `staticfiles/` directo.
- No guardes credenciales SMTP (usuario/password de Gmail) en `Configuracion` ni en ningún modelo — van por variable de entorno, igual que `SECRET_KEY`/`DATABASE_URL`.
- No quites la validación de horario de `confirmar_pedido` (`validar_horario_pedidos()`) ni la muevas a un middleware genérico que también bloquee `crear_item`/`eliminar_item` — es una decisión explícita que solo bloquea la confirmación, no la captura.
- Al modificar horarios/recordatorios en código, invalida `CONFIGURACION_CACHE_KEY` (`django.core.cache.cache.delete(...)`) o los cambios tardan hasta 5 minutos en reflejarse por el caché de `get_configuracion()`.
- Si se agrega más de un worker de gunicorn, revisa primero el caveat de duplicado en `pedidos/scheduler.py` antes de hacer deploy.
