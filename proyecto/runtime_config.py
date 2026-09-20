"""Carga y valida la configuracion sensible del runtime.

La logica vive fuera de ``settings.py`` para poder probar los perfiles sin
necesitar secretos reales ni alterar la configuracion global de Django.
"""

from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured


PERFILES_VALIDOS = {"development", "test", "production", "vps"}
PERFILES_PRODUCTIVOS = {"production", "vps"}
SECRET_KEY_DESARROLLO = "django-insecure-development-only-do-not-use-in-production"


@dataclass(frozen=True)
class RuntimeSecurityConfig:
    perfil: str
    es_produccion: bool
    secret_key: str
    debug: bool


def _secret_key_insegura(secret_key):
    secret_key_normalizada = secret_key.lower()
    return (
        len(secret_key) < 50
        or len(set(secret_key)) < 5
        or secret_key.startswith("django-insecure-")
        or any(
            marcador in secret_key_normalizada
            for marcador in ("change-me", "example", "reemplazar", "placeholder")
        )
        or "<" in secret_key
        or ">" in secret_key
    )


def cargar_configuracion_segura(config_reader):
    """Devuelve la configuracion validada o falla antes de iniciar Django."""

    perfil = str(config_reader("DJANGO_ENV", default="development")).strip().lower()
    if perfil not in PERFILES_VALIDOS:
        opciones = ", ".join(sorted(PERFILES_VALIDOS))
        raise ImproperlyConfigured(
            f"DJANGO_ENV debe ser uno de: {opciones}. Se recibio: {perfil!r}."
        )

    perfil_productivo = perfil in PERFILES_PRODUCTIVOS
    debug = config_reader("DEBUG", default=not perfil_productivo, cast=bool)
    # Tambien trata DEBUG=False como produccion para proteger despliegues
    # existentes que aun no hayan incorporado DJANGO_ENV.
    es_produccion = perfil_productivo or not debug
    secret_key = str(config_reader("SECRET_KEY", default="")).strip()

    if es_produccion:
        if debug:
            raise ImproperlyConfigured(
                "DEBUG no puede estar habilitado con DJANGO_ENV=production o vps."
            )
        if not secret_key:
            raise ImproperlyConfigured(
                "SECRET_KEY es obligatoria en un perfil productivo o con DEBUG=False."
            )
        if _secret_key_insegura(secret_key):
            raise ImproperlyConfigured(
                "SECRET_KEY no cumple los requisitos minimos para produccion."
            )
    elif not secret_key:
        secret_key = SECRET_KEY_DESARROLLO

    return RuntimeSecurityConfig(
        perfil=perfil,
        es_produccion=es_produccion,
        secret_key=secret_key,
        debug=False if es_produccion else debug,
    )
