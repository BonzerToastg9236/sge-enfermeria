"""
Configuración de la institución: mensualidades por carrera, materias por
plan, catálogo de conceptos de cobro (y sus precios), datos generales de
la institución y política de recargos. Ver deploy/PENDIENTES_PRODUCCION.md
§3: NULL en un precio significa "sin configurar", nunca se convierte en
$0 automáticamente -- no alterar esa regla al mover estas rutas.
"""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, flash, redirect, url_for

from extensiones import db
from modelos import (
    PlanEstudio, Materia, ConceptoCobro, ConfiguracionCobros, ConfiguracionInstitucion,
    Calificacion, InscripcionMateria, TipoRecargo,
)
from utilidades.seguridad import rol_requerido
from servicios.academico import _max_periodos

configuracion_bp = Blueprint('configuracion', __name__)


@configuracion_bp.route('/planes/mensualidades', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def planes_mensualidades():
    """
    Pantalla mínima para que Dirección ajuste el precio de mensualidad de
    cada carrera (cada una puede costar distinto). Solo edita ese campo;
    el CRUD completo de Planes de Estudio sigue pendiente como tarea aparte.
    """
    if request.method == 'POST':
        plan_id = request.form.get('plan_id', '').strip()
        monto_raw = request.form.get('monto_mensualidad', '').strip()
        plan = db.get_or_404(PlanEstudio, int(plan_id)) if plan_id.isdigit() else None

        if not plan:
            flash('Plan de estudios no encontrado.', 'danger')
        elif not monto_raw:
            # Campo vacío = "No definido" a propósito (así lo indica el
            # placeholder del formulario). Antes esto siempre fallaba la
            # validación de Decimal('') y nunca se podía volver a dejar sin
            # definir una mensualidad ya configurada.
            plan.monto_mensualidad = None
            db.session.commit()
            flash(f'Mensualidad de "{plan.nombre}" eliminada (queda sin definir).', 'success')
        else:
            try:
                monto = Decimal(monto_raw)
                if monto < 0:
                    raise InvalidOperation
                plan.monto_mensualidad = monto
                db.session.commit()
                flash(f'Mensualidad de "{plan.nombre}" actualizada a ${monto}.', 'success')
            except InvalidOperation:
                flash('El monto no es un número válido.', 'danger')

        return redirect(url_for('configuracion.planes_mensualidades'))

    planes = PlanEstudio.query.order_by(PlanEstudio.nombre.asc()).all()
    return render_template('planes_mensualidades.html', planes=planes)


@configuracion_bp.route('/planes/<int:plan_id>/materias', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def gestionar_materias(plan_id):
    """
    Alta manual de materias por cuatrimestre para un plan de estudios.
    Solo Directivo: esto es lo que define el "Escudo del Plan de
    Estudios" que todo lo demás (boletas, cargos de mensualidad, carga
    académica) respeta -- agregar una materia aquí por error se propaga
    a todo el sistema, así que queda con el rol más restringido.
    """
    plan = db.get_or_404(PlanEstudio, plan_id)
    max_cuatri = _max_periodos()

    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        clave = request.form.get('clave', '').strip() or None
        cuatrimestre_raw = request.form.get('cuatrimestre', '').strip()
        creditos_raw = request.form.get('creditos', '').strip()

        errores = []
        if len(nombre) < 3:
            errores.append('El nombre de la materia debe tener al menos 3 caracteres.')

        cuatrimestre = None
        try:
            cuatrimestre = int(cuatrimestre_raw)
            if cuatrimestre < 1 or cuatrimestre > max_cuatri:
                errores.append(f'El cuatrimestre debe estar entre 1 y {max_cuatri}.')
                cuatrimestre = None
        except (ValueError, TypeError):
            errores.append('Indica un número de cuatrimestre válido.')

        creditos = None
        if creditos_raw:
            try:
                creditos = float(creditos_raw)
            except ValueError:
                errores.append('Los créditos deben ser un número.')

        if not errores:
            duplicada = Materia.query.filter_by(id_plan_fk=plan.id, nombre=nombre, cuatrimestre=cuatrimestre).first()
            if duplicada:
                errores.append(f'Ya existe "{nombre}" en el {cuatrimestre}° cuatrimestre de este plan.')

        if errores:
            for error in errores:
                flash(error, 'danger')
        else:
            db.session.add(Materia(nombre=nombre, clave=clave, cuatrimestre=cuatrimestre, creditos=creditos, id_plan_fk=plan.id))
            db.session.commit()
            flash(f'"{nombre}" agregada al {cuatrimestre}° cuatrimestre de {plan.nombre}.', 'success')

        return redirect(url_for('configuracion.gestionar_materias', plan_id=plan.id))

    materias_por_cuatrimestre = {}
    for materia in Materia.query.filter_by(id_plan_fk=plan.id).order_by(Materia.cuatrimestre.asc(), Materia.nombre.asc()).all():
        materias_por_cuatrimestre.setdefault(materia.cuatrimestre, []).append(materia)

    return render_template(
        'gestionar_materias.html',
        plan=plan,
        materias_por_cuatrimestre=materias_por_cuatrimestre,
        max_cuatrimestres=max_cuatri,
    )


@configuracion_bp.route('/materias/<int:materia_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_materia(materia_id):
    """
    Solo permite eliminar una materia si NUNCA se usó -- ni calificaciones
    ni carga académica registradas con ella. Igual que con Cargo, no se
    permite borrar algo que ya dejó huella en el sistema.
    """
    materia = db.get_or_404(Materia, materia_id)
    plan_id = materia.id_plan_fk

    tiene_calificaciones = Calificacion.query.filter_by(id_materia_fk=materia.id).first() is not None
    tiene_inscripciones = InscripcionMateria.query.filter_by(id_materia_fk=materia.id).first() is not None

    if tiene_calificaciones or tiene_inscripciones:
        flash(f'No se puede eliminar "{materia.nombre}": ya tiene calificaciones o carga académica registradas.', 'danger')
    else:
        nombre = materia.nombre
        db.session.delete(materia)
        db.session.commit()
        flash(f'"{nombre}" eliminada del plan.', 'success')

    return redirect(url_for('configuracion.gestionar_materias', plan_id=plan_id))


@configuracion_bp.route('/conceptos-cobro', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def conceptos_cobro():
    """Catálogo de conceptos de cobro — se administra aquí, NUNCA como texto libre al capturar un cargo."""
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        monto_sugerido_raw = request.form.get('monto_sugerido', '').strip()
        es_mensualidad = request.form.get('es_mensualidad') == 'on'

        monto_sugerido = None
        if monto_sugerido_raw:
            try:
                monto_sugerido = Decimal(monto_sugerido_raw)
                if monto_sugerido < 0:
                    raise InvalidOperation
            except InvalidOperation:
                flash('El precio sugerido no es un número válido.', 'danger')
                return redirect(url_for('configuracion.conceptos_cobro'))

        if len(nombre) < 3:
            flash('El nombre del concepto debe tener al menos 3 caracteres.', 'danger')
        elif ConceptoCobro.query.filter_by(nombre=nombre).first():
            flash(f'Ya existe un concepto llamado "{nombre}".', 'danger')
        else:
            nuevo = ConceptoCobro(nombre=nombre, monto_sugerido=monto_sugerido, es_mensualidad=es_mensualidad, activo=True)
            db.session.add(nuevo)
            db.session.commit()
            flash(f'Concepto "{nombre}" agregado al catálogo.', 'success')

        return redirect(url_for('configuracion.conceptos_cobro'))

    conceptos = ConceptoCobro.query.order_by(ConceptoCobro.activo.desc(), ConceptoCobro.nombre.asc()).all()
    return render_template('conceptos_cobro.html', conceptos=conceptos)


@configuracion_bp.route('/conceptos-cobro/<int:concepto_id>/editar-precio', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def editar_precio_concepto(concepto_id):
    """
    Ajustar el precio sugerido de un concepto YA existente -- pensado
    para cuando cambian las cuotas cada año (algo poco frecuente, no
    necesita un CRUD completo, solo poder tocar el número).
    """
    concepto = db.get_or_404(ConceptoCobro, concepto_id)
    monto_sugerido_raw = request.form.get('monto_sugerido', '').strip()
    es_mensualidad = request.form.get('es_mensualidad') == 'on'

    monto_sugerido = None
    if monto_sugerido_raw:
        try:
            monto_sugerido = Decimal(monto_sugerido_raw)
            if monto_sugerido < 0:
                raise InvalidOperation
        except InvalidOperation:
            flash('El precio sugerido no es un número válido.', 'danger')
            return redirect(url_for('configuracion.conceptos_cobro'))

    concepto.monto_sugerido = monto_sugerido
    concepto.es_mensualidad = es_mensualidad
    db.session.commit()

    flash(f'Precio de "{concepto.nombre}" actualizado.', 'success')
    return redirect(url_for('configuracion.conceptos_cobro'))


@configuracion_bp.route('/conceptos-cobro/<int:concepto_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def toggle_concepto_cobro(concepto_id):
    concepto = db.get_or_404(ConceptoCobro, concepto_id)
    concepto.activo = not concepto.activo
    db.session.commit()

    estado = 'activado' if concepto.activo else 'desactivado'
    flash(f'El concepto "{concepto.nombre}" fue {estado}.', 'success')
    return redirect(url_for('configuracion.conceptos_cobro'))


@configuracion_bp.route('/configuracion/institucion', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def configuracion_institucion():
    """
    Pantalla donde Directivo configura cómo se llama la institución, cómo
    se le llama a cada periodo (Cuatrimestre / Grado / Semestre / Año /
    Trimestre...) y al programa que los agrupa (Carrera / Nivel Educativo
    / Grado Escolar...). Esto es lo que hace que el sistema sirva para
    cualquier tipo de escuela sin tocar código -- la estructura de datos
    de por sí ya es genérica, solo cambia cómo se le llama a cada cosa.
    """
    config = ConfiguracionInstitucion.obtener()

    if request.method == 'POST':
        nombre_institucion = request.form.get('nombre_institucion', '').strip()
        nombre_periodo_singular = request.form.get('nombre_periodo_singular', '').strip()
        nombre_periodo_plural = request.form.get('nombre_periodo_plural', '').strip()
        nombre_programa_singular = request.form.get('nombre_programa_singular', '').strip()
        nombre_programa_plural = request.form.get('nombre_programa_plural', '').strip()
        max_periodos_raw = request.form.get('max_periodos', '').strip()

        errores = []
        if len(nombre_institucion) < 3:
            errores.append('El nombre de la institución debe tener al menos 3 caracteres.')
        if not nombre_periodo_singular or not nombre_periodo_plural:
            errores.append('Indica cómo se llama cada periodo, en singular y en plural (ej. "Cuatrimestre" / "Cuatrimestres").')
        if not nombre_programa_singular or not nombre_programa_plural:
            errores.append('Indica cómo se llama el programa que agrupa los periodos, en singular y en plural (ej. "Carrera" / "Carreras").')

        max_periodos = None
        try:
            max_periodos = int(max_periodos_raw)
            if max_periodos < 1 or max_periodos > 30:
                errores.append('El número máximo de periodos debe estar entre 1 y 30.')
        except (ValueError, TypeError):
            errores.append('Indica un número máximo de periodos válido.')

        if errores:
            for error in errores:
                flash(error, 'danger')
        else:
            config.nombre_institucion = nombre_institucion
            config.nombre_periodo_singular = nombre_periodo_singular
            config.nombre_periodo_plural = nombre_periodo_plural
            config.nombre_programa_singular = nombre_programa_singular
            config.nombre_programa_plural = nombre_programa_plural
            config.max_periodos = max_periodos
            db.session.commit()
            flash('Configuración de la institución actualizada.', 'success')

        return redirect(url_for('configuracion.configuracion_institucion'))

    return render_template('configuracion_institucion.html', config=config)


@configuracion_bp.route('/configuracion/cobros', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def configuracion_cobros():
    """
    Configuración de recargos por atraso — auto-ajustable: cada
    universidad define su propia fórmula aquí, sin tocar código.
    """
    config = ConfiguracionCobros.obtener()

    if request.method == 'POST':
        tipo_raw = request.form.get('tipo_recargo', '')
        valor_raw = request.form.get('valor_recargo', '').strip()
        dias_gracia_raw = request.form.get('dias_gracia', '0').strip()

        errores = []

        if tipo_raw not in TipoRecargo.__members__:
            errores.append('Selecciona un tipo de recargo válido.')

        valor = None
        try:
            valor = Decimal(valor_raw)
            if valor < 0:
                errores.append('El valor del recargo no puede ser negativo.')
        except InvalidOperation:
            errores.append('El valor del recargo no es un número válido.')

        try:
            dias_gracia = int(dias_gracia_raw)
            if dias_gracia < 0:
                errores.append('Los días de gracia no pueden ser negativos.')
        except ValueError:
            errores.append('Los días de gracia deben ser un número entero.')
            dias_gracia = 0

        if errores:
            for error in errores:
                flash(error, 'danger')
            return redirect(url_for('configuracion.configuracion_cobros'))

        config.tipo_recargo = TipoRecargo[tipo_raw]
        config.valor_recargo = valor
        config.dias_gracia = dias_gracia
        db.session.commit()

        flash('Configuración de recargos actualizada correctamente.', 'success')
        return redirect(url_for('configuracion.configuracion_cobros'))

    return render_template('configuracion_cobros.html', config=config, tipos_recargo=list(TipoRecargo))
