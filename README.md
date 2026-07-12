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

La contraseña de cada sucursal/cliente es exactamente su nombre:

- `Aguilas` / `Aguilas`
- `Fortin` / `Fortin`
- `Estancia` / `Estancia`
- `Brot Nueva Galicia` / `Brot Nueva Galicia`
- `Brot CAT` / `Brot CAT`
- `Rakebela` / `Rakebela`
- Admin: `admin` / `admin123`

Los usernames internos sin espacios también funcionan para pruebas técnicas: `aguilas`, `fortin`, `estancia`, `brot_nueva_galicia`, `brot_cat`, `rakebela`.

## Flujos

- `/pedidos/`: captura de pedido con Fetch API, calculadora y resumen responsivo.
- `/api/pedidos/crear-item/`: agrega o acumula producto en el pedido pendiente.
- `/api/pedidos/eliminar-item/`: elimina item del pedido pendiente.
- `/api/pedidos/confirmar/`: confirma con transacción atómica y rate limit de 1 minuto.
- `/admin/`: dashboard propio de matriz con filtros, detalle y descarga.
- `/admin/pedidos/<id>/descargar/`: descarga Excel y marca como enviado.
- `/django-admin/`: admin nativo de Django.

## Render

Crear un Web Service con PostgreSQL y configurar variables de entorno:

```env
DEBUG=False
SECRET_KEY=your-secret-key-here
ALLOWED_HOSTS=tu-app.render.com
CSRF_TRUSTED_ORIGINS=https://tu-app.render.com
DATABASE_URL=postgresql://user:password@host:port/database
```

Build command:

```bash
pip install -r requirements.txt && python manage.py collectstatic --noinput
```

Pre-deploy o release command:

```bash
python manage.py migrate && python manage.py seed_demo
```

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

El Excel se genera en memoria con `openpyxl`, sin headers, en tres columnas: producto, cantidad y columna vacía.
