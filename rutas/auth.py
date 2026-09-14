"""Login, logout y edición del perfil propio."""

from flask import Blueprint, render_template, request, flash, redirect, url_for, session
from flask_login import login_user, logout_user, login_required, current_user

from extensiones import db, limiter
from modelos import Usuario
from utilidades.seguridad import es_url_segura
from utilidades.fechas import ahora_utc

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
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
        if usuario and usuario.activo and usuario.check_password(password):
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
            if len(password_nueva) < 8:
                flash('La nueva contraseña debe tener al menos 8 caracteres.', 'danger')
                return redirect(url_for('auth.perfil'))
            if password_nueva != password_confirmar:
                flash('La confirmación no coincide con la nueva contraseña.', 'danger')
                return redirect(url_for('auth.perfil'))
            current_user.set_password(password_nueva)
            flash('Contraseña actualizada correctamente.', 'success')

        db.session.commit()
        flash('Perfil actualizado.', 'success')
        return redirect(url_for('auth.perfil'))

    return render_template('perfil.html')
