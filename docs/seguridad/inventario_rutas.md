# Inventario y revisión de superficie HTTP

Estado revisado: código candidato de la rama `codex/retencion-exportacion-vps`, antes de publicación/fusión/despliegue. **No describe lo que ya está activo en producción.** Fuentes: `proyecto/urls.py`, `pedidos/urls.py`, `pedidos/views.py`, `pedidos/mfa.py`, `pedidos/middleware.py`, `pedidos/api_pos.py`, `pedidos/admin.py` y `proyecto/settings.py`. Son **39 rutas explícitas** de Pedidos más el árbol generado por Django Admin bajo `/django-admin/`.

## Rutas públicas y sesión humana

| Ruta | Método aceptado actualmente | Clasificación y efecto |
| --- | --- | --- |
| `/` | sin restricción explícita | Pública; redirige según sesión/rol. |
| `/privacidad/` | sin restricción explícita | Pública; aviso de privacidad. |
| `/login/` | GET, POST | Pública; contraseña con Django Axes (5 fallos, cooldown 15 min por usuario/IP) y mensajes de fallo genéricos. Para privilegiados deja reto MFA pendiente sin sesión autenticada; `force_takeover` exige la prueba MFA previa. |
| `/mfa/login/` | GET, POST | Reto TOTP preautenticado tras contraseña válida; pendiente expira a los 300 s. Un código de recuperación consumido conduce al enrolamiento, no concede sesión. |
| `/mfa/revalidar/` | GET, POST | Cuenta privilegiada ya autenticada y verificada; exige **contraseña y TOTP** para refrescar autorización de acciones admin por 300 s. `next` admite únicamente ruta local validada. |
| `/mfa/recuperacion/` | GET, POST | Preautenticada sólo tras contraseña y código de recuperación de un uso; crea y confirma un nuevo TOTP antes de permitir sesión, rota códigos y revoca sesión anterior. |
| `/logout/` | **POST** | Pública; revoca sesión con CSRF. GET responde 405 y no cierra sesión. |
| `/api/horarios/` | sin restricción explícita | Pública; horario operativo sin datos de pedido. `Configuracion.get_solo()` puede crear el singleton si aún no existe, incluso en GET. |

`/login/` no usa parámetro `next`; `/mfa/revalidar/` valida ruta local mediante `url_has_allowed_host_and_scheme` y rechaza `//`. No se observó *open redirect* en estas vistas. `MFAEnforcementMiddleware` bloquea una sesión privilegiada sin OTP válido incluso en rutas no decoradas. La huella de credenciales almacenada en sesión se deriva con HMAC del password hash, actividad, flags de rol, grupos y versión MFA; no guarda esos valores en claro y cambios en ellos invalidan la verificación. Las cookies de sesión/CSRF son Secure en producción, SameSite Lax; la cookie de sesión es HttpOnly, dura una hora, expira al cerrar navegador y se renueva con cada solicitud. Esta configuración es del **candidato**, pendiente de validación en el entorno productivo.

## Cuenta de sucursal/cliente autenticada

| Ruta | Método/rol | Control de objeto observado |
| --- | --- | --- |
| `/pedidos/` | autenticada; cuenta de sucursal activa | `sucursal_para_usuario(request.user)` fija el alcance; no acepta ID de otra sucursal. |
| `/pedidos/historial/` | autenticada; sucursal activa | `macropedidos_historial_usuario(sucursal)` filtra por sucursal. |
| `/pedidos/historial/dia/<uuid:codigo_publico>/imprimir/` | autenticada; sucursal activa | `get_object_or_404(macropedidos_historial_usuario(sucursal), codigo_publico=...)`: UUID ajeno no debe revelar objeto. |
| `/pedidos/historial/<uuid:codigo_publico>/imprimir/` | autenticada; sucursal activa | `get_object_or_404(pedidos_historial_usuario(sucursal), codigo_publico=...)`: UUID ajeno no debe revelar objeto. |
| `/api/pedidos/crear-item/` | POST, autenticada | Rechaza admin; toma sucursal desde usuario. Sólo productos con precio vigente para esa sucursal. |
| `/api/pedidos/eliminar-item/` | POST, autenticada | Toma sucursal desde usuario; exige `ItemPedido.pedido` igual al pedido pendiente de esa sucursal. |
| `/api/pedidos/limpiar/` | POST, autenticada | Sólo limpia el pedido pendiente de la sucursal del usuario. |
| `/api/pedidos/confirmar/` | POST, autenticada | Rechaza admin; sólo confirma el pedido pendiente de la sucursal del usuario. |
| `/api/pedidos/log-cliente/` | POST, autenticada | Recibe eventos de esa sesión; conserva compatibilidad con el payload legacy de un evento. No es ruta de descarga/exportación. |
| `/api/sesion/heartbeat/` | POST, autenticada | Renueva/valida la sesión por `SesionUnicaMiddleware`; no recibe ID de sucursal. |

