"""
Autenticación y autorización a nivel de vista: quién puede seguir después
del login (rol_requerido), a dónde puede redirigir "next" sin riesgo de
open-redirect (es_url_segura), y el user_loader de Flask-Login.
"""

from functools import wraps
from urllib.parse import urlparse

from flask import flash, redirect, url_for
from flask_login import login_required, current_user

from extensiones import db, login_manager
from modelos import Usuario


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Usuario, int(user_id))


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
