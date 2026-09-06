"""
Pruebas del "Escudo" del Plan de Estudios — la regla de negocio #1 del
proyecto: un alumno NUNCA debe poder tener una calificación de una materia
que no pertenece a su propio plan de estudios, ni siquiera manipulando el
formulario a mano.
"""

from tests.conftest import crear_plan, crear_materia, crear_alumno, crear_usuario, login


def test_materia_pertenece_a_su_plan_devuelve_true_para_su_propio_plan(app):
    plan = crear_plan()
    materia = crear_materia(plan)
    alumno = crear_alumno(plan)

    assert alumno.materia_pertenece_a_su_plan(materia) is True


def test_materia_pertenece_a_su_plan_devuelve_false_para_plan_ajeno(app):
    plan_del_alumno = crear_plan(nombre='Plan A', clave='PLA')
    plan_ajeno = crear_plan(nombre='Plan B', clave='PLB')

    materia_ajena = crear_materia(plan_ajeno, nombre='Materia de Otro Plan')
    alumno = crear_alumno(plan_del_alumno)

    assert alumno.materia_pertenece_a_su_plan(materia_ajena) is False


def test_boleta_solo_muestra_materias_del_plan_del_alumno(client, app):
    """
    Prueba de integración: la pantalla de captura de boleta (GET) debe
    listar ÚNICAMENTE las materias del plan del alumno consultado, aunque
    existan materias con el mismo número de cuatrimestre en otros planes.
    """
    plan_a = crear_plan(nombre='Licenciatura en Enfermería (Prueba)', clave='LEN')
    plan_b = crear_plan(nombre='Ingeniería en Sistemas (Prueba)', clave='ISC')

    crear_materia(plan_a, nombre='Fundamentos de Enfermería', cuatrimestre=1)
    crear_materia(plan_b, nombre='Fundamentos de Programación', cuatrimestre=1)

    alumno = crear_alumno(plan_a, curp='AAAA010101HDFXYZ01')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.get(f'/alumno/{alumno.matricula_id}/boleta?cuatrimestre=1')

    assert respuesta.status_code == 200
    assert b'Fundamentos de Enfermeria' in respuesta.data or 'Fundamentos de Enfermería'.encode('utf-8') in respuesta.data
    assert b'Fundamentos de Programacion' not in respuesta.data
    assert 'Fundamentos de Programación'.encode('utf-8') not in respuesta.data


def test_captura_de_boleta_rechaza_materia_de_otro_plan_aunque_se_manipule_el_formulario(client, app):
    """
    Aunque alguien edite el HTML a mano y mande el ID de una materia que NO
    pertenece al plan del alumno, el servidor debe rechazarla — el "Escudo"
    se revalida siempre del lado del backend, nunca se confía solo en que
    el formulario mostró las opciones correctas.
    """
    plan_a = crear_plan(nombre='Plan del Alumno', clave='PLA')
    plan_b = crear_plan(nombre='Plan Ajeno', clave='PLB')

    materia_ajena = crear_materia(plan_b, nombre='Materia Prohibida', cuatrimestre=1)
    alumno = crear_alumno(plan_a, curp='BBBB010101HDFXYZ02')

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        f'/alumno/{alumno.matricula_id}/boleta?cuatrimestre=1',
        data={
            'periodo_escolar': '2026-A',
            f'calificacion_{materia_ajena.id}': '10',
        },
        follow_redirects=True
    )

    assert respuesta.status_code == 200

    from app import Calificacion
    calificaciones_guardadas = Calificacion.query.filter_by(matricula_fk=alumno.matricula_id).all()
    assert len(calificaciones_guardadas) == 0


def _construir_excel_boletas(encabezados, filas):
    """Arma un .xlsx en memoria (como si el usuario lo hubiera subido)."""
    import io
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Calificaciones'
    ws.append(encabezados)
    for fila in filas:
        ws.append(fila)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def test_carga_masiva_de_boletas_rechaza_alumno_de_otro_plan(client, app):
    """
    Escudo aplicado también en la carga masiva: si en el archivo aparece
    una matrícula que NO pertenece a la carrera seleccionada, esa fila se
    rechaza -- nunca se le guarda una calificación de una carrera ajena.
    """
    plan_a = crear_plan(nombre='Carrera Seleccionada', clave='SEL')
    plan_b = crear_plan(nombre='Otra Carrera', clave='OTR')

    materia = crear_materia(plan_a, nombre='Materia Válida', cuatrimestre=1)
    alumno_ajeno = crear_alumno(plan_b, curp='CCCC010101HDFXYZ03')

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    archivo = _construir_excel_boletas(
        encabezados=['Matrícula', 'Nombre Completo', materia.nombre],
        filas=[[alumno_ajeno.matricula_id, alumno_ajeno.nombre_completo, 9.0]],
    )

    respuesta = client.post(
        '/boletas/importar',
        data={
            'plan_id': str(plan_a.id),
            'cuatrimestre': '1',
            'periodo_escolar': '2026-A',
            'archivo_excel': (archivo, 'boletas.xlsx'),
        },
        content_type='multipart/form-data',
        follow_redirects=True
    )

    assert respuesta.status_code == 200

    from app import Calificacion
    assert Calificacion.query.filter_by(matricula_fk=alumno_ajeno.matricula_id).count() == 0


