"""
Módulo académico: boletas de calificaciones (captura individual + carga
masiva desde Excel), historial y carga académica del alumno.
"""

import io

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from flask import Blueprint, render_template, request, flash, redirect, url_for, send_file
from flask_login import current_user

from extensiones import db
from modelos import (
    Alumno, Materia, PlanEstudio, Calificacion, InscripcionMateria,
    EstatusAlumno, HistorialCalificacion,
)
from utilidades.seguridad import rol_requerido
from utilidades.fechas import periodo_escolar_actual
from utilidades.archivos import valor_seguro_excel, error_de_tamano_xlsx, error_de_dimensiones_hoja
from utilidades.dinero import parsear_calificacion, MontoInvalido
from servicios.terminologia import terminos
from servicios.academico import _max_periodos, _registrar_historial_calificacion, _siguiente_numero_acta

academico_bp = Blueprint('academico', __name__)


# ---------------------------------------------------------------------------
# MÓDULO DE CAPTURA DE CALIFICACIONES (BOLETA)
# Solo Control Escolar captura, con base en actas físicas firmadas.
# El "Escudo" se aplica filtrando SIEMPRE por id_plan_fk del alumno.
# ---------------------------------------------------------------------------

@academico_bp.route('/alumno/<matricula>/boleta', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def boleta(matricula):
    alumno = db.get_or_404(Alumno, matricula)

    cuatrimestre_seleccionado = request.values.get('cuatrimestre', type=int)
    if not cuatrimestre_seleccionado:
        cuatrimestre_seleccionado = alumno.cuatrimestre_actual or 1

    # ESCUDO DEL PLAN DE ESTUDIOS: solo materias de ESTE plan y ESTE cuatrimestre.
    # Nunca se ofrece (ni se acepta) una materia fuera de esta consulta.
    materias = (
        Materia.query
        .filter_by(id_plan_fk=alumno.id_plan_fk, cuatrimestre=cuatrimestre_seleccionado, activa=True)
        .order_by(Materia.nombre.asc())
        .all()
    )

    if request.method == 'POST':
        periodo_escolar = request.form.get('periodo_escolar', '').strip()
        # SECURITY-NOTE: antes venía de un <input> de texto libre en el
        # formulario, así que cualquiera podía escribir el nombre de otra
        # persona ahí -- el registro de auditoría no era confiable. Ahora
        # se toma directo de la sesión autenticada, igual que ya se hacía
        # en Cargo.generado_por_fk y Pago.capturado_por_fk.
        capturado_por = current_user.nombre_completo

        if not periodo_escolar:
            flash('Indica el periodo escolar (ej. "2026-A") antes de guardar.', 'danger')
            return redirect(url_for('academico.boleta', matricula=matricula, cuatrimestre=cuatrimestre_seleccionado))

        # Número de Acta: ya NO es un campo editable -- se genera un folio
        # nuevo por cada envío del formulario (estilo "ACTA-2026-000042",
        # igual que Pago.folio), y se aplica por igual a todas las
        # materias que sí se guarden en este mismo envío.
        numero_acta = _siguiente_numero_acta()

        guardadas = 0
        for materia in materias:
            valor_raw = request.form.get(f'calificacion_{materia.id}', '').strip()

            if valor_raw == '':
                continue  # Casilla vacía = aún no se captura, se omite sin error

            try:
                valor = parsear_calificacion(valor_raw)
            except MontoInvalido as e:
                flash(f'La calificación de "{materia.nombre}" no es válida. {e}', 'danger')
                continue

            # Doble verificación del "Escudo" a nivel de objeto, por si acaso.
            if not alumno.materia_pertenece_a_su_plan(materia):
                flash('Se rechazó una materia que no pertenece al plan del alumno.', 'danger')
                continue

            existente = Calificacion.query.filter_by(
                matricula_fk=alumno.matricula_id,
                id_materia_fk=materia.id,
                periodo_escolar=periodo_escolar
            ).first()

            if existente:
                if existente.calificacion_final != valor:
                    _registrar_historial_calificacion(alumno, materia, periodo_escolar, existente.calificacion_final, valor)
                existente.calificacion_final = valor
                existente.numero_acta = numero_acta
                existente.capturado_por = capturado_por
            else:
                _registrar_historial_calificacion(alumno, materia, periodo_escolar, None, valor)
                db.session.add(Calificacion(
                    matricula_fk=alumno.matricula_id,
                    id_materia_fk=materia.id,
                    calificacion_final=valor,
                    periodo_escolar=periodo_escolar,
                    numero_acta=numero_acta,
                    capturado_por=capturado_por,
                ))
            guardadas += 1

        db.session.commit()
        flash(f'Se guardaron {guardadas} calificación(es) de {cuatrimestre_seleccionado}° {terminos().periodo_l}.', 'success')
        return redirect(url_for('academico.boleta', matricula=matricula, cuatrimestre=cuatrimestre_seleccionado))

    # Precargar calificaciones existentes de las materias de este cuatrimestre
    calificaciones_existentes = {
        c.id_materia_fk: c
        for c in alumno.calificaciones
        if c.materia.cuatrimestre == cuatrimestre_seleccionado
    }

    return render_template(
        'boleta.html',
        alumno=alumno,
        materias=materias,
        cuatrimestre_seleccionado=cuatrimestre_seleccionado,
        max_cuatrimestres=_max_periodos(),
        calificaciones_existentes=calificaciones_existentes,
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@academico_bp.route('/boletas/importar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def boletas_importar():
    """
    Pantalla de carga masiva de calificaciones -- misma idea que la carga
    masiva de alumnos, pero la plantilla es DINÁMICA: una columna por cada
    materia del plan+cuatrimestre elegido, porque cada carrera tiene
    materias distintas.
    """
    planes = PlanEstudio.query.filter_by(activo=True).order_by(PlanEstudio.nombre.asc()).all()
    return render_template(
        'boletas_importar.html',
        planes=planes,
        max_cuatrimestres=_max_periodos(),
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@academico_bp.route('/boletas/importar/plantilla')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def plantilla_boletas():
    """
    Genera la plantilla .xlsx para el plan+cuatrimestre pedidos: una
    columna por cada materia de ESE plan y cuatrimestre (Escudo del Plan
    de Estudios aplicado también aquí: jamás se mezclan materias de
    otras carreras), con todos los alumnos ACTIVOS de esa carrera
    precargados por matrícula.
    """
    plan_id = request.args.get('plan_id', type=int)
    cuatrimestre = request.args.get('cuatrimestre', type=int)

    plan = db.get_or_404(PlanEstudio, plan_id) if plan_id else None
    if not plan or not cuatrimestre:
        flash(f'Selecciona {terminos().programa_l} y {terminos().periodo_l} antes de descargar la plantilla.', 'danger')
        return redirect(url_for('academico.boletas_importar'))

    materias = (
        Materia.query
        .filter_by(id_plan_fk=plan.id, cuatrimestre=cuatrimestre, activa=True)
        .order_by(Materia.nombre.asc())
        .all()
    )
    if not materias:
        flash(f'"{plan.nombre}" no tiene materias registradas en {cuatrimestre}° {terminos().periodo_l}.', 'danger')
        return redirect(url_for('academico.boletas_importar'))

    alumnos = (
        Alumno.query
        .filter_by(id_plan_fk=plan.id, estatus=EstatusAlumno.ACTIVO)
        .order_by(Alumno.nombre_completo.asc())
        .all()
    )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Calificaciones'

    fuente_encabezado = Font(bold=True, color='FFFFFF')
    relleno_fijo = PatternFill(start_color='0D6EFD', end_color='0D6EFD', fill_type='solid')
    relleno_materia = PatternFill(start_color='198754', end_color='198754', fill_type='solid')

    encabezados = ['Matrícula', 'Nombre Completo'] + [m.nombre for m in materias]
    for col_idx, nombre_col in enumerate(encabezados, start=1):
        celda = ws.cell(row=1, column=col_idx, value=nombre_col)
        celda.font = fuente_encabezado
        celda.fill = relleno_fijo if col_idx <= 2 else relleno_materia
        ws.column_dimensions[get_column_letter(col_idx)].width = 24

    for row_idx, alumno in enumerate(alumnos, start=2):
        ws.cell(row=row_idx, column=1, value=alumno.matricula_id)
        ws.cell(row=row_idx, column=2, value=valor_seguro_excel(alumno.nombre_completo))

    ws.freeze_panes = 'C2'

    ws_guia = wb.create_sheet('Guía')
    ws_guia.append([terminos().programa, plan.nombre])
    ws_guia.append([terminos().periodo, cuatrimestre])
    ws_guia.append([])
    ws_guia.append(['Instrucciones'])
    ws_guia.append(['No cambies los encabezados ni la columna Matrícula.'])
    ws_guia.append(['Calificación en escala 0-10. Deja en blanco lo que aún no se captura.'])
    ws_guia.append(['El Periodo Escolar y el Número de Acta se capturan UNA vez al subir el archivo, no aquí.'])
    ws_guia.column_dimensions['A'].width = 60

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    nombre_archivo = f'plantilla_boletas_{plan.clave_carrera}_{cuatrimestre}cuatri.xlsx'
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nombre_archivo,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@academico_bp.route('/boletas/importar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def importar_boletas():
    plan_id = request.form.get('plan_id', type=int)
    cuatrimestre = request.form.get('cuatrimestre', type=int)
    periodo_escolar = request.form.get('periodo_escolar', '').strip()
    # Ya no es texto libre: un folio nuevo por archivo subido, compartido
    # por todas las filas que se guarden en esta carga (ver boleta() y
    # _siguiente_numero_acta() para el mismo criterio en captura individual).
    numero_acta = _siguiente_numero_acta()
    capturado_por = current_user.nombre_completo

    planes = PlanEstudio.query.filter_by(activo=True).order_by(PlanEstudio.nombre.asc()).all()

    plan = db.session.get(PlanEstudio, plan_id) if plan_id else None
    if not plan or not cuatrimestre:
        flash(f'Selecciona {terminos().programa_l} y {terminos().periodo_l}.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    if not periodo_escolar:
        flash('Indica el periodo escolar (ej. "2026-B") antes de subir el archivo.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    archivo = request.files.get('archivo_excel')
    if not archivo or archivo.filename == '':
        flash('Selecciona el archivo Excel (.xlsx) lleno.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    if not archivo.filename.lower().endswith('.xlsx'):
        flash('El archivo debe tener formato .xlsx. Usa la plantilla descargable.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    # ESCUDO DEL PLAN DE ESTUDIOS: las materias del archivo son SIEMPRE las
    # de este plan+cuatrimestre -- nunca se leen materias del archivo por
    # nombre libre sin cruzarlas contra esta consulta.
    materias = (
        Materia.query
        .filter_by(id_plan_fk=plan.id, cuatrimestre=cuatrimestre, activa=True)
        .order_by(Materia.nombre.asc())
        .all()
    )

    error_archivo = error_de_tamano_xlsx(archivo.stream)
    if error_archivo:
        flash(error_archivo, 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    try:
        wb = openpyxl.load_workbook(archivo, data_only=True)
        ws = wb['Calificaciones'] if 'Calificaciones' in wb.sheetnames else wb.active
    except Exception:
        flash('No se pudo leer el archivo. Verifica que sea un .xlsx válido generado con la plantilla.', 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    error_hoja = error_de_dimensiones_hoja(ws)
    if error_hoja:
        flash(error_hoja, 'danger')
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    encabezados = [(c.value.strip() if isinstance(c.value, str) else c.value) for c in ws[1]]
    nombres_materias_esperadas = [m.nombre for m in materias]

    if encabezados[:2] != ['Matrícula', 'Nombre Completo'] or encabezados[2:] != nombres_materias_esperadas:
        flash(
            'Las columnas del archivo no coinciden con las materias de '
            f'"{plan.nombre}" - {cuatrimestre}° {terminos().periodo_l}. Descarga la '
            f'plantilla de nuevo para esa combinación exacta de {terminos().programa_l} y {terminos().periodo_l} '
            '(puede que la hayas descargado para otra combinación).',
            'danger'
        )
        return render_template('boletas_importar.html', planes=planes, max_cuatrimestres=_max_periodos(), periodo_escolar_sugerido=periodo_escolar_actual())

    exitosos = []
    errores = []

    for num_fila, fila in enumerate(ws.iter_rows(min_row=2), start=2):
        if all(c.value in (None, '') for c in fila):
            continue

        matricula = str(fila[0].value or '').strip()
        nombre_mostrado = str(fila[1].value or '').strip()

        alumno = db.session.get(Alumno, matricula) if matricula else None
        if not alumno:
            errores.append({'fila': num_fila, 'matricula': matricula, 'nombre': nombre_mostrado, 'errores': ['Matrícula no encontrada.']})
            continue
        if alumno.id_plan_fk != plan.id:
            errores.append({'fila': num_fila, 'matricula': matricula, 'nombre': alumno.nombre_completo, 'errores': [f'Esta matrícula no pertenece a {terminos().art} {terminos().programa_l} seleccionad{terminos().a_o}.']})
            continue

        fila_errores = []
        guardadas_de_esta_fila = 0

        for col_idx, materia in enumerate(materias, start=3):
            celda = fila[col_idx - 1] if col_idx - 1 < len(fila) else None
            valor_raw = celda.value if celda is not None else None
            if valor_raw in (None, ''):
                continue

            try:
                valor = parsear_calificacion(valor_raw)
            except MontoInvalido:
                fila_errores.append(f'"{materia.nombre}": no es una calificación válida (número de 0 a 10, ej. 8.5).')
                continue

            # Doble verificación del Escudo a nivel de objeto, por si acaso.
            if not alumno.materia_pertenece_a_su_plan(materia):
                fila_errores.append(f'"{materia.nombre}": no pertenece al plan del alumno (rechazado).')
                continue

            existente = Calificacion.query.filter_by(
                matricula_fk=alumno.matricula_id,
                id_materia_fk=materia.id,
                periodo_escolar=periodo_escolar
            ).first()

            if existente:
                if existente.calificacion_final != valor:
                    _registrar_historial_calificacion(alumno, materia, periodo_escolar, existente.calificacion_final, valor)
                existente.calificacion_final = valor
                existente.numero_acta = numero_acta
                existente.capturado_por = capturado_por
            else:
                _registrar_historial_calificacion(alumno, materia, periodo_escolar, None, valor)
                db.session.add(Calificacion(
                    matricula_fk=alumno.matricula_id,
                    id_materia_fk=materia.id,
                    calificacion_final=valor,
                    periodo_escolar=periodo_escolar,
                    numero_acta=numero_acta,
                    capturado_por=capturado_por,
                ))
            guardadas_de_esta_fila += 1

        # Un commit por ALUMNO: si una fila tiene un error en una materia,
        # las demás materias válidas de esa misma fila SÍ se guardan.
        db.session.commit()

        if guardadas_de_esta_fila > 0:
            exitosos.append({'fila': num_fila, 'matricula': matricula, 'nombre': alumno.nombre_completo, 'guardadas': guardadas_de_esta_fila})
        if fila_errores:
            errores.append({'fila': num_fila, 'matricula': matricula, 'nombre': alumno.nombre_completo, 'errores': fila_errores})

    flash(
        f'{sum(e["guardadas"] for e in exitosos)} calificación(es) guardada(s) en {len(exitosos)} alumno(s). '
        f'{len(errores)} fila(s) con algún error (ver detalle abajo).',
        'success' if exitosos else 'warning'
    )

    return render_template(
        'boletas_importar.html',
        planes=planes,
        max_cuatrimestres=_max_periodos(),
        exitosos=exitosos,
        errores=errores,
        periodo_escolar_sugerido=periodo_escolar_actual(),
    )


@academico_bp.route('/alumno/<matricula>/historial-calificaciones')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def historial_calificaciones(matricula):
    """
    Bitácora de auditoría: cada vez que se capturó o corrigió una
    calificación de este alumno, quién lo hizo y cuándo. Cubre tanto la
    captura individual como la carga masiva -- ambas registran aquí.
    """
    alumno = db.get_or_404(Alumno, matricula)
    historial = (
        HistorialCalificacion.query
        .filter_by(matricula_fk=matricula)
        .order_by(HistorialCalificacion.fecha.desc())
        .all()
    )
    return render_template('historial_calificaciones.html', alumno=alumno, historial=historial)


@academico_bp.route('/alumno/<matricula>/carga-academica')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def carga_academica(matricula):
    """
    Documento imprimible de la carga académica del alumno: las materias
    que se le asignaron en un periodo dado (por defecto, el periodo más
    reciente que tenga registrado). Se alimenta de InscripcionMateria,
    NO se recalcula contra el plan actual -- así sigue siendo correcto
    aunque el plan de estudios cambie después de que el alumno se inscribió.
    """
    alumno = db.get_or_404(Alumno, matricula)

    periodos_disponibles = sorted({
        i.periodo_escolar for i in
        InscripcionMateria.query.filter_by(matricula_fk=matricula).all()
    }, reverse=True)

    periodo_seleccionado = request.args.get('periodo', '').strip() or (periodos_disponibles[0] if periodos_disponibles else periodo_escolar_actual())

    inscripciones = (
        InscripcionMateria.query
        .filter_by(matricula_fk=matricula, periodo_escolar=periodo_seleccionado)
        .join(Materia)
        .order_by(Materia.nombre.asc())
        .all()
    )

    return render_template(
        'carga_academica.html',
        alumno=alumno,
        inscripciones=inscripciones,
        periodo_seleccionado=periodo_seleccionado,
        periodos_disponibles=periodos_disponibles,
    )
