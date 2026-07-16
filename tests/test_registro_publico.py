"""
Pruebas del Módulo de Auto-registro Público (/registro).
"""

from datetime import datetime

from tests.conftest import crear_plan

# generar_matricula() usa el AÑO REAL del sistema, no un valor fijo —
# usamos lo mismo aquí para que la prueba no dependa de una fecha fija.
ANIO_ACTUAL = datetime.utcnow().year

DATOS_VALIDOS_BASE = {
    'nombre_completo': 'María Fernanda López Ramírez',
    'curp': 'LORF050101MDFXYZ09',
    'fecha_nacimiento': '2005-01-01',
    'fecha_certificado_prepa': '2023-07-15',
    'sexo': 'Femenino',
    'domicilio_calle_numero': 'Av. Juárez #123',
    'domicilio_ciudad': 'Cuautitlán Izcalli',
    'domicilio_cp': '54800',
    'domicilio_estado': 'Estado de México',
    'contacto_emergencia_nombre': 'Juana Ramírez',
    'contacto_emergencia_telefono': '5512345678',
    'modalidad': 'ESCOLARIZADO',
    'turno': 'MATUTINO',
}


def _datos_con_plan(plan_id, **overrides):
    datos = dict(DATOS_VALIDOS_BASE)
    datos['id_plan_fk'] = str(plan_id)
    datos.update(overrides)
    return datos


def test_registro_exitoso_crea_alumno_pendiente(client, app):
    plan = crear_plan()

    respuesta = client.post('/registro', data=_datos_con_plan(plan.id), follow_redirects=True)

    assert respuesta.status_code == 200

    from app import Alumno, EstatusAlumno
    alumno = Alumno.query.filter_by(curp='LORF050101MDFXYZ09').first()
    assert alumno is not None
    assert alumno.estatus == EstatusAlumno.PENDIENTE
    assert alumno.id_plan_fk == plan.id


def test_registro_genera_matricula_con_prefijo_de_la_carrera(client, app):
    plan = crear_plan(clave='LEN', anio=2026)

    client.post('/registro', data=_datos_con_plan(plan.id), follow_redirects=True)

    from app import Alumno
    alumno = Alumno.query.filter_by(curp='LORF050101MDFXYZ09').first()
    assert alumno.matricula_id.startswith(f'LEN{ANIO_ACTUAL}-')


def test_registro_rechaza_curp_con_longitud_invalida(client, app):
    plan = crear_plan()

    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, curp='CORTA123'),
        follow_redirects=True
    )

    assert respuesta.status_code == 400

    from app import Alumno
    assert Alumno.query.count() == 0


def test_registro_rechaza_fecha_de_nacimiento_invalida(client, app):
    plan = crear_plan()

    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, fecha_nacimiento='no-es-una-fecha'),
        follow_redirects=True
    )

    assert respuesta.status_code == 400

    from app import Alumno
    assert Alumno.query.count() == 0


def test_registro_rechaza_curp_duplicada(client, app):
    plan = crear_plan()

    # Primer registro: debe funcionar
    client.post('/registro', data=_datos_con_plan(plan.id), follow_redirects=True)

    # Segundo registro con LA MISMA CURP: debe rechazarse
    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, nombre_completo='Otra Persona Distinta'),
        follow_redirects=True
    )

    assert respuesta.status_code == 400

    from app import Alumno
    assert Alumno.query.filter_by(curp='LORF050101MDFXYZ09').count() == 1


def test_registro_rechaza_plan_inexistente(client, app):
    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(99999),
        follow_redirects=True
    )

    assert respuesta.status_code == 400

    from app import Alumno
    assert Alumno.query.count() == 0
