# Rollback

El rollback de aplicacion no necesita GitHub: cada release conserva codigo,
estaticos y venv. No borres el release fallido hasta terminar el diagnostico.

## Prueba paralela

Si falla la prueba en `127.0.0.1:8012`, detenla y verifica que produccion sigue
intacta:

```bash
sudo systemctl stop tcysweb-release-test-<PREFIJO_COMMIT>
sudo systemctl status tcysweb-prod --no-pager
sudo ss -lntp | grep ':8002'
```

No cambies `current`, Nginx, DNS, Render ni Supabase por una prueba fallida.

## Release activado

Recupera la ruta registrada y valida que pertenece al arbol de releases:

```bash
APP_ROOT=/home/deploy/apps/tcysPedidosSucursales
PREVIOUS=$(cat "$APP_ROOT/shared/previous_release")
case "$PREVIOUS" in "$APP_ROOT"/releases/*) ;; *) echo 'Ruta de rollback invalida' >&2; exit 1 ;; esac
test -x "$PREVIOUS/.venv/bin/gunicorn"
```

Cambia el symlink de forma atomica, reinicia y prueba HTTPS:

```bash
ln -s "$PREVIOUS" "$APP_ROOT/current.rollback"
mv -Tf "$APP_ROOT/current.rollback" "$APP_ROOT/current"
if test -f "$APP_ROOT/shared/tcysweb-prod.service.before-release"; then
  sudo install -m 0644 "$APP_ROOT/shared/tcysweb-prod.service.before-release" /etc/systemd/system/tcysweb-prod.service
  sudo systemctl daemon-reload
fi
sudo systemctl restart tcysweb-prod.service
sudo systemctl status tcysweb-prod.service --no-pager
curl --fail --silent --show-error --output /dev/null https://tcysweb.lostocayos-lostcys.com.mx/
sudo journalctl -u tcysweb-prod.service -n 100 --no-pager
```

Si el release aplico migraciones, cambiar solo el codigo puede no ser seguro.
Antes de migrar debe existir una decision documentada entre migracion hacia
adelante o restauracion del backup previo, con ventana de mantenimiento para
evitar split-brain. Nunca restaures una base ni reviertas una migracion
productiva sin autorizacion expresa.

Mantiene Render y Supabase disponibles durante la ventana de rollback. No
elimines releases previos hasta que la version nueva haya sido observada y
aceptada.
