"""
Autenticación y autorización a nivel de vista: quién puede seguir después
del login (rol_requerido), a dónde puede redirigir "next" sin riesgo de
open-redirect (es_url_segura), y el user_loader de Flask-Login.
"""

import hmac
from functools import wraps
from urllib.parse import urlparse

from flask import flash, redirect, url_for
from flask_login import login_required, current_user

from extensiones import db, login_manager
from modelos import Usuario


@login_manager.user_loader
def load_user(user_id):
    """
    El id de sesión es "<id>:<huella>" (ver Usuario.get_id). Una sesión con el
    formato anterior (solo el id) o con una huella que ya no coincide -- porque
    cambió la contraseña -- no se acepta.
    """
    id_texto, separador, huella = str(user_id).partition(':')
    if not separador or not id_texto.isascii() or not id_texto.isdigit():
        return None
    usuario = db.session.get(Usuario, int(id_texto))
    if usuario is None or not hmac.compare_digest(huella, usuario.huella_sesion()):
        return None
    return usuario


def es_url_segura(destino: str) -> bool:
    """
    Evita un 'Open Redirect': sin esta validación, alguien podría mandar un
    link tipo /login?next=https://sitio-falso.com y, tras iniciar sesión
    correctamente en el sitio REAL, el usuario terminaría redirigido a un
    sitio externo (útil para phishing dirigido al personal). Solo se
    permite continuar si 'next' es una ruta relativa de este mismo sitio
    (sin esquema http/https ni host propios).
    """
    if not destino:
        return False
    partes = urlparse(destino)
    return not partes.scheme and not partes.netloc


def rol_requerido(*roles_permitidos):
    """
    Decorador para restringir una ruta a ciertos roles (ej. solo DIRECTIVO).
    Siempre exige login primero (@login_required incluido). Uso:

        @app.route('/algo-solo-de-dirección')
        @rol_requerido('DIRECTIVO')
        def algo():
            ...
    """
    def decorador(func):
        @wraps(func)
        @login_required
        def envoltura(*args, **kwargs):
            if current_user.rol.name not in roles_permitidos:
                flash('No tienes permisos para realizar esta acción.', 'danger')
                return redirect(url_for('alumnos.index'))
            return func(*args, **kwargs)
        return envoltura
    return decorador


# ---------------------------------------------------------------------------
# Política de contraseñas
# ---------------------------------------------------------------------------
# Antes solo se exigían 8 caracteres: "12345678" pasaba (dos cuentas DIRECTIVO
# de la BD de desarrollo la usaban). Guía actual (NIST 800-63B): longitud +
# lista de claves comunes, sin exigir mezclas de símbolos que la gente resuelve
# con "Password1!".
LONGITUD_MINIMA_PASSWORD = 10

_CLAVES_COMUNES = {
    'password', 'password1', 'password12', 'password123', 'passw0rd', 'contrasena', 'contrasena1',
    'contrasena12', 'contrasena123', 'contraseña', 'contraseña1', 'contraseña123', 'qwertyuiop',
    'qwerty123', 'qwerty1234', 'asdfghjkl', 'asdfghjkl1', 'zxcvbnm123', 'iloveyou12', 'letmein123',
    'welcome123', 'admin1234', 'admin12345', 'administrador', 'administrador1', 'administrator',
    'directivo123', 'contador123', 'capturador1', 'enfermeria', 'enfermeria1', 'enfermeria12',
    'enfermeria123', 'enfermeria2026', 'enfermeria2025', 'escuela123', 'escuela2026', 'universidad',
    'universidad1', 'sge12345', 'sge2026', 'sge2026!', 'clave12345', 'clave123456', 'mexico2026',
    'mexico12345', 'cambiame123', 'cambiame1234', 'temporal123', 'temporal1234', 'test123456',
    '1q2w3e4r5t', '1qaz2wsx3e', 'q1w2e3r4t5', 'abc1234567', 'abcd123456', 'abcd1234567',
}


def _es_secuencia(texto: str) -> bool:
    """'1234567890', 'abcdefghij', '0987654321': cada carácter es el siguiente (o anterior) al previo."""
    if len(texto) < 4:
        return False
    pasos = {ord(b) - ord(a) for a, b in zip(texto, texto[1:])}
    return pasos in ({1}, {-1})


def validar_password(password: str, username: str = '') -> 'str | None':
    """Devuelve el motivo de rechazo (para mostrarlo tal cual) o None si la clave es aceptable."""
    password = password or ''
    if len(password) < LONGITUD_MINIMA_PASSWORD:
        return f'La contraseña debe tener al menos {LONGITUD_MINIMA_PASSWORD} caracteres.'
    minuscula = password.lower()
    if minuscula in _CLAVES_COMUNES:
        return 'Esa contraseña es de las más usadas; elige otra (una frase corta funciona bien).'
    if password.isdigit():
        return 'La contraseña no puede ser solo números.'
    if len(set(password)) == 1 or _es_secuencia(minuscula):
        return 'La contraseña no puede ser una repetición o una secuencia (aaaaaaaaaa, 1234567890).'
    usuario = (username or '').strip().lower()
    if len(usuario) >= 4 and usuario in minuscula:
        return 'La contraseña no puede contener tu nombre de usuario.'
    return None
