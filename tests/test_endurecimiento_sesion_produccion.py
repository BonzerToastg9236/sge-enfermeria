"""
Pruebas anti-regresión del endurecimiento de sesión y arranque en
producción (hallazgo #3 de la auditoría, plan de remediación paso 3).

CONTEXTO:
1. La cookie `remember_token` de Flask-Login (checkbox "recordar sesión"
   del login) nunca se configuró en config.py, así que usa los defaults
   de Flask-Login: sin Secure y 365 días de duración.
2. app.py arranca en modo desarrollo (con SECRET_KEY conocida en
   DevelopmentConfig) si la variable de entorno FLASK_ENV no está
   definida -- "falla abierto" en vez de "falla cerrado". El
   .env.example encima trae FLASK_ENV=development SIN comentar, a
   diferencia de las demás variables opcionales de ese archivo.

Las pruebas de "arranca como producción por defecto" corren la
importación de app.py en un subproceso limpio: app.py ejecuta
`app = create_app(...)` en el momento del import, sobre un único objeto
Flask (ver nota en conftest.py) -- no hay forma de re-ejercer ese import
con otro FLASK_ENV dentro del mismo proceso de pytest sin arriesgar el
estado de toda la suite.
"""

import os
import subprocess
import sys

from config import Config, ProductionConfig


# ---------------------------------------------------------------------------
# Cookie "recordar sesión" (REMEMBER_COOKIE_*)
# ---------------------------------------------------------------------------

def test_cookie_recordar_sesion_endurecida_en_config_base():
    assert Config.REMEMBER_COOKIE_HTTPONLY is True
    assert Config.REMEMBER_COOKIE_SAMESITE == 'Lax'
    assert Config.REMEMBER_COOKIE_DURATION.days == 14, (
        'El default de Flask-Login es 365 días; debe quedar acotado explícitamente'
    )


def test_cookie_recordar_sesion_secure_forzada_en_produccion():
    assert Config.REMEMBER_COOKIE_SECURE is False, 'En dev/testing no hay HTTPS'
    assert ProductionConfig.REMEMBER_COOKIE_SECURE is True, 'En producción el VPS sirve por HTTPS'


# ---------------------------------------------------------------------------
# Arranque sin FLASK_ENV definido: debe fallar cerrado (producción), no
# abierto (desarrollo con SECRET_KEY conocida)
# ---------------------------------------------------------------------------

_SCRIPT_IMPORTAR_APP = """
import dotenv
dotenv.load_dotenv = lambda *a, **k: None  # ignora el .env real de esta máquina de desarrollo
from app import app
print("DEBUG=" + str(app.config["DEBUG"]))
"""


def _correr_import_de_app(env_overrides, quitar=()):
    """
    Importa app.py en un subproceso con el entorno indicado. Regresa
    (returncode, stdout, stderr). Nunca toca la app real de este proceso
    de pytest, y neutraliza load_dotenv() para que el .env real de esta
    máquina (que trae su propio FLASK_ENV=development, legítimo para
    desarrollo local) no contamine la prueba de "sin FLASK_ENV definido".
    """
    env = dict(os.environ)
    for clave in quitar:
        env.pop(clave, None)
    env.update(env_overrides)

    resultado = subprocess.run(
        [sys.executable, '-c', _SCRIPT_IMPORTAR_APP],
        cwd=os.path.dirname(os.path.dirname(__file__)) or '.',
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return resultado


def test_sin_flask_env_arranca_como_produccion():
    resultado = _correr_import_de_app(
        {
            'SECRET_KEY': 'clave-de-prueba-para-este-subproceso',
            # ProductionConfig usa Redis por defecto (requisito real del VPS,
            # ver config.py); esta máquina de pruebas no tiene el paquete
            # redis instalado, así que se apunta a memoria solo para poder
            # importar la app y verificar DEBUG, sin probar el storage.
            'RATELIMIT_STORAGE_URI': 'memory://',
        },
        quitar=('FLASK_ENV',),
    )

    assert resultado.returncode == 0, resultado.stderr
    assert 'DEBUG=False' in resultado.stdout, (
        'Sin FLASK_ENV definido, la app debería arrancar en modo producción (DEBUG=False)'
    )


def test_sin_flask_env_ni_secret_key_la_app_no_arranca():
    """Falla cerrado: sin SECRET_KEY, producción debe abortar el arranque, no usar una clave insegura."""
    resultado = _correr_import_de_app({}, quitar=('FLASK_ENV', 'SECRET_KEY'))

    assert resultado.returncode != 0
    assert 'SECRET_KEY' in resultado.stderr


def test_flask_env_development_explicito_sigue_funcionando():
    """El cambio de default no debe romper el desarrollo local de siempre."""
    resultado = _correr_import_de_app({'FLASK_ENV': 'development'}, quitar=('SECRET_KEY',))

    assert resultado.returncode == 0, resultado.stderr
    assert 'DEBUG=True' in resultado.stdout