La comprobación de usuario de sucursal está en `sucursal_para_usuario()`; una cuenta staff o del grupo de impresión se redirige al dashboard en las páginas de pedido/historial. En `eliminar_item()` una cuenta sin sucursal recibe 400 (frente a 403 de otras rutas), diferencia de estado a uniformar si importa evitar inferencias.

## API POS de servicio

| Ruta | Método/rol | Alcance y transición |
| --- | --- | --- |
| `/api/v1/pos/pedidos/` | GET, Bearer token | Contrato v1. **Conservar** durante transición, agente3 y rollback; no contiene UUID público ni señal de brecha. |
| `/api/v2/pos/pedidos/` | GET, Bearer token | Contrato v2: `codigo_publico` UUID, cursor firmado y `410 retention_gap` ante intervalo de purga/cursor obsoleto. No sustituir v1 sin validación E2E y autorización de corte. |

Ambas rutas son sólo lectura, exigen token configurado, HTTPS cuando `POS_API_REQUIRE_HTTPS`, ventana temporal, límites de página y allowlist `POS_API_ALLOWED_SUCURSAL_IDS`; consultan sólo sucursales de tipo `SUCURSAL` y pedidos confirmados no eliminados. La allowlist es **global para todos los tokens**, no una asignación de sucursales por token. Si hay consumidores de distintos alcances, compartir esta configuración concede a cualquiera de los tokens acceso a toda la unión permitida: falta política y prueba de alcance por credencial. El rate limit usa la caché `LocMemCache`, por lo que es por proceso y no global en Gunicorn; no tomarlo como protección robusta frente a abuso distribuido.

La revisión encontró que v2 consultaba los extremos temporales globales de `RegistroPurga`: un token que pedía una sucursal podía recibir `410` causado por otra y una purga mixta podía cubrir fechas sin pedido purgado. La corrección candidata usa `PedidoPurgado.sucursal_cliente_id` y la `fecha_confirmacion` exacta para acotar tanto la brecha como la época del cursor al filtro solicitado; además excluye del alcance de purga a mayoristas y firma en el cursor una huella opaca, sin exponer el ID global del recibo. Hay pruebas de código para purga ajena, mayorista, huecos entre fechas, cursor viejo y ausencia de ID global; faltan resultados E2E contra POS/VPS. V1 se mantiene sin esa señal por compatibilidad, lo que limita su uso tras activar purgas.

## Panel de negocio y Django Admin

`admin_required` admite cualquier `is_staff` o `is_superuser` (sin permiso de modelo/objeto), pero ahora exige sesión MFA válida y contraseña+OTP recientes para los métodos inseguros. `dashboard_required` admite esos usuarios y el grupo `Operador de impresion`, con sesión MFA válida. **El grupo de impresión puede ver el dashboard y tickets de todas las sucursales**, pero no `/admin/datos/`, `/admin/configuracion/`, `/admin/diagnostico/` ni mutaciones, según decoradores. Confirmar que esa visibilidad amplia es la autorizada.

