# Los Tocayos - Gestión de pedidos

App Django tipo SPA para que sucursales y clientes mayoristas capturen pedidos y matriz los descargue en Excel.

## Setup local

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Abrir `http://127.0.0.1:8000/`.

## Credenciales demo

La contraseña de cada sucursal/cliente es su nombre más 4 dígitos:

- `Aguilas` / `Aguilas8445`
- `Fortin` / `Fortin9481`
- `Estancia` / `Estancia7608`
- `Brot Nueva Galicia` / `Brot Nueva Galicia0846`
- `Brot CAT` / `Brot CAT7721`
- `Rakebela` / `Rakebela4349`
- Admin: `juancarlos` / `TocayosMO2026`
- Solo impresión: `juanmanuel` / `imprimir`
- Pruebas/debugging: `ivanprueba` / `prueba8989`

Los usernames internos sin espacios también funcionan para pruebas técnicas: `aguilas`, `fortin`, `estancia`, `brot_nueva_galicia`, `brot_cat`, `rakebela`.

## Flujos

- `/pedidos/`: captura de pedido con Fetch API, calculadora y resumen responsivo.
- `/api/pedidos/crear-item/`: guarda o reemplaza la cantidad del producto en el pedido pendiente.
- `/api/pedidos/eliminar-item/`: elimina item del pedido pendiente.
- `/api/pedidos/confirmar/`: confirma con transacción atómica, rate limit de 1 minuto, aviso de total tentativo y restricción horaria (rechaza con 400 fuera de `hora_inicio_pedidos`/`hora_fin_pedidos`).
- `/api/horarios/`: informa el horario vigente de pedidos (sin auth), mostrado en la pantalla de login.
- `/admin/`: dashboard propio de matriz con filtros, detalle, descarga e impresión. El usuario `juanmanuel` solo puede ver e imprimir. Los pedidos se muestran por fecha/hora, no por ID incremental.
- `/admin/configuracion/`: productos, precios, sucursales/clientes (incluye correo de recordatorios), horarios de pedidos y recordatorios, cuenta admin.
- `/admin/pedidos/<id>/descargar/`: descarga Excel y marca como enviado.
- `/django-admin/`: admin nativo de Django.

## Recordatorios diarios por correo

```bash
python manage.py enviar_recordatorios            # respeta día configurado + recordatorios_habilitados
python manage.py enviar_recordatorios --test      # simula, no manda correos reales
python manage.py enviar_recordatorios --sucursal "Aguilas"
python manage.py enviar_recordatorios --fuerza    # ignora día/recordatorios_habilitados
```

Mientras el proyecto vive en Render, este comando se dispara solo vía APScheduler (`pedidos/scheduler.py`, arrancado desde `pedidos/apps.py`), que revisa cada minuto si toca enviar según `Configuracion.hora_envio_recordatorio`. Al migrar a un VPS, esto se reemplaza por un cron nativo llamando al mismo comando (ver CLAUDE.md, sección "Automatización de correos") y se debe poner `SCHEDULER_ENABLED=False`.

## Render

Crear un Web Service con PostgreSQL externo (por ejemplo Supabase) y configurar variables de entorno:

```env
DEBUG=False
SECRET_KEY=your-secret-key-here
ALLOWED_HOSTS=tu-app.render.com
CSRF_TRUSTED_ORIGINS=https://tu-app.render.com
DATABASE_URL=postgresql://user:password@host:port/database
EMAIL_HOST_USER=correos@lostocayos.com
EMAIL_HOST_PASSWORD=contraseña-de-aplicacion-de-gmail
DEFAULT_FROM_EMAIL=Los Tocayos <correos@lostocayos.com>
SCHEDULER_ENABLED=True
```

Build command:

```bash
pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate && python manage.py seed_demo
```

Si tu plan de Render tiene Pre-deploy o Release command, puedes mover ahí la parte de base de datos:

```bash
python manage.py migrate && python manage.py seed_demo
```

En el plan gratuito, dejar `migrate && seed_demo` dentro del Build command evita depender de Shell.

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

El Excel se genera en memoria con `openpyxl`, sin headers, en tres columnas: producto, cantidad y columna vacía. El ticket sólo imprime una fila por producto pedido; no agrega filas vacías de relleno.
