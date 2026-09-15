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

    # --- Zona horaria de la institución ---
    # El VPS corre en UTC; todo lo que se GUARDA sigue en UTC (ver
    # ahora_utc() en app.py). Esto es solo para REGLAS DE NEGOCIO que
    # dependen de "qué día es hoy" (vencimientos, recargos, folios, corte
    # de caja) y para mostrar fechas/horas en plantillas -- ver hoy_local()
    # y a_local() en app.py.
    ZONA_HORARIA = os.environ.get('ZONA_HORARIA', 'America/Mexico_City')

    # --- Documentos digitalizados del expediente ---
    # SECURITY-NOTE: NUNCA dentro de static/. Flask sirve todo lo que está
    # bajo static/ públicamente (sin login) a través de su ruta implícita
    # /static/<path:filename>. Estos archivos son INE, CURP, actas de
    # nacimiento y comprobantes de domicilio de alumnos — deben servirse
    # SOLO a través de la ruta protegida /alumno/<matricula>/documento/<id>/ver
    # (ver app.py), que sí exige login + rol.
    UPLOAD_FOLDER = os.path.join(basedir, 'instance', 'documentos_alumnos')
    EXTENSIONES_PERMITIDAS = {'pdf', 'jpg', 'jpeg', 'png'}
    # OJO: Flask/Werkzeug aplica esto al TAMAÑO TOTAL DE LA PETICIÓN, no a
    # cada archivo por separado -- el formulario de documentos permite subir
    # hasta 6 archivos de una vez, y lo que se compara contra este límite es
    # la suma. Al excederlo, Werkzeug corta la petición con un 413 antes de
    # llegar a la vista (ver el errorhandler(413) en app.py, que lo traduce
    # a un mensaje entendible). nginx_sge.conf tiene client_max_body_size
    # 10M, holgadamente por encima, para que el rechazo lo dé la app y no
    # Nginx con su propia página de error.
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024  # 8 MB por envío

    # --- Seguridad de sesión (login) ---
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)  # Cierra sesión tras 8h de inactividad
    SESSION_COOKIE_HTTPONLY = True   # JS del navegador no puede leer la cookie de sesión
    SESSION_COOKIE_SAMESITE = 'Lax'  # Mitiga CSRF vía navegación cruzada
    SESSION_COOKIE_SECURE = False    # En ProductionConfig se fuerza a True (requiere HTTPS)

    # --- Cookie de "recordar sesión" (checkbox del login, Flask-Login) ---
    # Sin esto, Flask-Login usa sus propios defaults: sin Secure y 365 días.
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = 'Lax'
    REMEMBER_COOKIE_SECURE = False    # En ProductionConfig se fuerza a True (requiere HTTPS)
    REMEMBER_COOKIE_DURATION = timedelta(days=14)  # En vez del año por defecto

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

    # Segundos máximos que puede tardar CUALQUIER operación contra el
    # servidor SMTP (conectar, STARTTLS, login, enviar).
    #
    # POR QUÉ EXISTE: Flask-Mail 0.10.0 abre la conexión con
    # `smtplib.SMTP(server, port)` SIN timeout, y sin timeout smtplib
    # hereda el default global de sockets, que es esperar PARA SIEMPRE. El
    # envío del comprobante es síncrono, dentro del flujo de cobro, así que
    # un Gmail lento o colgado bloquearía al worker de Gunicorn que atiende
    # ese pago. Con solo 3 workers (deploy/sge.service), dos o tres cobros
    # simultáneos en ese estado dejan el sistema sin capacidad para nadie.
    # La app aplica este valor en _ConexionSMTPConTimeout (app.py).
    #
    # 10s es holgado: Gmail normalmente responde en menos de 2s. Súbelo
    # solo si ves fallos de correo en una red lenta.
    MAIL_TIMEOUT = int(os.environ.get('MAIL_TIMEOUT', 10))
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
    REMEMBER_COOKIE_SECURE = True  # Ídem, para la cookie de "recordar sesión"

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
    y destruye por cada test, nunca toca tu sge_dev.db real), sin CSRF
    para no complicar las pruebas de rutas.

    NOTA: el límite de intentos de login (Flask-Limiter) se deja
    funcionando IGUAL que en producción (no se desactiva aquí) --
    desactivarlo desde el arranque hace que Flask-Limiter se salte la
    configuración de su almacenamiento por completo, y luego truene en
    tiempo de ejecución. En vez de desactivarlo, el fixture de pruebas
    llama a limiter.reset() antes de cada prueba, así que los intentos
    nunca se acumulan de una prueba a otra.
    """
    TESTING = True
    SECRET_KEY = 'clave-de-pruebas-no-usar-en-produccion'
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
    WTF_CSRF_ENABLED = False
    MAIL_SUPPRESS_SEND = True  # Nunca manda correos reales al correr pytest


config_by_name = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
}
