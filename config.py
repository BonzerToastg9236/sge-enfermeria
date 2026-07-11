import os
from datetime import timedelta
from dotenv import load_dotenv

# Carga variables de entorno desde .env (si existe)
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))


class Config:
    """Configuración base compartida por todos los entornos."""

    SECRET_KEY = os.environ.get('SECRET_KEY', 'clave-desarrollo-cambiar-en-produccion')

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
    UPLOAD_FOLDER = os.path.join(basedir, 'static', 'uploads', 'documentos')
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


class DevelopmentConfig(Config):
    DEBUG = True


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


config_by_name = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
}
