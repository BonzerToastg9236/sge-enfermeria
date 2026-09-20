"""Alta y administración de cuentas de personal (DIRECTIVO)."""

import re
from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import Usuario, RolUsuario
from servicios.auditoria import registrar
from utilidades.seguridad import rol_requerido, validar_password

usuarios_bp = Blueprint('usuarios', __name__)


@usuarios_bp.route('/usuarios')
@rol_requerido('DIRECTIVO')
def usuarios():
    lista = Usuario.query.order_by(Usuario.rol.asc(), Usuario.nombre_completo.asc()).all()
    return render_template('usuarios.html', usuarios=lista)


@usuarios_bp.route('/usuarios/nuevo', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def nuevo_usuario():
    if request.method == 'POST':
        nombre_completo = request.form.get('nombre_completo', '').strip()
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        confirmar = request.form.get('confirmar_password', '')
        rol_raw = request.form.get('rol', 'ADMINISTRATIVO')

        errores = []
        if len(nombre_completo) < 3:
            errores.append('Ingresa el nombre completo del usuario.')
        if len(username) < 4 or not re.match(r'^[a-z0-9_.]+$', username):
            errores.append('El usuario debe tener al menos 4 caracteres (letras, números, "." o "_").')
        elif Usuario.query.filter_by(username=username).first():
            errores.append(f'El usuario "{username}" ya existe.')
        motivo_password = validar_password(password, username)
        if motivo_password:
            errores.append(motivo_password)
        elif password != confirmar:
            errores.append('Las contraseñas no coinciden.')
        if rol_raw not in RolUsuario.__members__:
            errores.append('Selecciona un rol válido.')

        if errores:
            for error in errores:
                flash(error, 'danger')
            return render_template('nuevo_usuario.html'), 400

        nuevo = Usuario(
            nombre_completo=nombre_completo,
            username=username,
            rol=RolUsuario[rol_raw],
            activo=True
        )
        nuevo.set_password(password)

        try:
            db.session.add(nuevo)
            db.session.flush()
            registrar('USUARIO_CREADO', 'Usuario', nuevo.id, f'{username} ({nuevo.rol.name}) - {nombre_completo}')
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Ocurrió un error al crear el usuario. Intenta de nuevo.', 'danger')
            return render_template('nuevo_usuario.html'), 400

        flash(f'Usuario "{username}" ({nuevo.rol.value}) creado correctamente.', 'success')
        return redirect(url_for('usuarios.usuarios'))

    return render_template('nuevo_usuario.html')


@usuarios_bp.route('/usuarios/<int:user_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO')
def toggle_usuario(user_id):
    """Activa/desactiva una cuenta sin borrarla (mejor que eliminarla: conserva auditoría)."""
    usuario = db.get_or_404(Usuario, user_id)

    if usuario.id == current_user.id:
        flash('No puedes desactivar tu propia cuenta.', 'danger')
        return redirect(url_for('usuarios.usuarios'))

    usuario.activo = not usuario.activo
    registrar('USUARIO_ACTIVADO' if usuario.activo else 'USUARIO_DESACTIVADO', 'Usuario', usuario.id, f'{usuario.username} ({usuario.rol.name})')
    db.session.commit()

    estado = 'activada' if usuario.activo else 'desactivada'
    flash(f'La cuenta de "{usuario.username}" fue {estado}.', 'success')
    return redirect(url_for('usuarios.usuarios'))
