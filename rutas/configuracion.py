"""
Configuración de la institución: mensualidades por carrera, materias por
plan, catálogo de conceptos de cobro (y sus precios), datos generales de
la institución y política de recargos. Ver deploy/PENDIENTES_PRODUCCION.md
§3: NULL en un precio significa "sin configurar", nunca se convierte en
$0 automáticamente -- no alterar esa regla al mover estas rutas.
"""


import re
from decimal import Decimal

from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import (
    PlanEstudio, Materia, ConceptoCobro, ConfiguracionCobros, ConfiguracionInstitucion,
    Calificacion, InscripcionMateria, TipoRecargo, Alumno, Cargo,
)
from utilidades.seguridad import rol_requerido
from utilidades.dinero import (
    parsear_monto, parsear_entero, MontoInvalido, MONTO_MAXIMO, RECARGO_MAXIMO_POR_DIA,
    PORCENTAJE_MAXIMO, DIAS_GRACIA_MAXIMOS,
)
from servicios.academico import _max_periodos
from servicios.auditoria import registrar
from servicios.cobros import aplicar_mensualidad_a_pendientes, aplicar_precio_a_pendientes, recalcular_recargos_vencidos, impacto_de_politica
from modelos.cobros import calcular_recargo
from servicios.matriculas import formato_matricula_cabe, ejemplo_matricula
from servicios.terminologia import terminos

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
                anterior = plan.monto_mensualidad
                plan.monto_mensualidad = monto
                detalle = f'{plan.nombre}: {anterior} -> {monto}'
                actualizados = None
                if request.form.get('aplicar_pendientes') == 'on':
                    # Solo cargos PENDIENTES sin pagos (y con la beca de cada alumno); lo ya cobrado no se toca.
                    actualizados = aplicar_mensualidad_a_pendientes(plan)
                    detalle += f'; aplicado a {actualizados} cargo(s) pendiente(s) sin pagos'
                registrar('PRECIO_MENSUALIDAD', 'PlanEstudio', plan.id, detalle)
                db.session.commit()
                flash(f'Mensualidad de "{plan.nombre}" actualizada a ${monto}.', 'success')
                if actualizados is not None:
                    flash(f'{actualizados} cargo(s) pendiente(s) sin pagos se actualizaron al nuevo precio.', 'info')

        return redirect(url_for('configuracion.planes_mensualidades'))

    planes = PlanEstudio.query.order_by(PlanEstudio.activo.desc(), PlanEstudio.nombre.asc()).all()
    alumnos_por_plan = dict(db.session.query(Alumno.id_plan_fk, func.count(Alumno.matricula_id)).group_by(Alumno.id_plan_fk).all())
    return render_template('planes_mensualidades.html', planes=planes, alumnos_por_plan=alumnos_por_plan)


# ---------------------------------------------------------------------------
# CARRERAS (planes de estudio): alta, edición, desactivación y baja
# ---------------------------------------------------------------------------
# Antes solo las creaba seed.py. Cada institución tiene las suyas. Las MATRÍCULAS ya emitidas no
# cambian nunca (son la llave de todo el expediente); clave y año solo afectan a las nuevas.

CLAVE_CARRERA_REGEX = re.compile(r'[A-Z0-9]{2,10}')