def test_carga_masiva_de_boletas_guarda_calificaciones_validas(client, app):
    plan = crear_plan(nombre='Carrera de Prueba', clave='CDP')
    materia = crear_materia(plan, nombre='Materia de Prueba', cuatrimestre=1)
    alumno = crear_alumno(plan, curp='DDDD010101HDFXYZ04')  # ya queda ACTIVO por defecto

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    archivo = _construir_excel_boletas(
        encabezados=['Matrícula', 'Nombre Completo', materia.nombre],
        filas=[[alumno.matricula_id, alumno.nombre_completo, 8.5]],
    )

    client.post(
        '/boletas/importar',
        data={
            'plan_id': str(plan.id),
            'cuatrimestre': '1',
            'periodo_escolar': '2026-A',
            'archivo_excel': (archivo, 'boletas.xlsx'),
        },
        content_type='multipart/form-data',
        follow_redirects=True
    )

    from app import Calificacion
    calificacion = Calificacion.query.filter_by(matricula_fk=alumno.matricula_id, id_materia_fk=materia.id).first()
    assert calificacion is not None
    assert float(calificacion.calificacion_final) == 8.5


def test_capturar_calificacion_registra_historial(client, app):
    """Toda captura nueva de calificación debe dejar un registro en el historial."""
    plan = crear_plan()
    materia = crear_materia(plan, nombre='Materia con Historial', cuatrimestre=1)
    alumno = crear_alumno(plan, curp='EEEE010101HDFXYZ05')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/alumno/{alumno.matricula_id}/boleta?cuatrimestre=1',
        data={
            'periodo_escolar': '2026-A',
            f'calificacion_{materia.id}': '7',
        },
        follow_redirects=True
    )

    from app import HistorialCalificacion
    historial = HistorialCalificacion.query.filter_by(matricula_fk=alumno.matricula_id).all()
    assert len(historial) == 1
    assert historial[0].calificacion_anterior is None
    assert historial[0].calificacion_nueva == 7.0


def test_corregir_calificacion_registra_historial_con_valor_anterior(client, app):
    """Al corregir una calificación ya capturada, el historial debe guardar el valor viejo y el nuevo."""
    plan = crear_plan()
    materia = crear_materia(plan, nombre='Materia Corregida', cuatrimestre=1)
    alumno = crear_alumno(plan, curp='FFFF010101HDFXYZ06')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/alumno/{alumno.matricula_id}/boleta?cuatrimestre=1',
        data={'periodo_escolar': '2026-A', f'calificacion_{materia.id}': '6'},
        follow_redirects=True
    )
    client.post(
        f'/alumno/{alumno.matricula_id}/boleta?cuatrimestre=1',
        data={'periodo_escolar': '2026-A', f'calificacion_{materia.id}': '9'},
        follow_redirects=True
    )

    from app import HistorialCalificacion
    historial = (
        HistorialCalificacion.query
        .filter_by(matricula_fk=alumno.matricula_id)
        .order_by(HistorialCalificacion.fecha.asc())
        .all()
    )
    assert len(historial) == 2
    assert historial[0].calificacion_anterior is None
    assert historial[0].calificacion_nueva == 6.0
    assert historial[1].calificacion_anterior == 6.0
    assert historial[1].calificacion_nueva == 9.0


def test_agregar_materia_a_un_plan(client, app):
    plan = crear_plan(nombre='Carrera para Materias', clave='CPM')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/planes/{plan.id}/materias',
        data={'nombre': 'Física Aplicada', 'clave': 'FIS-101', 'cuatrimestre': '3', 'creditos': '6'},
        follow_redirects=True
    )

    from app import Materia
    materia = Materia.query.filter_by(id_plan_fk=plan.id, nombre='Física Aplicada').first()
    assert materia is not None
    assert materia.cuatrimestre == 3
    assert materia.creditos == 6.0


def test_no_se_puede_agregar_materia_duplicada_en_mismo_cuatrimestre(client, app):
    plan = crear_plan(nombre='Carrera Sin Duplicados', clave='CSD')
    crear_materia(plan, nombre='Ética Profesional', cuatrimestre=2)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/planes/{plan.id}/materias',
        data={'nombre': 'Ética Profesional', 'cuatrimestre': '2'},
        follow_redirects=True
    )

    from app import Materia
    coincidencias = Materia.query.filter_by(id_plan_fk=plan.id, nombre='Ética Profesional', cuatrimestre=2).count()
    assert coincidencias == 1  # sigue habiendo solo una, no se duplicó


def test_no_se_puede_eliminar_materia_con_calificaciones(client, app):
    plan = crear_plan(nombre='Carrera con Historial', clave='CCH')
    materia = crear_materia(plan, nombre='Materia con Nota', cuatrimestre=1)
    alumno = crear_alumno(plan, curp='JJJJ010101HDFXYZ10')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/alumno/{alumno.matricula_id}/boleta?cuatrimestre=1',
        data={'periodo_escolar': '2026-A', f'calificacion_{materia.id}': '8'},
        follow_redirects=True
    )

    client.post(f'/materias/{materia.id}/eliminar', follow_redirects=True)

    from app import Materia, db
    assert db.session.get(Materia, materia.id) is not None  # sigue existiendo, no se dejó borrar