| Ruta | Método/rol | Efecto |
| --- | --- | --- |
| `/admin/` | GET, `dashboard_required` | Dashboard global de macropedidos; filtros de consulta. |
| `/admin/pedidos/nuevo/` | GET, `admin_required` | Selección de sucursal activa para crear pedido desde el panel; `sucursal` se valida contra la lista. |
| `/admin/api/pedidos/crear-item/` | POST, `admin_required` | Crea ítem en la sucursal activa indicada en JSON. |
| `/admin/api/pedidos/eliminar-item/` | POST, `admin_required` | Elimina ítem del pedido pendiente de la sucursal indicada. |
| `/admin/api/pedidos/limpiar/` | POST, `admin_required` | Limpia pedido pendiente de la sucursal indicada. |
| `/admin/api/pedidos/confirmar/` | POST, `admin_required` | Confirma pedido de la sucursal indicada; omite horario/espera por diseño administrativo. |
| `/admin/datos/` | GET, `admin_required` | Analítica global de pedidos/precios; datos sensibles de negocio. |
| `/admin/configuracion/` | GET, POST, `admin_required` | Productos, precios, sucursales, usuarios/password y configuración; el POST exige OTP reciente por decorador y middleware. |
| `/admin/diagnostico/` | GET, superusuario con MFA | Eventos y sesiones de todas las sucursales, IP/user-agent y detalle; staff no superusuario recibe 403. |
| `/admin/aguas/imprimir/` | GET, `dashboard_required` | Ticket/reporte agregado de aguas. |
| `/admin/sucursales/imprimir/` | GET, `dashboard_required` | Ticket/reporte agregado por sucursales. |
| `/admin/macropedidos/<int:macropedido_id>/imprimir/` | GET, `dashboard_required` | Ticket de cualquier macropedido no eliminado; sin filtro por sucursal, acorde al rol global. |
| `/admin/macropedidos/<int:macropedido_id>/marcar-enviado/` | POST, `admin_required` | Cambia estado a enviado; `require_POST`. |
| `/admin/macropedidos/<int:macropedido_id>/revertir-enviado/` | POST, `admin_required` | Revierte a confirmado; `require_POST`. |
| `/admin/macropedidos/<int:macropedido_id>/eliminar/` | POST, `admin_required` | Borrado lógico; `require_POST`. |
| `/admin/pedidos/<int:pedido_id>/imprimir/` | GET, `dashboard_required` | Ticket de cualquier pedido no eliminado; sin filtro por sucursal, acorde al rol global. |
| `/admin/pedidos/<int:pedido_id>/marcar-enviado/` | POST, `admin_required` | Cambia estado; `require_POST`. |
| `/admin/pedidos/<int:pedido_id>/revertir-enviado/` | POST, `admin_required` | Revierte estado; `require_POST`. |
| `/admin/pedidos/<int:pedido_id>/eliminar/` | POST, `admin_required` | Borrado lógico; `require_POST`. |

`/django-admin/` es **un árbol adicional**, no una sola página. Incluye login/logout/password-change, índices, vistas de alta/cambio/borrado/historial y acciones de los modelos registrados `SucursalCliente`, `Producto`, `Precio`, `MacroPedido`, `Pedido` (inline `ItemPedido`), `Configuracion`, `LogRecordatorio`, `SesionActiva` y `EventoCliente`, además de `auth.User`/`auth.Group`. El candidato usa `OTPAdminSite`: exige OTP al entrar y estampa la verificación sólo después de un login real; middleware exige OTP reciente para POST de Django Admin. El `TOTPDevice` se desregistró de Admin para impedir crear/cambiar semillas por esa vía. `LogRecordatorio`, `SesionActiva` y `EventoCliente` sólo son visibles a superusuario y prohíben alta/cambio/borrado; sus columnas de Admin omiten token y cargas históricas. Django Admin sigue exigiendo permisos Django por modelo; un superusuario puede editar otros modelos sin las reglas específicas del panel de negocio. Validar que ese acceso técnico privilegiado es intencional.

## Exportación/retención y rutas legacy

