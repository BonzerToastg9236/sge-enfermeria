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
