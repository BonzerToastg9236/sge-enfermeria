"""
Pruebas de que cuatrimestre_actual queda acotado (hallazgo #5.4 de la
auditoría, plan de remediación paso 5.4). Dos capas:

1. CheckConstraint en BD (ck_alumno_cuatrimestre_positivo, mismo patrón
   que ck_materia_cuatrimestre_positivo en Materia.cuatrimestre): nunca
   permite un valor <= 0, venga de donde venga.
2. La importación masiva (rutas/alumnos.py::importar_alumnos) valida
   además el tope SUPERIOR (_max_periodos()) antes de crear el alumno --
   el CheckConstraint no puede expresar ese tope porque es configurable
   por institución (ConfiguracionInstitucion.max_periodos), no una
   constante fija en el esquema.
"""

import io
from datetime import date

from openpyxl import Workbook
from sqlalchemy.exc import IntegrityError

from tests.conftest import crear_plan, crear_usuario, login
from app import db, Alumno, EstatusAlumno, ConfiguracionInstitucion


# ---------------------------------------------------------------------------
# Capa 1: CheckConstraint en BD
# ---------------------------------------------------------------------------

def _alumno_con_cuatrimestre(plan, cuatrimestre_actual, curp='ABCD010101HDFXYZ01'):
    return Alumno(
        matricula_id=f'{plan.clave_carrera}{plan.anio_generacion}-TEST',
        nombre_completo='Alumno de Prueba',
        curp=curp,
        fecha_nacimiento=date(2005, 1, 1),
        fecha_certificado_prepa=date(2023, 7, 1),
        id_plan_fk=plan.id,
        estatus=EstatusAlumno.ACTIVO,
        cuatrimestre_actual=cuatrimestre_actual,
    )


def test_constraint_rechaza_cuatrimestre_cero_o_negativo(app):
    plan = crear_plan()

    for valor_invalido in (0, -1):
        db.session.add(_alumno_con_cuatrimestre(plan, valor_invalido))
        try:
            db.session.commit()
            assert False, f'La BD debió rechazar cuatrimestre_actual={valor_invalido}'
        except IntegrityError:
            db.session.rollback()

    assert Alumno.query.count() == 0


def test_constraint_permite_cuatrimestre_positivo(app):
    plan = crear_plan()

    db.session.add(_alumno_con_cuatrimestre(plan, 1))
    db.session.commit()

    assert Alumno.query.count() == 1


# ---------------------------------------------------------------------------
# Capa 2: tope superior en la importación masiva
# ---------------------------------------------------------------------------

COLUMNAS = [
    'nombre_completo', 'curp', 'fecha_nacimiento', 'fecha_certificado_prepa',
    'clave_carrera', 'sexo', 'estatus', 'cuatrimestre_actual',
]


def _xlsx_con_fila(clave_carrera, cuatrimestre_actual, curp='LORF050101MDFXYZ09'):
    wb = Workbook()
    ws = wb.active
    ws.append(COLUMNAS)
    ws.append([
        'María Fernanda López', curp, '2005-01-01', '2023-07-01',
        clave_carrera, 'Femenino', 'ACTIVO', cuatrimestre_actual,
    ])
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def _importar(client, clave_carrera, cuatrimestre_actual):
    archivo = _xlsx_con_fila(clave_carrera, cuatrimestre_actual)
    return client.post(
        '/alumnos/importar',
        data={'archivo_excel': (archivo, 'alumnos.xlsx')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )


def test_importacion_rechaza_cuatrimestre_mayor_al_maximo_de_la_institucion(client, app):
    plan = crear_plan(clave='LEN')
    tope = ConfiguracionInstitucion.obtener().max_periodos
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = _importar(client, 'LEN', tope + 1)

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 0
    assert f'entre 1 y {tope}'.encode() in respuesta.data


def test_importacion_rechaza_cuatrimestre_cero(client, app):
    plan = crear_plan(clave='LEN')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = _importar(client, 'LEN', 0)

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 0


def test_importacion_acepta_cuatrimestre_dentro_del_rango(client, app):
    plan = crear_plan(clave='LEN')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = _importar(client, 'LEN', 3)

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 1
    assert Alumno.query.first().cuatrimestre_actual == 3
