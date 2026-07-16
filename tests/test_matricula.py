"""
Pruebas de generar_matricula(): la regla de "Letras de carrera + Año +
consecutivo" definida desde el inicio del proyecto.
"""

from datetime import date, datetime

from tests.conftest import crear_plan
from app import generar_matricula, Alumno, EstatusAlumno, db

# generar_matricula() usa el AÑO REAL del sistema al momento de generarse
# (no el año de generación del plan) — usamos lo mismo aquí para que estas
# pruebas no dependan de una fecha fija ni se rompan solas con el tiempo.
ANIO_ACTUAL = datetime.utcnow().year


def _guardar_alumno_con_matricula(plan, matricula, curp):
    alumno = Alumno(
        matricula_id=matricula,
        nombre_completo='Alumno de Prueba',
        curp=curp,
        fecha_nacimiento=date(2005, 1, 1),
        fecha_certificado_prepa=date(2023, 7, 1),
        id_plan_fk=plan.id,
        estatus=EstatusAlumno.ACTIVO,
    )
    db.session.add(alumno)
    db.session.commit()
    return alumno


def test_primera_matricula_del_plan_termina_en_00001(app):
    plan = crear_plan(clave='LEN', anio=2026)

    matricula = generar_matricula(plan)

    assert matricula == f'LEN{ANIO_ACTUAL}-00001'


def test_segunda_matricula_incrementa_el_consecutivo(app):
    plan = crear_plan(clave='LEN', anio=2026)

    primera = generar_matricula(plan)
    _guardar_alumno_con_matricula(plan, primera, curp='AAAA010101HDFXYZ01')

    segunda = generar_matricula(plan)

    assert segunda == f'LEN{ANIO_ACTUAL}-00002'


def test_planes_distintos_tienen_contadores_independientes(app):
    plan_a = crear_plan(nombre='Plan A', clave='LEN', anio=2026)
    plan_b = crear_plan(nombre='Plan B', clave='ISC', anio=2026)

    matricula_a1 = generar_matricula(plan_a)
    _guardar_alumno_con_matricula(plan_a, matricula_a1, curp='AAAA010101HDFXYZ01')

    matricula_b1 = generar_matricula(plan_b)

    # El plan B no debe verse afectado por lo que ya existe en el plan A
    assert matricula_b1 == f'ISC{ANIO_ACTUAL}-00001'


def test_crear_alumno_reintenta_si_el_commit_falla_por_colision(app, monkeypatch):
    """
    Simula la condición de carrera del hallazgo A: dos registros generando
    la misma matrícula al mismo tiempo. Forzamos que el PRIMER intento de
    guardar falle con IntegrityError (como pasaría en una colisión real),
    y verificamos que crear_alumno_generando_matricula() se recupera sola
    en el segundo intento, sin que el usuario vea ningún error.
    """
    from app import crear_alumno_generando_matricula
    from sqlalchemy.exc import IntegrityError as SA_IntegrityError

    plan = crear_plan(clave='LEN', anio=2026)

    commit_original = db.session.commit
    contador_intentos = {'veces': 0}

    def commit_falla_la_primera_vez():
        contador_intentos['veces'] += 1
        if contador_intentos['veces'] == 1:
            db.session.rollback()
            raise SA_IntegrityError('INSERT simulado', {}, Exception('matrícula duplicada simulada'))
        return commit_original()

    monkeypatch.setattr(db.session, 'commit', commit_falla_la_primera_vez)

    alumno, error = crear_alumno_generando_matricula(
        plan,
        nombre_completo='Alumno de Prueba de Reintento',
        curp='WWWW010101HDFXYZ07',
        fecha_nacimiento=date(2005, 1, 1),
        fecha_certificado_prepa=date(2023, 7, 1),
        estatus=EstatusAlumno.ACTIVO,
    )

    assert error is None
    assert alumno is not None
    assert contador_intentos['veces'] == 2  # falló una vez, tuvo éxito en el reintento
