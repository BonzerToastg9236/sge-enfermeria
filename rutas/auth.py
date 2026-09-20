"""Login, logout y edición del perfil propio."""

from flask import Blueprint, render_template, request, flash, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import login_user, logout_user, login_required, current_user

from extensiones import db, limiter
from modelos import Usuario
from utilidades.seguridad import es_url_segura, validar_password
from utilidades.fechas import ahora_utc
from servicios.auditoria import registrar

auth_bp = Blueprint('auth', __name__)

# Hash de mentira para gastar el mismo tiempo cuando el usuario NO existe: sin
# esto el login tardaba ~145 ms si existía y ~2.5 ms si no, y cualquiera podía
# averiguar qué usuarios existen. (Auditoría 2026-09-19.)
_HASH_FICTICIO = generate_password_hash('contraseña-que-nadie-usa')


def _cuenta_del_intento():
    """Llave del límite por CUENTA: el mismo usuario, venga de la IP que venga."""
    return 'cuenta:' + request.form.get('username', '').strip().lower()[:80]


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
# Además del límite por IP: 10 intentos FALLIDOS por cuenta cada 15 minutos, sin
# importar desde cuántas IPs lleguen. Los inicios de sesión correctos (302) no
# cuentan. Contrapartida asumida: alguien puede bloquear 15 min a un usuario
# concreto a propósito; es preferible a permitir adivinar su clave sin freno.
@limiter.limit('10 per 15 minutes', key_func=_cuenta_del_intento, methods=['POST'],
               deduct_when=lambda respuesta: respuesta.status_code != 302, scope='login-cuenta')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('alumnos.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        recordar = request.form.get('recordar') == 'on'

        usuario = Usuario.query.filter_by(username=username).first()

        # Mensaje genérico a propósito: no revelamos si falló el usuario
        # o la contraseña, para no facilitar enumeración de cuentas.
        # Siempre se verifica UN hash (el del usuario o el ficticio): tiempo constante.
        hash_a_verificar = usuario.password_hash if usuario else _HASH_FICTICIO
        password_correcta = check_password_hash(hash_a_verificar, password) and usuario is not None

        if password_correcta and usuario.activo:
            session.permanent = True  # Activa PERMANENT_SESSION_LIFETIME (expira tras 8h de inactividad)
            login_user(usuario, remember=recordar)
            usuario.ultimo_acceso = ahora_utc()
            db.session.commit()

            flash(f'Bienvenido, {usuario.nombre_completo}.', 'success')
            siguiente = request.args.get('next')
            destino = siguiente if es_url_segura(siguiente) else url_for('alumnos.index')
            return redirect(destino)

        flash('Usuario o contraseña incorrectos.', 'danger')

    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada correctamente.', 'success')
    return redirect(url_for('auth.login'))


@auth_bp.route('/perfil', methods=['GET', 'POST'])
@login_required
def perfil():
    """Cada usuario (Directivo o Administrativo) edita SU propia cuenta aquí."""
    if request.method == 'POST':
        nombre_completo = request.form.get('nombre_completo', '').strip()
        if nombre_completo:
            current_user.nombre_completo = nombre_completo

        password_actual = request.form.get('password_actual', '')
        password_nueva = request.form.get('password_nueva', '')
        password_confirmar = request.form.get('password_confirmar', '')

        if password_nueva or password_confirmar or password_actual:
            if not current_user.check_password(password_actual):
                flash('Tu contraseña actual no es correcta; no se cambió nada.', 'danger')
                return redirect(url_for('auth.perfil'))
            motivo = validar_password(password_nueva, current_user.username)
            if motivo:
                flash(motivo, 'danger')
                return redirect(url_for('auth.perfil'))
            if password_nueva != password_confirmar:
                flash('La confirmación no coincide con la nueva contraseña.', 'danger')
                return redirect(url_for('auth.perfil'))
            current_user.set_password(password_nueva)
            # La huella de sesión cambió con la clave: toda otra sesión (incluida una
            # cookie robada) deja de valer; a quien la cambió se le renueva la suya.
            login_user(current_user._get_current_object())
            registrar('PASSWORD_CAMBIADA', 'Usuario', current_user.id, current_user.username)
            flash('Contraseña actualizada correctamente. Las demás sesiones abiertas de tu cuenta se cerraron.', 'success')

        db.session.commit()
        flash('Perfil actualizado.', 'success')
        return redirect(url_for('auth.perfil'))

    return render_template('perfil.html')
