import sys
from datetime import timedelta
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
    'proyecto.apps.AdminSeguroConfig',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'axes',
    'django_otp',
    'django_otp.plugins.otp_totp',
    'pedidos',
]

AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django_otp.middleware.OTPMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'pedidos.middleware.SesionUnicaMiddleware',
    'pedidos.middleware.MFAEnforcementMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'axes.middleware.AxesMiddleware',
]

MFA_ENFORCE = True
MFA_STEP_UP_SECONDS = 300
MFA_PENDING_SECONDS = 300
MFA_MAX_ATTEMPTS = 5
MFA_LOCK_SECONDS = 900
OTP_TOTP_ISSUER = 'Los Tocayos Pedidos'
OTP_ADMIN_HIDE_SENSITIVE_DATA = True
OTP_TOTP_THROTTLE_FACTOR = 1

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']
AXES_CLIENT_IP_CALLABLE = 'proyecto.security.login_client_ip'
AXES_LOCKOUT_CALLABLE = 'proyecto.security.login_lockout'
AXES_RESET_ON_SUCCESS = True
AXES_SENSITIVE_PARAMETERS = [
    'username', 'ip_address', 'codigo', 'otp_token', 'otp_device',
    'csrfmiddlewaretoken', 'device_id',
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
DB_SSLMODE = config("DB_SSLMODE", default="").strip().lower()
if DB_SSLMODE and DB_SSLMODE not in {
    "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
}:
    raise ImproperlyConfigured("DB_SSLMODE no es un valor PostgreSQL admitido.")

if IS_PRODUCTION and not DATABASE_URL:
    raise ImproperlyConfigured("DATABASE_URL es obligatoria en produccion.")

if DATABASE_URL:
    database_config = dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
    )
    if database_config["ENGINE"] == "django.db.backends.postgresql":
        if DB_SSLMODE:
            database_config.setdefault("OPTIONS", {})["sslmode"] = DB_SSLMODE
        elif IS_PRODUCTION:
            database_config.setdefault("OPTIONS", {}).setdefault("sslmode", "require")
    elif DB_SSLMODE:
        raise ImproperlyConfigured("DB_SSLMODE sólo aplica a PostgreSQL.")
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

# API HTTPS de solo lectura para el POS. Sin tokens o sucursales permitidas la
# vista falla cerrada con HTTP 503. Durante una rotacion pueden coexistir dos
# tokens separados por coma; nunca se registran ni se incluyen en respuestas.
POS_API_TOKENS = tuple(
    token.strip()
    for token in config("POS_API_TOKENS", default="").split(",")
    if token.strip()
)
if any(len(token) < 32 or len(token) > 512 for token in POS_API_TOKENS):
    raise ImproperlyConfigured(
        "Cada token de POS_API_TOKENS debe tener entre 32 y 512 caracteres."
    )

try:
    POS_API_ALLOWED_SUCURSAL_IDS = tuple(
        sorted(
            {
                int(value.strip())
                for value in config(
                    "POS_API_ALLOWED_SUCURSAL_IDS", default=""
                ).split(",")
                if value.strip()
            }
        )
    )
except ValueError as exc:
    raise ImproperlyConfigured(
        "POS_API_ALLOWED_SUCURSAL_IDS debe contener enteros separados por coma."
    ) from exc
if any(value <= 0 for value in POS_API_ALLOWED_SUCURSAL_IDS):
    raise ImproperlyConfigured(
        "POS_API_ALLOWED_SUCURSAL_IDS solo admite enteros positivos."
    )

POS_API_DEFAULT_PAGE_SIZE = config(
    "POS_API_DEFAULT_PAGE_SIZE", default=100, cast=int
)
POS_API_MAX_PAGE_SIZE = config("POS_API_MAX_PAGE_SIZE", default=500, cast=int)
POS_API_MAX_WINDOW_DAYS = config("POS_API_MAX_WINDOW_DAYS", default=31, cast=int)
POS_API_RATE_LIMIT_PER_MINUTE = config(
    "POS_API_RATE_LIMIT_PER_MINUTE", default=120, cast=int
)
POS_API_REQUIRE_HTTPS = config(
    "POS_API_REQUIRE_HTTPS", default=IS_PRODUCTION, cast=bool
)
POS_V2_REQUIRE_BRANCH_BINDING = config(
    "POS_V2_REQUIRE_BRANCH_BINDING", default=IS_PRODUCTION, cast=bool
)
if IS_PRODUCTION and not POS_V2_REQUIRE_BRANCH_BINDING:
    raise ImproperlyConfigured(
        "POS_V2_REQUIRE_BRANCH_BINDING es obligatorio en produccion."
    )
if not 1 <= POS_API_DEFAULT_PAGE_SIZE <= POS_API_MAX_PAGE_SIZE <= 1000:
    raise ImproperlyConfigured(
        "Los limites de pagina de la API POS deben cumplir 1 <= default <= max <= 1000."
    )
if not 1 <= POS_API_MAX_WINDOW_DAYS <= 90:
    raise ImproperlyConfigured("POS_API_MAX_WINDOW_DAYS debe estar entre 1 y 90.")
if not 0 <= POS_API_RATE_LIMIT_PER_MINUTE <= 10000:
    raise ImproperlyConfigured(
        "POS_API_RATE_LIMIT_PER_MINUTE debe estar entre 0 y 10000."
    )
POS_API_MAX_WINDOW = timedelta(days=POS_API_MAX_WINDOW_DAYS)

# El borrado físico y la reconciliación de restauraciones son operaciones
# manuales, con autorización y ventanas propias. Ningún worker/timer las inicia.
RETENTION_PURGE_ENABLED = config("RETENTION_PURGE_ENABLED", default=False, cast=bool)
RETENTION_EXPORT_CLEANUP_ENABLED = config("RETENTION_EXPORT_CLEANUP_ENABLED", default=False, cast=bool)
RETENTION_RESTORE_ISOLATED = config("RETENTION_RESTORE_ISOLATED", default=False, cast=bool)
RETENTION_BACKFILL_ENABLED = config("RETENTION_BACKFILL_ENABLED", default=False, cast=bool)
RETENTION_LOCAL_DB_CONFIRMED = config("RETENTION_LOCAL_DB_CONFIRMED", default=False, cast=bool)

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
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_SAVE_EVERY_REQUEST = True

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