No hay URL HTTP registrada para generar, descargar o confirmar un export de retención, ni para purga o conciliación. Actualmente son **management commands** (`generar_exportacion_retencion`, `confirmar_exportacion_retencion`, `purgar_retencion`, etc.), de modo que cambiar una URL o adivinar UUID de lote no permite invocarlos por HTTP ni descargar archivos. `generar_exportacion()` rechaza destinos dentro del release, `STATIC_ROOT`, `MEDIA_ROOT` o directorios estáticos y, en POSIX, exige directorio propio/privado y ZIP 0600; además debe revisarse la publicación real de rutas por Nginx en el VPS. Tampoco existe `/admin/pedidos/<id>/descargar/`: impresión y `marcar-enviado` son operaciones distintas y no constituyen prueba de exportación verificada. Si se crea una descarga web futura, debe tener autorización por rol/objeto, expiración, enlace no adivinable, control de acceso al archivo fuera de `STATIC_ROOT`/`MEDIA_ROOT`, MFA reciente, auditoría y pruebas IDOR.

Conservar durante transición y rollback:

- `/api/v1/pos/pedidos/`, porque el consumidor anterior/agente3 puede depender del contrato v1 y v2 requiere E2E.
- Las rutas humanas de pedidos e impresión existentes, incluidos POST de estado individual y macro, porque siguen siendo operación y rollback válidos. No inferir obsolescencia por la introducción de v2/retención.
- La conectividad PostgreSQL externa/Supabase legacy es **configuración de base de datos, no ruta HTTP**. No retirarla ni cambiar `DATABASE_URL` hasta completar ensayo, corte y estrategia de datos/rollback.
- El payload legacy de `/api/pedidos/log-cliente/` mientras haya clientes web antiguos; medir uso antes de retirarlo.

Candidatas a retirar **sólo después** de evidencia de cero consumidores y aprobación del corte: v1; compatibilidad de payload único de `log-cliente`; rutas administrativas duplicadas por `/django-admin/` si existe sustituto operacional y control de permisos equivalente; acceso directo legacy a Supabase. Ninguna se elimina en esta revisión. La ruta inexistente `/admin/pedidos/<id>/descargar/` no requiere eliminación.

## Cobertura de pruebas y límites del inventario

En el candidato se añadieron pruebas negativas de IDOR por UUID/ID, roles de sucursal/impresión, GET no destructivo, logout POST-only, CSRF y rutas de exportación/purga inexistentes (`pedidos/test_seguridad_web.py`). Las pruebas de MFA (`pedidos/test_mfa.py`) cubren contraseña sin OTP, OTP erróneo/reutilizado y bloqueo, cuenta staff sin dispositivo, acceso directo y Django Admin, cambio de rol/password, step-up, sesión reemplazada, recuperación de un uso, errores genéricos y CSRF. `pedidos/test_seguridad_admin.py` cubre desregistro de TOTP y minimización de Admin/logs. Las pruebas de retención cubren ZIP manipulado, destino público/release, ticket sin ruta HTTP y doble confirmación (`pedidos/test_retencion.py`); las de POS v2 cubren `410` propio, otra sucursal, mayorista, huecos, cursor opaco y UUID ajeno (`pedidos/test_api_pos.py`). Este inventario describe **cobertura implementada**, no certifica por sí solo el resultado de la suite completa ni el comportamiento productivo; el integrador debe adjuntar el resultado con Python 3.13.12 y el entorno actualizado.

Límites aún por resolver antes de corte: no hay scopes distintos por token POS (todos comparten la allowlist global; declarar una sola cuenta de servicio/alcance o diseñar credenciales por sucursal), el rate limit POS usa caché por proceso, `Configuracion.get_solo()` puede insertar el singleton en un GET inicial y la visibilidad global del grupo de impresión requiere aprobación de negocio. También faltan ensayo PostgreSQL real en VPS, inspección de reglas Nginx para exports y E2E con el POS; nada de ello se afirma como ejecutado aquí.
