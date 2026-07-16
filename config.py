import os
from datetime import timedelta
from dotenv import load_dotenv

# Carga variables de entorno desde .env (si existe)
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))


class Config:
    """Configuración base compartida por todos los entornos."""

    # SECURITY-NOTE: ya no hay valor por defecto. Si SECRET_KEY no está en el
    # entorno, la app debe fallar al arrancar (ver ProductionConfig abajo) en
    # vez de usar una clave pública y predecible en silencio.
    SECRET_KEY = os.environ.get('SECRET_KEY')

    # --- Base de datos ---
    # En VPS (producción) se define DATABASE_URL apuntando a PostgreSQL, ej:
    # postgresql://usuario:password@localhost:5432/sge_enfermeria
    # En desarrollo local, si no existe la variable, se usa SQLite automáticamente.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(basedir, 'instance', 'sge_dev.db')
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- Reglas de negocio globales ---
    CUATRIMESTRES_MAXIMOS = 9  # Ajustable según la duración máxima de las carreras

    # --- Documentos digitalizados del expediente ---
    # SECURITY-NOTE: NUNCA dentro de static/. Flask sirve todo lo que está
    # bajo static/ públicamente (sin login) a través de su ruta implícita
    # /static/<path:filename>. Estos archivos son INE, CURP, actas de
    # nacimiento y comprobantes de domicilio de alumnos — deben servirse
    # SOLO a través de la ruta protegida /alumno/<matricula>/documento/<id>/ver
    # (ver app.py), que sí exige login + rol.
    UPLOAD_FOLDER = os.path.join(basedir, 'instance', 'documentos_alumnos')
    EXTENSIONES_PERMITIDAS = {'pdf', 'jpg', 'jpeg', 'png'}
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB por archivo

    # --- Seguridad de sesión (login) ---
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)  # Cierra sesión tras 8h de inactividad
    SESSION_COOKIE_HTTPONLY = True   # JS del navegador no puede leer la cookie de sesión
    SESSION_COOKIE_SAMESITE = 'Lax'  # Mitiga CSRF vía navegación cruzada
    SESSION_COOKIE_SECURE = False    # En ProductionConfig se fuerza a True (requiere HTTPS)

    # --- Rate limiting (Flask-Limiter): protege /login contra fuerza bruta ---
    # "memory://" guarda los contadores en RAM del proceso: funciona bien para
    # desarrollo y para un único worker de Gunicorn. En producción con VARIOS
    # workers/servidores, cada proceso tendría su propio contador (alguien
    # podría burlar el límite con más peticiones de las esperadas), así que
    # ahí se debe cambiar a un storage compartido, ej. Redis:
    #     RATELIMIT_STORAGE_URI = "redis://localhost:6379"
    RATELIMIT_STORAGE_URI = 'memory://'

    # --- Correo (comprobantes de pago) ---
    # Usamos Gmail con una "contraseña de aplicación" (no tu contraseña
    # normal -- Google la bloquea). Se genera en:
    # https://myaccount.google.com/security > Verificación en 2 pasos >
    # Contraseñas de aplicaciones. Va en tu .env, NUNCA aquí en el código.
    MAIL_SERVER = 'smtp.gmail.com'
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = ('Control Escolar SGE', os.environ.get('MAIL_USERNAME'))


class DevelopmentConfig(Config):
    DEBUG = True
    # En desarrollo sí toleramos una clave por defecto, para no obligar a
    # crear un .env solo para levantar el proyecto localmente.
    SECRET_KEY = os.environ.get('SECRET_KEY', 'clave-desarrollo-cambiar-en-produccion')


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True  # El VPS debe servir por HTTPS (Nginx + certificado)

    # Gunicorn corre VARIOS workers (procesos separados) en producción.
    # "memory://" es por-proceso, así que cada worker tendría su propio
    # contador de intentos de login — el límite de 5/min dejaría de ser real
    # (alguien podría intentar 5 × número_de_workers antes de que aplique).
    # Redis es un storage COMPARTIDO entre todos los workers, por eso se
    # exige aquí. Instálalo en el VPS con: sudo apt install redis-server
    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI', 'redis://localhost:6379')

    # SECURITY-NOTE: la validación de que SECRET_KEY exista de verdad NO se
    # hace aquí con un __init__ -- Flask llama a app.config.from_object()
    # pasando la CLASE (no una instancia), así que un __init__ en esta clase
    # nunca se ejecutaría y el error pasaría desapercibido. La validación
    # real está en create_app() dentro de app.py, inmediatamente después de
    # cargar esta configuración.


class TestingConfig(Config):
    """
    Configuración exclusiva para pytest. Base de datos en memoria (se crea
    y destruye por cada test, nunca toca tu sge_dev.db real), sin CSRF ni
    rate limiting para no complicar las pruebas de rutas.
    """
    TESTING = True
    SECRET_KEY = 'clave-de-pruebas-no-usar-en-produccion'
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    MAIL_SUPPRESS_SEND = True  # Nunca manda correos reales al correr pytest


config_by_name = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
}
