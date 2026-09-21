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

Recupera el modo registrado antes de la activacion. El modo `bootstrap`
restaura el checkout y la unidad anteriores sin usar `previous_release`; el
modo `release` vuelve al release administrado anterior:

```bash
APP_ROOT=/home/deploy/apps/tcysPedidosSucursales
ACTIVE_CHECKOUT=/home/deploy/src/tcysPedidosSucursales
ROLLBACK_MODE=$(cat "$APP_ROOT/shared/rollback_mode")
UNIT_BACKUP="$APP_ROOT/shared/tcysweb-prod.service.before-release"
test -f "$UNIT_BACKUP"

case "$ROLLBACK_MODE" in
  bootstrap)
    PREVIOUS_CHECKOUT=$(cat "$APP_ROOT/shared/bootstrap_previous_checkout")
    PREVIOUS_COMMIT=$(cat "$APP_ROOT/shared/bootstrap_previous_commit")
    test "$PREVIOUS_CHECKOUT" = "$ACTIVE_CHECKOUT"
    test "$PREVIOUS_COMMIT" = ba7330bf006417cc1377b20bdcb532a4a27c37b9
    test "$(git -C "$PREVIOUS_CHECKOUT" rev-parse HEAD)" = "$PREVIOUS_COMMIT"
    ;;
  release)
    PREVIOUS=$(cat "$APP_ROOT/shared/previous_release")
    case "$PREVIOUS" in
      "$APP_ROOT"/releases/*) ;;
      *) echo 'Ruta de rollback invalida' >&2; exit 1 ;;
    esac
    test -x "$PREVIOUS/.venv/bin/gunicorn"
    ;;
  *)
    echo 'Modo de rollback invalido o ausente' >&2
    exit 1
    ;;
esac
```

Para un release posterior, cambia el symlink de forma atomica. En bootstrap no
se modifica `current` antes del reinicio: la unidad respaldada vuelve a apuntar
directamente al checkout antiguo. Restaura la unidad, reinicia y prueba HTTPS:

```bash
if test "$ROLLBACK_MODE" = release; then
  ln -s "$PREVIOUS" "$APP_ROOT/current.rollback"
  mv -Tf "$APP_ROOT/current.rollback" "$APP_ROOT/current"
fi

sudo install -m 0644 "$UNIT_BACKUP" /etc/systemd/system/tcysweb-prod.service
sudo systemctl daemon-reload
sudo systemctl restart tcysweb-prod.service
sudo systemctl status tcysweb-prod.service --no-pager
curl --fail --silent --show-error --output /dev/null https://tcysweb.lostocayos-lostcys.com.mx/
sudo journalctl -u tcysweb-prod.service -n 100 --no-pager

if test "$ROLLBACK_MODE" = bootstrap; then
  if test -L "$APP_ROOT/current"; then
    FAILED_RELEASE=$(readlink -f "$APP_ROOT/current")
    case "$FAILED_RELEASE" in
      "$APP_ROOT"/releases/*) ;;
      *) echo 'El symlink current apunta fuera del arbol de releases' >&2; exit 1 ;;
    esac
    rm -- "$APP_ROOT/current"
  elif test -e "$APP_ROOT/current"; then
    echo 'current existe pero no es un symlink' >&2
    exit 1
  fi
fi
```

Si el release aplico migraciones, cambiar solo el codigo puede no ser seguro.
Antes de migrar debe existir una decision documentada entre migracion hacia
adelante o restauracion del backup previo, con ventana de mantenimiento para
evitar split-brain. Nunca restaures una base ni reviertas una migracion
productiva sin autorizacion expresa.

Mantiene Render y Supabase disponibles durante la ventana de rollback. No
elimines releases previos hasta que la version nueva haya sido observada y
aceptada.
