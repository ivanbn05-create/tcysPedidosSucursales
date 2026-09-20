import sys
from pathlib import Path

import dj_database_url
from decouple import config
from django.core.exceptions import ImproperlyConfigured

from .runtime_config import cargar_configuracion_segura

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


RUNTIME_SECURITY = cargar_configuracion_segura(config)
DJANGO_ENV = RUNTIME_SECURITY.perfil
IS_PRODUCTION = RUNTIME_SECURITY.es_produccion
SECRET_KEY = RUNTIME_SECURITY.secret_key
DEBUG = RUNTIME_SECURITY.debug

ALLOWED_HOSTS = [
    host.strip()
    for host in config(
        "ALLOWED_HOSTS",
        default="" if IS_PRODUCTION else "localhost,127.0.0.1,[::1],testserver",
    ).split(",")
    if host.strip()
]

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in config("CSRF_TRUSTED_ORIGINS", default="").split(",")
    if origin.strip()
]

if IS_PRODUCTION and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS es obligatorio en produccion.")

if IS_PRODUCTION and not CSRF_TRUSTED_ORIGINS:
    raise ImproperlyConfigured("CSRF_TRUSTED_ORIGINS es obligatorio en produccion.")


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'pedidos',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'pedidos.middleware.SesionUnicaMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'proyecto.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'proyecto.wsgi.application'


DATABASE_URL = config("DATABASE_URL", default="")

if IS_PRODUCTION and not DATABASE_URL:
    raise ImproperlyConfigured("DATABASE_URL es obligatoria en produccion.")

if DATABASE_URL:
    database_config = dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
    )
    if IS_PRODUCTION and database_config["ENGINE"] != "django.db.backends.sqlite3":
        database_config.setdefault("OPTIONS", {}).setdefault("sslmode", "require")
    DATABASES = {
        "default": database_config,
    }
else:
    DATABASES = {
        'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'es-mx'

TIME_ZONE = 'America/Mexico_City'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

if "test" in sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    STORAGES["staticfiles"] = {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    }

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

# Arrendamiento persistente de sesión única. Se renueva cada minuto mientras
# la página está activa; 10 minutos es menor a los 15 minutos de inactividad
# que provocan el spin-down de Render Free.
ACTIVE_SESSION_TTL_SECONDS = config("ACTIVE_SESSION_TTL_SECONDS", default=600, cast=int)

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Email (recordatorios diarios de pedidos, ver pedidos/management/commands/enviar_recordatorios.py)
# Credenciales SIEMPRE por variable de entorno, nunca en BD ni hardcoded (ver CLAUDE.md).
REMINDER_CONTACT_EMAIL = "tocayos.tacos@gmail.com"
EMAIL_BACKEND = config(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend" if not DEBUG else "django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = config("EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default=REMINDER_CONTACT_EMAIL)
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default=EMAIL_HOST_USER or REMINDER_CONTACT_EMAIL)

# Caché en memoria del proceso: evita consultar Configuracion en cada request
# (ver pedidos/views.py::get_configuracion). TIMEOUT real se define por llamada.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "pedidos-locmem",
    }
}

# El scheduler embebido fue retirado. Se conserva este ajuste fijo para hacer
# explicito que ningun worker web debe iniciar tareas automaticas.
SCHEDULER_ENABLED = False

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = IS_PRODUCTION or not DEBUG
CSRF_COOKIE_SECURE = IS_PRODUCTION or not DEBUG

# Explícito a propósito: "Lax" es el default de Django, pero las sucursales abren
# el sistema desde un link en WhatsApp o en la app de Google, y con "Strict" la
# cookie NO viajaría en esa primera navegación (el usuario caería siempre en el
# login). No lo subas a "Strict" sin probar antes desde un navegador in-app.
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = config(
    "SECURE_SSL_REDIRECT",
    default=IS_PRODUCTION or not DEBUG,
    cast=bool,
)
if IS_PRODUCTION and not SECURE_SSL_REDIRECT:
    raise ImproperlyConfigured("SECURE_SSL_REDIRECT debe estar habilitado en produccion.")

# HSTS empieza deshabilitado hasta validar HTTPS y el plan de rollback. Su
# activacion y alcance son decisiones explicitas del entorno, no del codigo.
SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=0, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=False,
    cast=bool,
)
SECURE_HSTS_PRELOAD = config("SECURE_HSTS_PRELOAD", default=False, cast=bool)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "pedidos": {
            "handlers": ["console"],
            "level": config("LOG_LEVEL", default="INFO"),
        },
        "pedidos.management.commands.enviar_recordatorios": {
            "handlers": ["console"],
            "level": config("LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
}
