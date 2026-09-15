"""Auto-registro público de aspirantes. Rate-limited (fix #4)."""

import re
from datetime import datetime

from flask import Blueprint, render_template, request, flash, redirect, url_for
from markupsafe import Markup, escape

from extensiones import db, limiter
from modelos import Alumno, PlanEstudio, EstatusAlumno, TurnoAlumno, ModalidadEstudio
from servicios.matriculas import crear_alumno_generando_matricula

registro_bp = Blueprint('registro', __name__)

# Letras (con acentos), espacios, guion y apóstrofo -- para apellidos
# compuestos reales ("Pérez-García", "D'León"). El PRIMER caracter debe
# ser letra a fuerzas: así un nombre nunca puede empezar con = + - @,
# que es lo que se interpretaría como fórmula al exportarlo a Excel (ver
# utilidades/archivos.py::valor_seguro_excel, que además neutraliza esto
# como segunda capa por si el nombre viene de otro lado, ej. importación
# masiva).
NOMBRE_REGEX = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ '\-]{4,119}$")

# correo es opcional; cuando SÍ se captura, se valida el formato antes de guardarlo.
CORREO_REGEX = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')


@registro_bp.route('/registro', methods=['GET', 'POST'])
@limiter.limit('20 per hour;5 per minute', methods=['POST'])
def registro():
    planes = PlanEstudio.query.filter_by(activo=True).order_by(PlanEstudio.nombre.asc()).all()

    if request.method == 'GET':
        return render_template('registro.html', planes=planes)

    # --- POST: procesar el formulario ---
    nombre_completo = request.form.get('nombre_completo', '').strip()
    curp = request.form.get('curp', '').strip().upper()
    fecha_nacimiento_raw = request.form.get('fecha_nacimiento', '')
    fecha_certificado_raw = request.form.get('fecha_certificado_prepa', '')
    id_plan_raw = request.form.get('id_plan_fk', '')
    correo = request.form.get('correo', '').strip() or None
    telefono = request.form.get('telefono', '').strip() or None
    telefono_movil = request.form.get('telefono_movil', '').strip() or None

    sexo = request.form.get('sexo', '').strip() or None
    numero_identificacion = request.form.get('numero_identificacion', '').strip() or None
    estado_civil = request.form.get('estado_civil', '').strip() or None
    nacionalidad = request.form.get('nacionalidad', '').strip() or 'Mexicana'
    tipo_sangre = request.form.get('tipo_sangre', '').strip() or None

    domicilio_calle_numero = request.form.get('domicilio_calle_numero', '').strip() or None
    domicilio_ciudad = request.form.get('domicilio_ciudad', '').strip() or None
    domicilio_cp = request.form.get('domicilio_cp', '').strip() or None
    domicilio_estado = request.form.get('domicilio_estado', '').strip() or None

    contacto_emergencia_nombre = request.form.get('contacto_emergencia_nombre', '').strip() or None
    contacto_emergencia_telefono = request.form.get('contacto_emergencia_telefono', '').strip() or None
    contacto_emergencia_parentesco = request.form.get('contacto_emergencia_parentesco', '').strip() or None

    como_se_entero = request.form.get('como_se_entero', '').strip() or None

    turno_raw = request.form.get('turno', '')
    modalidad_raw = request.form.get('modalidad', '')

    errores = []

    if not NOMBRE_REGEX.match(nombre_completo):
        errores.append('Ingresa tu nombre completo correctamente (solo letras, espacios, guion y apóstrofo).')

    if not re.match(r'^[A-Z0-9]{18}$', curp):
        errores.append('La CURP debe tener exactamente 18 caracteres alfanuméricos.')

    if correo and not CORREO_REGEX.match(correo):
        errores.append('El correo electrónico no tiene un formato válido.')
    elif Alumno.query.filter_by(curp=curp).first():
        errores.append(f'Ya existe un alumno registrado con la CURP "{curp}".')

    if not sexo:
        errores.append('Selecciona tu sexo.')

    if not domicilio_calle_numero or not domicilio_ciudad or not domicilio_cp or not domicilio_estado:
        errores.append('Completa todos los campos de tu domicilio.')

    if not contacto_emergencia_nombre or not contacto_emergencia_telefono:
        errores.append('Indica el nombre y teléfono de tu contacto de emergencia.')

    turno = None
    if turno_raw not in TurnoAlumno.__members__:
        errores.append('Selecciona un turno válido.')
    else:
        turno = TurnoAlumno[turno_raw]

    modalidad = None
    if modalidad_raw not in ModalidadEstudio.__members__:
        errores.append('Selecciona una modalidad válida.')
    else:
        modalidad = ModalidadEstudio[modalidad_raw]

    fecha_nacimiento = None
    try:
        fecha_nacimiento = datetime.strptime(fecha_nacimiento_raw, '%Y-%m-%d').date()
    except ValueError:
        errores.append('La fecha de nacimiento no es válida.')

    fecha_certificado_prepa = None
    try:
        fecha_certificado_prepa = datetime.strptime(fecha_certificado_raw, '%Y-%m-%d').date()
    except ValueError:
        errores.append('La fecha del certificado de preparatoria no es válida.')

    plan = None
    if not id_plan_raw:
        errores.append('Debes seleccionar tu carrera / plan de estudios.')
    else:
        try:
            plan = db.session.get(PlanEstudio, int(id_plan_raw))
        except (ValueError, TypeError):
            plan = None
        if not plan or not plan.activo:
            errores.append('El plan de estudios seleccionado no es válido.')

    if errores:
        for error in errores:
            flash(error, 'danger')
        # Reenviamos el formulario con los planes para no perder el <select>
        return render_template('registro.html', planes=planes), 400

    alumno, error_creacion = crear_alumno_generando_matricula(
        plan,
        nombre_completo=nombre_completo,
        curp=curp,
        fecha_nacimiento=fecha_nacimiento,
        fecha_certificado_prepa=fecha_certificado_prepa,
        estatus=EstatusAlumno.PENDIENTE,
        correo=correo,
        telefono=telefono,
        telefono_movil=telefono_movil,
        sexo=sexo,
        numero_identificacion=numero_identificacion,
        estado_civil=estado_civil,
        nacionalidad=nacionalidad,
        tipo_sangre=tipo_sangre,
        domicilio_calle_numero=domicilio_calle_numero,
        domicilio_ciudad=domicilio_ciudad,
        domicilio_cp=domicilio_cp,
        domicilio_estado=domicilio_estado,
        contacto_emergencia_nombre=contacto_emergencia_nombre,
        contacto_emergencia_telefono=contacto_emergencia_telefono,
        contacto_emergencia_parentesco=contacto_emergencia_parentesco,
        como_se_entero=como_se_entero,
        turno=turno,
        modalidad=modalidad,
    )

    if error_creacion:
        flash('Ocurrió un error al guardar tu registro. Verifica tus datos e intenta de nuevo.', 'danger')
        return render_template('registro.html', planes=planes), 400

    # SECURITY-NOTE: esta es la ÚNICA vista PÚBLICA sin login del sistema, así
    # que es la de mayor exposición. Antes la plantilla usaba {{ message|safe }}
    # para poder mostrar <strong>{matricula}</strong> en negritas -- pero eso
    # dejaba la puerta abierta a que un flash() futuro con datos de usuario sin
    # escapar se convirtiera en XSS reflejado. Ahora se arma explícitamente con
    # Markup() + escape(): el HTML fijo (las etiquetas <strong>) se conserva,
    # pero cualquier dato variable (la matrícula) SIEMPRE pasa por escape().
    flash(
        Markup(
            f'¡Registro exitoso! Tu matrícula es <strong>{escape(alumno.matricula_id)}</strong>. '
            'Tu solicitud quedó en estatus "Pendiente de Validación" y será revisada por Control Escolar.'
        ),
        'success'
    )
    return redirect(url_for('registro.registro'))