def _validar_carrera(form, plan_actual=None):
    """(datos, errores) de un formulario de carrera. Reutilizado por alta y edición."""
    errores = []
    nombre = form.get('nombre', '').strip()
    clave = form.get('clave_carrera', '').strip().upper()
    if not 3 <= len(nombre) <= 150:
        errores.append('El nombre debe tener entre 3 y 150 caracteres.')
    if not CLAVE_CARRERA_REGEX.fullmatch(clave):
        errores.append('La clave debe tener de 2 a 10 letras o números, sin espacios ni símbolos (ej. LEN).')
    anio = duracion = monto = None
    try:
        anio = parsear_entero(form.get('anio_generacion', ''), minimo=2000, maximo=2100)
    except MontoInvalido:
        errores.append('El año de generación debe estar entre 2000 y 2100.')
    if form.get('duracion_anios', '').strip():
        try:
            duracion = parsear_entero(form.get('duracion_anios', ''), minimo=1, maximo=15)
        except MontoInvalido:
            errores.append('La duración debe ser de 1 a 15 años (o dejarse vacía).')
    if 'monto_mensualidad' in form and form.get('monto_mensualidad', '').strip():
        try:
            monto = parsear_monto(form.get('monto_mensualidad'), permitir_cero=True)
        except MontoInvalido as e:
            errores.append(f'La mensualidad no es válida. {e}')
    if not errores:
        existente = PlanEstudio.query.filter_by(clave_carrera=clave, anio_generacion=anio).first()
        if existente and (plan_actual is None or existente.id != plan_actual.id):
            errores.append(f'Ya existe {terminos().un} {terminos().programa_l} con la clave {clave} y el año {anio}.')
        elif not formato_matricula_cabe(ConfiguracionInstitucion.obtener(), clave):
            errores.append('Con el formato de matrícula configurado, esa clave haría matrículas de más de 20 caracteres. '
                           'Usa una clave más corta o ajusta el formato en Configuración de la Institución.')
    return dict(nombre=nombre, clave=clave, anio=anio, duracion=duracion, monto=monto), errores


@configuracion_bp.route('/planes/nuevo', methods=['POST'])
@rol_requerido('DIRECTIVO')
def nuevo_plan():
    datos, errores = _validar_carrera(request.form)
    if errores:
        for error in errores:
            flash(error, 'danger')
        return redirect(url_for('configuracion.planes_mensualidades'))
    plan = PlanEstudio(nombre=datos['nombre'], clave_carrera=datos['clave'], anio_generacion=datos['anio'],
                       duracion_anios=datos['duracion'], monto_mensualidad=datos['monto'], activo=True)
    db.session.add(plan)
    try:
        db.session.flush()
        registrar('PLAN_CREADO', 'PlanEstudio', plan.id,
                  f'{plan.clave_carrera}-{plan.anio_generacion} {plan.nombre}; duración {plan.duracion_anios or "—"} años; '
                  f'mensualidad {plan.monto_mensualidad if plan.monto_mensualidad is not None else "sin definir"}')
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(f'Ya existe {terminos().un} {terminos().programa_l} con esa clave y ese año.', 'danger')
        return redirect(url_for('configuracion.planes_mensualidades'))
    flash(f'{terminos().programa} "{plan.nombre}" cread{terminos().a_o}. Ahora agrega sus materias y define su mensualidad.', 'success')
    return redirect(url_for('configuracion.planes_mensualidades'))


