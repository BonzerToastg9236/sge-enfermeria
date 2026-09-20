"""
Configuración de la institución: mensualidades por carrera, materias por
plan, catálogo de conceptos de cobro (y sus precios), datos generales de
la institución y política de recargos. Ver deploy/PENDIENTES_PRODUCCION.md
§3: NULL en un precio significa "sin configurar", nunca se convierte en
$0 automáticamente -- no alterar esa regla al mover estas rutas.
"""


from flask import Blueprint, render_template, request, flash, redirect, url_for
from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import (
    PlanEstudio, Materia, ConceptoCobro, ConfiguracionCobros, ConfiguracionInstitucion,
    Calificacion, InscripcionMateria, TipoRecargo,
)
from utilidades.seguridad import rol_requerido
from utilidades.dinero import (
    parsear_monto, parsear_entero, MontoInvalido, MONTO_MAXIMO, RECARGO_MAXIMO_POR_DIA,
    PORCENTAJE_MAXIMO, DIAS_GRACIA_MAXIMOS,
)
from servicios.academico import _max_periodos
from servicios.auditoria import registrar

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
        plan = db.get_or_404(PlanEstudio, int(plan_id)) if plan_id.isascii() and plan_id.isdigit() else None

        if not plan:
            flash('Plan de estudios no encontrado.', 'danger')
        elif not monto_raw:
            registrar('PRECIO_MENSUALIDAD', 'PlanEstudio', plan.id, f'{plan.nombre}: {plan.monto_mensualidad} -> sin definir')
            # Campo vacío = "No definido" a propósito (así lo indica el
            # placeholder del formulario). Antes esto siempre fallaba la
            # validación de Decimal('') y nunca se podía volver a dejar sin
            # definir una mensualidad ya configurada.
            plan.monto_mensualidad = None
            db.session.commit()
            flash(f'Mensualidad de "{plan.nombre}" eliminada (queda sin definir).', 'success')
        else:
            try:
                # 0 es válido: la institución decidió que esa carrera es gratuita.
                monto = parsear_monto(monto_raw, permitir_cero=True)
            except MontoInvalido as e:
                flash(f'La mensualidad no es válida. {e}', 'danger')
            else:
                registrar('PRECIO_MENSUALIDAD', 'PlanEstudio', plan.id, f'{plan.nombre}: {plan.monto_mensualidad} -> {monto}')
                plan.monto_mensualidad = monto
                db.session.commit()
                flash(f'Mensualidad de "{plan.nombre}" actualizada a ${monto}.', 'success')

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
                monto_sugerido = parsear_monto(monto_sugerido_raw, permitir_cero=True)
            except MontoInvalido as e:
                flash(f'El precio sugerido no es válido. {e}', 'danger')
                return redirect(url_for('configuracion.conceptos_cobro'))

        if len(nombre) < 3:
            flash('El nombre del concepto debe tener al menos 3 caracteres.', 'danger')
        elif ConceptoCobro.query.filter_by(nombre=nombre).first():
            flash(f'Ya existe un concepto llamado "{nombre}".', 'danger')
        else:
            nuevo = ConceptoCobro(nombre=nombre, monto_sugerido=monto_sugerido, es_mensualidad=es_mensualidad, activo=True)
            db.session.add(nuevo)
            try:
                db.session.flush()
                registrar('CONCEPTO_CREADO', 'ConceptoCobro', nuevo.id, f'{nombre}: precio {monto_sugerido if monto_sugerido is not None else "sin definir"}, mensualidad={es_mensualidad}')
                db.session.commit()
                flash(f'Concepto "{nombre}" agregado al catálogo.', 'success')
            except IntegrityError:
                # Índice único parcial (migración b0e4f9d2a1c7): a lo más un
                # concepto puede ser es_mensualidad=True y activo=True.
                db.session.rollback()
                flash(
                    'Ya existe un concepto marcado como mensualidad activo. '
                    'Desactívalo antes de marcar otro.',
                    'danger'
                )

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
            monto_sugerido = parsear_monto(monto_sugerido_raw, permitir_cero=True)
        except MontoInvalido as e:
            flash(f'El precio sugerido no es válido. {e}', 'danger')
            return redirect(url_for('configuracion.conceptos_cobro'))

    registrar(
        'PRECIO_CONCEPTO', 'ConceptoCobro', concepto.id,
        f'{concepto.nombre}: {concepto.monto_sugerido if concepto.monto_sugerido is not None else "sin definir"} -> '
        f'{monto_sugerido if monto_sugerido is not None else "sin definir"}; mensualidad {concepto.es_mensualidad} -> {es_mensualidad}',
    )
    concepto.monto_sugerido = monto_sugerido
    concepto.es_mensualidad = es_mensualidad
    try:
        db.session.commit()
        flash(f'Precio de "{concepto.nombre}" actualizado.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash(
            'Ya existe un concepto marcado como mensualidad activo. '
            'Desactívalo antes de marcar otro.',
            'danger'
        )

    return redirect(url_for('configuracion.conceptos_cobro'))


@configuracion_bp.route('/conceptos-cobro/<int:concepto_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def toggle_concepto_cobro(concepto_id):
    concepto = db.get_or_404(ConceptoCobro, concepto_id)
    concepto.activo = not concepto.activo
    registrar('CONCEPTO_ACTIVADO' if concepto.activo else 'CONCEPTO_DESACTIVADO', 'ConceptoCobro', concepto.id, concepto.nombre)
    try:
        db.session.commit()
        estado = 'activado' if concepto.activo else 'desactivado'
        flash(f'El concepto "{concepto.nombre}" fue {estado}.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash(
            f'No se pudo activar "{concepto.nombre}": ya existe otro concepto '
            'marcado como mensualidad activo. Desactívalo primero.',
            'danger'
        )

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

        # Cada tipo de recargo tiene su propio tope: un porcentaje no pasa de
        # 100 y un monto por día no puede ser de millones (el recargo nunca
        # baja solo, así que un typo aquí no se revierte).
        if tipo_raw in ('PORCENTAJE', 'PORCENTAJE_MENSUAL'):
            tope = PORCENTAJE_MAXIMO
        elif tipo_raw == 'POR_DIA':
            tope = RECARGO_MAXIMO_POR_DIA
        else:
            tope = MONTO_MAXIMO
        valor = None
        try:
            valor = parsear_monto(valor_raw, permitir_cero=True, maximo=tope)
        except MontoInvalido as e:
            errores.append(f'El valor del recargo no es válido. {e}')

        dias_gracia = 0
        try:
            dias_gracia = parsear_entero(dias_gracia_raw or '0', minimo=0, maximo=DIAS_GRACIA_MAXIMOS)
        except MontoInvalido as e:
            errores.append(f'Los días de gracia no son válidos. {e}')

        if errores:
            for error in errores:
                flash(error, 'danger')
            return redirect(url_for('configuracion.configuracion_cobros'))

        registrar(
            'CONFIG_RECARGOS', 'ConfiguracionCobros', config.id,
            f'{config.tipo_recargo.name} {config.valor_recargo} gracia {config.dias_gracia} -> {tipo_raw} {valor} gracia {dias_gracia}',
        )
        config.tipo_recargo = TipoRecargo[tipo_raw]
        config.valor_recargo = valor
        config.dias_gracia = dias_gracia
        db.session.commit()

        flash('Configuración de recargos actualizada correctamente.', 'success')
        return redirect(url_for('configuracion.configuracion_cobros'))

    return render_template('configuracion_cobros.html', config=config, tipos_recargo=list(TipoRecargo))