@configuracion_bp.route('/planes/<int:plan_id>/editar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def editar_plan(plan_id):
    plan = db.get_or_404(PlanEstudio, plan_id)
    datos, errores = _validar_carrera(request.form, plan_actual=plan)
    if errores:
        for error in errores:
            flash(error, 'danger')
        return redirect(url_for('configuracion.planes_mensualidades'))
    antes = f'{plan.clave_carrera}-{plan.anio_generacion} "{plan.nombre}" ({plan.duracion_anios or "—"} años)'
    plan.nombre, plan.clave_carrera, plan.anio_generacion, plan.duracion_anios = datos['nombre'], datos['clave'], datos['anio'], datos['duracion']
    registrar('PLAN_EDITADO', 'PlanEstudio', plan.id,
              f'{antes} -> {plan.clave_carrera}-{plan.anio_generacion} "{plan.nombre}" ({plan.duracion_anios or "—"} años)')
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(f'Ya existe {terminos().un} {terminos().programa_l} con esa clave y ese año.', 'danger')
        return redirect(url_for('configuracion.planes_mensualidades'))
    flash(f'{terminos().programa} "{plan.nombre}" actualizad{terminos().a_o}. Los cambios ya se ven en todo el sistema; '
          'las matrículas ya emitidas no cambian (solo las nuevas usarán la clave y año actuales).', 'success')
    return redirect(url_for('configuracion.planes_mensualidades'))


@configuracion_bp.route('/planes/<int:plan_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO')
def toggle_plan(plan_id):
    """Desactivar = deja de ofrecerse a aspirantes y en importaciones/avances; sus alumnos y su historia siguen intactos."""
    plan = db.get_or_404(PlanEstudio, plan_id)
    plan.activo = not plan.activo
    registrar('PLAN_ACTIVADO' if plan.activo else 'PLAN_DESACTIVADO', 'PlanEstudio', plan.id, f'{plan.clave_carrera}-{plan.anio_generacion} {plan.nombre}')
    db.session.commit()
    flash(f'{terminos().programa} "{plan.nombre}" {"activad" if plan.activo else "desactivad"}{terminos().a_o}' + ('.' if plan.activo else ' (ya no se ofrece a nuevos aspirantes; sus alumnos siguen igual).'), 'success')
    return redirect(url_for('configuracion.planes_mensualidades'))


@configuracion_bp.route('/planes/<int:plan_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_plan(plan_id):
    """Solo si nunca tuvo alumnos (borra también sus materias). Con alumnos, se DESACTIVA."""
    plan = db.get_or_404(PlanEstudio, plan_id)
    if Alumno.query.filter_by(id_plan_fk=plan.id).first():
        flash(f'No se puede eliminar "{plan.nombre}": ya tiene alumnos. Desactívala para que deje de ofrecerse sin perder su historia.', 'danger')
        return redirect(url_for('configuracion.planes_mensualidades'))
    nombre, total_materias = plan.nombre, len(plan.materias)
    registrar('PLAN_ELIMINADO', 'PlanEstudio', plan.id, f'{plan.clave_carrera}-{plan.anio_generacion} {nombre} ({total_materias} materia(s) eliminadas con ella)')
    db.session.delete(plan)
    db.session.commit()
    flash(f'{terminos().programa} "{nombre}" eliminad{terminos().a_o}.', 'success')
    return redirect(url_for('configuracion.planes_mensualidades'))


# ---------------------------------------------------------------------------
# MATERIAS: alta, edición, archivado y baja
# ---------------------------------------------------------------------------

_CREDITOS_REGEX = re.compile(r'[0-9]{1,3}(\.[0-9]{1,2})?')


def _validar_materia(form, plan_id, excluir_id=None):
    """(datos, errores) de un formulario de materia. Reutilizado por alta y edición."""
    errores = []
    max_cuatri = _max_periodos()
    nombre = form.get('nombre', '').strip()
    clave = form.get('clave', '').strip() or None
    if not 3 <= len(nombre) <= 150:
        errores.append('El nombre de la materia debe tener entre 3 y 150 caracteres.')
    if clave and len(clave) > 20:
        errores.append('La clave no puede pasar de 20 caracteres.')
    cuatrimestre = None
    try:
        cuatrimestre = parsear_entero(form.get('cuatrimestre', ''), minimo=1, maximo=max_cuatri)
    except MontoInvalido:
        errores.append(f'El número de {terminos().periodo_l} debe estar entre 1 y {max_cuatri}.')
    creditos = None
    creditos_raw = form.get('creditos', '').strip()
    if creditos_raw:
        if _CREDITOS_REGEX.fullmatch(creditos_raw):
            creditos = float(creditos_raw)
        else:
            errores.append('Los créditos deben ser un número de 0 a 999 (hasta 2 decimales).')
    if not errores:
        duplicada = Materia.query.filter_by(id_plan_fk=plan_id, nombre=nombre, cuatrimestre=cuatrimestre).first()
        if duplicada and duplicada.id != excluir_id:
            errores.append(f'Ya existe "{nombre}" en {cuatrimestre}° {terminos().periodo_l} de este plan.')
    return dict(nombre=nombre, clave=clave, cuatrimestre=cuatrimestre, creditos=creditos), errores


@configuracion_bp.route('/planes/<int:plan_id>/materias', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def gestionar_materias(plan_id):
    """
    Alta y consulta de materias por cuatrimestre para un plan de estudios.
    Solo Directivo: esto es lo que define el "Escudo del Plan de Estudios"
    que todo lo demás (boletas, cargos de mensualidad, carga
    académica) respeta -- un cambio aquí se propaga a todo el sistema, así que
    queda con el rol más restringido.
    """
    plan = db.get_or_404(PlanEstudio, plan_id)
    max_cuatri = _max_periodos()

    if request.method == 'POST':
        datos, errores = _validar_materia(request.form, plan.id)
        if errores:
            for error in errores:
                flash(error, 'danger')
        else:
            materia = Materia(nombre=datos['nombre'], clave=datos['clave'], cuatrimestre=datos['cuatrimestre'],
                              creditos=datos['creditos'], id_plan_fk=plan.id)
            db.session.add(materia)
            db.session.flush()
            registrar('MATERIA_CREADA', 'Materia', materia.id, f'{plan.clave_carrera}: {materia.nombre} ({materia.cuatrimestre}°)')
            db.session.commit()
            flash(f'"{materia.nombre}" agregada a {materia.cuatrimestre}° {terminos().periodo_l} de {plan.nombre}.', 'success')

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


@configuracion_bp.route('/materias/<int:materia_id>/editar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def editar_materia(materia_id):
    """
    Cambia nombre, clave, cuatrimestre o créditos. El resto del sistema (calificaciones, carga
    académica, historial, boletas) apunta a la materia por su id, así que el cambio se ve en todos
    lados a la vez y no se pierde ninguna calificación.
    """
    materia = db.get_or_404(Materia, materia_id)
    datos, errores = _validar_materia(request.form, materia.id_plan_fk, excluir_id=materia.id)
    if errores:
        for error in errores:
            flash(error, 'danger')
        return redirect(url_for('configuracion.gestionar_materias', plan_id=materia.id_plan_fk))
    antes = f'{materia.nombre} [{materia.clave or "—"}] {materia.cuatrimestre}° {materia.creditos or "—"} cr.'
    materia.nombre, materia.clave, materia.cuatrimestre, materia.creditos = datos['nombre'], datos['clave'], datos['cuatrimestre'], datos['creditos']
    registrar('MATERIA_EDITADA', 'Materia', materia.id, f'{antes} -> {materia.nombre} [{materia.clave or "—"}] {materia.cuatrimestre}° {materia.creditos or "—"} cr.')
    db.session.commit()
    flash(f'Materia "{materia.nombre}" actualizada en todo el sistema.', 'success')
    return redirect(url_for('configuracion.gestionar_materias', plan_id=materia.id_plan_fk))


@configuracion_bp.route('/materias/<int:materia_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO')
def toggle_materia(materia_id):
    """Archivar = deja de ofrecerse (boletas, carga académica, egreso) pero conserva calificaciones e historial."""
    materia = db.get_or_404(Materia, materia_id)
    materia.activa = not materia.activa
    registrar('MATERIA_REACTIVADA' if materia.activa else 'MATERIA_ARCHIVADA', 'Materia', materia.id, f'{materia.nombre} ({materia.cuatrimestre}°)')
    db.session.commit()
    flash(f'"{materia.nombre}" {"reactivada" if materia.activa else "archivada: ya no se ofrece en boletas ni cargas nuevas, pero su historial se conserva"}.', 'success')
    return redirect(url_for('configuracion.gestionar_materias', plan_id=materia.id_plan_fk))


@configuracion_bp.route('/materias/<int:materia_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_materia(materia_id):
    """
    Solo permite eliminar una materia si NUNCA se usó -- ni calificaciones
    ni carga académica registradas con ella. Con uso, se ARCHIVA (no se pierde nada).
    """
    materia = db.get_or_404(Materia, materia_id)
    plan_id = materia.id_plan_fk

    tiene_calificaciones = Calificacion.query.filter_by(id_materia_fk=materia.id).first() is not None
    tiene_inscripciones = InscripcionMateria.query.filter_by(id_materia_fk=materia.id).first() is not None

    if tiene_calificaciones or tiene_inscripciones:
        flash(f'No se puede eliminar "{materia.nombre}": ya tiene calificaciones o carga académica registradas. '
              'Archívala para que deje de ofrecerse sin perder ese historial.', 'danger')
    else:
        nombre = materia.nombre
        registrar('MATERIA_ELIMINADA', 'Materia', materia.id, f'{nombre} ({materia.cuatrimestre}°)')
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

    detalle = (
        f'{concepto.nombre}: {concepto.monto_sugerido if concepto.monto_sugerido is not None else "sin definir"} -> '
        f'{monto_sugerido if monto_sugerido is not None else "sin definir"}; mensualidad {concepto.es_mensualidad} -> {es_mensualidad}'
    )
    concepto.monto_sugerido = monto_sugerido
    concepto.es_mensualidad = es_mensualidad
    actualizados = None
    if request.form.get('aplicar_pendientes') == 'on' and monto_sugerido is not None and not es_mensualidad:
        # Solo cargos PENDIENTES sin pagos de este concepto; lo ya cobrado no se toca.
        actualizados = aplicar_precio_a_pendientes(concepto, monto_sugerido)
        detalle += f'; aplicado a {actualizados} cargo(s) pendiente(s) sin pagos'
    registrar('PRECIO_CONCEPTO', 'ConceptoCobro', concepto.id, detalle)
    try:
        db.session.commit()
        flash(f'Precio de "{concepto.nombre}" actualizado.', 'success')
        if actualizados is not None:
            flash(f'{actualizados} cargo(s) pendiente(s) sin pagos se actualizaron al nuevo precio.', 'info')
    except IntegrityError:
        db.session.rollback()
        flash(
            'Ya existe un concepto marcado como mensualidad activo. '
            'Desactívalo antes de marcar otro.',
            'danger'
        )

    return redirect(url_for('configuracion.conceptos_cobro'))


@configuracion_bp.route('/conceptos-cobro/<int:concepto_id>/renombrar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def renombrar_concepto(concepto_id):
    """
    Cambia el nombre de un concepto y lo actualiza en TODOS los cargos que ya lo usan (el cargo guarda
    una copia del nombre para mostrarlo sin joins). El cambio queda en la bitácora.
    """
    concepto = db.get_or_404(ConceptoCobro, concepto_id)
    nuevo = request.form.get('nombre', '').strip()
    if not 3 <= len(nuevo) <= 100:
        flash('El nombre del concepto debe tener entre 3 y 100 caracteres.', 'danger')
        return redirect(url_for('configuracion.conceptos_cobro'))
    repetido = ConceptoCobro.query.filter(func.lower(ConceptoCobro.nombre) == nuevo.lower(), ConceptoCobro.id != concepto.id).first()
    if repetido:
        flash(f'Ya existe un concepto llamado "{repetido.nombre}".', 'danger')
        return redirect(url_for('configuracion.conceptos_cobro'))
    anterior = concepto.nombre
    concepto.nombre = nuevo
    actualizados = Cargo.query.filter_by(concepto_cobro_fk=concepto.id).update({'concepto': nuevo}, synchronize_session=False)
    registrar('CONCEPTO_RENOMBRADO', 'ConceptoCobro', concepto.id, f'"{anterior}" -> "{nuevo}"; {actualizados} cargo(s) actualizados')
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(f'Ya existe un concepto llamado "{nuevo}".', 'danger')
        return redirect(url_for('configuracion.conceptos_cobro'))
    flash(f'Concepto renombrado a "{nuevo}" ({actualizados} cargo(s) existentes actualizados).', 'success')
    return redirect(url_for('configuracion.conceptos_cobro'))


@configuracion_bp.route('/conceptos-cobro/<int:concepto_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def eliminar_concepto(concepto_id):
    """Solo si nunca se cobró; si ya tiene cargos se DESACTIVA (así no se pierde el historial de cobros)."""
    concepto = db.get_or_404(ConceptoCobro, concepto_id)
    if Cargo.query.filter_by(concepto_cobro_fk=concepto.id).first():
        flash(f'No se puede eliminar "{concepto.nombre}": ya tiene cargos. Desactívalo para que deje de ofrecerse sin perder el historial.', 'danger')
        return redirect(url_for('configuracion.conceptos_cobro'))
    nombre = concepto.nombre
    registrar('CONCEPTO_ELIMINADO', 'ConceptoCobro', concepto.id, nombre)
    db.session.delete(concepto)
    db.session.commit()
    flash(f'Concepto "{nombre}" eliminado.', 'success')
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


_CARACTERES_DE_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_ROTULO = re.compile(r"[\w .'\-]+")          # letras (con acentos), números, espacio, punto, guion, apóstrofo


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
    El nombre y la terminología se usan en TODAS las pantallas, documentos impresos,
    correos y exportaciones (ver servicios/terminologia.py).
    """
    config = ConfiguracionInstitucion.obtener()

    if request.method == 'POST':
        errores = []

        def leer(campo, etiqueta, minimo, maximo, patron=None):
            crudo = request.form.get(campo, '')
            if _CARACTERES_DE_CONTROL.search(crudo):
                errores.append(f'{etiqueta}: no puede llevar saltos de línea ni caracteres de control.')
                return None
            limpio = re.sub(r'\s+', ' ', crudo).strip()          # espacios repetidos -> uno
            if not minimo <= len(limpio) <= maximo:
                errores.append(f'{etiqueta}: debe tener entre {minimo} y {maximo} caracteres.')
                return None
            if patron is not None and not patron.fullmatch(limpio):
                errores.append(f'{etiqueta}: solo letras, números, espacios, punto, guion y apóstrofo.')
                return None
            return limpio

        nombre_institucion = leer('nombre_institucion', 'Nombre de la institución', 3, 150)
        periodo_s = leer('nombre_periodo_singular', 'Nombre del periodo (singular)', 1, 40, _ROTULO)
        periodo_p = leer('nombre_periodo_plural', 'Nombre del periodo (plural)', 1, 40, _ROTULO)
        programa_s = leer('nombre_programa_singular', 'Nombre del programa (singular)', 1, 40, _ROTULO)
        programa_p = leer('nombre_programa_plural', 'Nombre del programa (plural)', 1, 40, _ROTULO)

        genero = request.form.get('programa_genero')
        if genero is not None and genero not in ('femenino', 'masculino'):
            errores.append('Elige si el programa se dice en femenino (la carrera) o masculino (el programa).')

        max_periodos = None
        try:
            max_periodos = parsear_entero(request.form.get('max_periodos', ''), minimo=1, maximo=30)
        except MontoInvalido:
            errores.append('El número máximo de periodos debe ser un entero entre 1 y 30.')
        if max_periodos is not None:
            # Bajar el máximo por debajo de lo que ya se usa dejaría materias y alumnos "fuera de rango":
            # desaparecerían de las pantallas (que recorren de 1 al máximo) sin poder verse ni editarse.
            usado = max(db.session.query(func.max(Materia.cuatrimestre)).scalar() or 0,
                        db.session.query(func.max(Alumno.cuatrimestre_actual)).scalar() or 0)
            if max_periodos < usado:
                errores.append(f'No puedes bajar el máximo a {max_periodos}: ya hay materias o alumnos en {usado}° {config.nombre_periodo_singular.lower()}. '
                               'Muévelos o elimínalos primero.')

        # --- Formato de matrícula (solo se procesa si el formulario lo trae) ---
        formato = None
        if 'matricula_digitos' in request.form:
            prefijo = request.form.get('matricula_prefijo', '').strip()
            separador = request.form.get('matricula_separador', '')
            digitos = None
            if not re.fullmatch(r'[A-Za-z0-9]{0,6}', prefijo):
                errores.append('El prefijo de la matrícula admite hasta 6 letras o números, sin espacios ni símbolos.')
            if not re.fullmatch(r'[-._]{0,2}', separador):
                errores.append('El separador de la matrícula puede ser vacío o hasta 2 de estos símbolos: - . _')
            try:
                digitos = parsear_entero(request.form.get('matricula_digitos', ''), minimo=3, maximo=8)
            except MontoInvalido:
                errores.append('Los dígitos del consecutivo deben ser un número de 3 a 8.')
            formato = dict(
                matricula_prefijo=prefijo,
                matricula_incluye_clave=request.form.get('matricula_incluye_clave') == 'on',
                matricula_incluye_anio=request.form.get('matricula_incluye_anio') == 'on',
                matricula_separador=separador,
                matricula_digitos=digitos,
            )
            if not errores:
                # La matrícula más larga posible debe caber en 20 caracteres (Alumno.matricula_id).
                clave_mas_larga = max((p.clave_carrera for p in PlanEstudio.query.all()), key=len, default='LEN')
                if not formato_matricula_cabe(config, clave_mas_larga, **formato):
                    errores.append(f'Con ese formato y la clave más larga ({clave_mas_larga}) las matrículas pasarían de 20 caracteres. Acórtalo.')

        if errores:
            for error in errores:
                flash(error, 'danger')
            return redirect(url_for('configuracion.configuracion_institucion'))

        cambios = []
        def cambiar(etiqueta, actual, nuevo):
            if actual != nuevo:
                cambios.append(f'{etiqueta}: "{actual}" -> "{nuevo}"')

        cambiar('Institución', config.nombre_institucion, nombre_institucion)
        cambiar('Periodo (singular)', config.nombre_periodo_singular, periodo_s)
        cambiar('Periodo (plural)', config.nombre_periodo_plural, periodo_p)
        cambiar('Programa (singular)', config.nombre_programa_singular, programa_s)
        cambiar('Programa (plural)', config.nombre_programa_plural, programa_p)
        cambiar('Máximo de periodos', config.max_periodos, max_periodos)
        if genero is not None:
            cambiar('Género del programa', 'femenino' if config.programa_es_femenino else 'masculino', genero)
            config.programa_es_femenino = genero == 'femenino'
        config.nombre_institucion, config.nombre_periodo_singular, config.nombre_periodo_plural = nombre_institucion, periodo_s, periodo_p
        config.nombre_programa_singular, config.nombre_programa_plural, config.max_periodos = programa_s, programa_p, max_periodos

        if formato is not None:
            antes = ejemplo_matricula(config)
            for campo, valor in formato.items():
                setattr(config, campo, valor)
            if ejemplo_matricula(config) != antes:
                registrar('CONFIG_MATRICULA', 'ConfiguracionInstitucion', config.id,
                          f'Formato de matrícula: ejemplo {antes} -> {ejemplo_matricula(config)} '
                          f'(prefijo "{config.matricula_prefijo}", clave={config.matricula_incluye_clave}, '
                          f'año={config.matricula_incluye_anio}, separador "{config.matricula_separador}", dígitos {config.matricula_digitos}). '
                          'Solo aplica a matrículas nuevas.')
        if cambios:
            registrar('CONFIG_INSTITUCION', 'ConfiguracionInstitucion', config.id, '; '.join(cambios))
        db.session.commit()
        flash('Configuración de la institución actualizada. Los cambios ya se ven en fichas, boletas, recibos, correos y demás pantallas.', 'success')
        return redirect(url_for('configuracion.configuracion_institucion'))

    return render_template('configuracion_institucion.html', config=config, ejemplo_matricula=ejemplo_matricula(config))


def _validar_politica_recargo(tipo_raw, valor_raw, dias_gracia_raw):
    """(tipo, valor, dias_gracia, errores) de una política de recargos. La usan guardar y simular."""
    errores = []
    tipo = TipoRecargo[tipo_raw] if tipo_raw in TipoRecargo.__members__ else None
    if tipo is None:
        errores.append('Selecciona un tipo de recargo válido.')

    # Cada tipo de recargo tiene su propio tope: un porcentaje no pasa de 100 y un monto por día
    # no puede ser de millones (un typo aquí se propaga a todos los cargos vencidos).
    if tipo_raw in ('PORCENTAJE', 'PORCENTAJE_MENSUAL'):
        tope = PORCENTAJE_MAXIMO
    elif tipo_raw == 'POR_DIA':
        tope = RECARGO_MAXIMO_POR_DIA
    else:
        tope = MONTO_MAXIMO
    valor = None
    try:
        valor = parsear_monto((valor_raw or '').strip(), permitir_cero=True, maximo=tope)
    except MontoInvalido as e:
        errores.append(f'El valor del recargo no es válido. {e}')

    dias_gracia = 0
    try:
        dias_gracia = parsear_entero((dias_gracia_raw or '0').strip() or '0', minimo=0, maximo=DIAS_GRACIA_MAXIMOS)
    except MontoInvalido as e:
        errores.append(f'Los días de gracia no son válidos. {e}')
    return tipo, valor, dias_gracia, errores


@configuracion_bp.route('/configuracion/cobros/simular')
@rol_requerido('DIRECTIVO', 'CONTADOR')
def simular_recargo():
    """
    Simulador: cuánto recargo daría una política (sin guardarla) sobre un cargo de ejemplo a distintos días
    de atraso, y cómo afectaría HOY a los cargos ya vencidos. Usa la MISMA fórmula que el cálculo real.
    """
    tipo, valor, dias_gracia, errores = _validar_politica_recargo(
        request.args.get('tipo_recargo', ''), request.args.get('valor_recargo', ''), request.args.get('dias_gracia', '0'))
    monto = None
    try:
        monto = parsear_monto(request.args.get('monto', '2500') or '2500')
    except MontoInvalido as e:
        errores.append(f'El monto de ejemplo no es válido. {e}')
    if errores:
        return jsonify(error=' '.join(errores)), 400

    ejemplos = []
    for dias in (1, 3, 5, 10, 15, 30, 45, 60, 90):
        recargo = calcular_recargo(monto, dias, tipo, valor, dias_gracia)
        ejemplos.append(dict(dias=dias, recargo=str(recargo), total=str(monto + recargo)))
    impacto = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in impacto_de_politica(tipo, valor, dias_gracia).items()}
    return jsonify(ejemplos=ejemplos, impacto=impacto, monto=str(monto))


@configuracion_bp.route('/configuracion/cobros', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def configuracion_cobros():
    """
    Configuración de recargos por atraso — auto-ajustable: cada
    universidad define su propia fórmula aquí, sin tocar código.

    El recargo de un cargo vencido solo SUBE por sí solo (así nunca se borra un recargo que ya se comunicó).
    Para aplicar una política más baja a los cargos ya vencidos hay que pedirlo explícitamente
    (casilla "recalcular"), y el sistema nunca deja un saldo a favor.
    """
    config = ConfiguracionCobros.obtener()

    if request.method == 'POST':
        tipo, valor, dias_gracia, errores = _validar_politica_recargo(
            request.form.get('tipo_recargo', ''), request.form.get('valor_recargo', ''), request.form.get('dias_gracia', '0'))

        if errores:
            for error in errores:
                flash(error, 'danger')
            return redirect(url_for('configuracion.configuracion_cobros'))

        detalle = f'{config.tipo_recargo.name} {config.valor_recargo} gracia {config.dias_gracia} -> {tipo.name} {valor} gracia {dias_gracia}'
        config.tipo_recargo = tipo
        config.valor_recargo = valor
        config.dias_gracia = dias_gracia

        resumen = None
        if request.form.get('recalcular_vencidos') == 'on':
            resumen = recalcular_recargos_vencidos(config)
            detalle += (f'; recalculados {resumen["cambiados"]} cargo(s) vencidos ({resumen["suben"]} suben, {resumen["bajan"]} bajan): '
                        f'recargos ${resumen["total_antes"]} -> ${resumen["total_despues"]}')
        registrar('CONFIG_RECARGOS', 'ConfiguracionCobros', config.id, detalle)
        db.session.commit()

        flash('Configuración de recargos actualizada correctamente.', 'success')
        if resumen is not None:
            flash(f'Se recalcularon {resumen["cambiados"]} cargo(s) ya vencidos: {resumen["suben"]} subieron y {resumen["bajan"]} bajaron '
                  f'(recargos ${resumen["total_antes"]} → ${resumen["total_despues"]}).', 'info')
        return redirect(url_for('configuracion.configuracion_cobros'))

    return render_template('configuracion_cobros.html', config=config, tipos_recargo=list(TipoRecargo),
                           impacto=impacto_de_politica(config.tipo_recargo, config.valor_recargo, config.dias_gracia))
