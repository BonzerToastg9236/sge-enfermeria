"""
Pruebas funcionales de la importación masiva de alumnos (hallazgo #6 de
la auditoría: esta ruta -- la que más parsea archivos del sistema --
solo estaba cubierta por el test de inventario de rutas, no por su
lógica).

NOTA: el caso "cuatrimestre_actual fuera de rango se rechaza" ya está
cubierto en tests/test_cuatrimestre_actual_acotado.py (paso 5.4 del
plan de remediación) -- no se duplica aquí.
"""

import io

from openpyxl import Workbook

from tests.conftest import crear_plan, crear_usuario, login

from app import Alumno


COLUMNAS = [
    'nombre_completo', 'curp', 'fecha_nacimiento', 'fecha_certificado_prepa',
    'clave_carrera', 'sexo', 'estatus', 'cuatrimestre_actual',
]


def _xlsx(filas, columnas=COLUMNAS):
    wb = Workbook()
    ws = wb.active
    ws.append(columnas)
    for fila in filas:
        ws.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def _fila(nombre, curp, clave_carrera='LEN', cuatrimestre_actual=1):
    return [nombre, curp, '2005-01-01', '2023-07-01', clave_carrera, 'Femenino', 'ACTIVO', cuatrimestre_actual]


def _importar(client, filas, columnas=COLUMNAS):
    archivo = _xlsx(filas, columnas)
    return client.post(
        '/alumnos/importar',
        data={'archivo_excel': (archivo, 'alumnos.xlsx')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )


def _preparar(client):
    crear_plan(clave='LEN')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')


def test_fila_valida_se_importa_correctamente(client, app):
    _preparar(client)

    respuesta = _importar(client, [_fila('María Fernanda López', 'LORF050101MDFXYZ09')])

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 1
    assert b'Se importaron 1 alumno' in respuesta.data


def test_curp_duplicada_se_reporta_sin_abortar_el_resto_del_archivo(client, app):
    _preparar(client)
    # Ya existe un alumno con esta CURP ANTES de importar (simula el caso real).
    _importar(client, [_fila('Alumno Existente', 'LORF050101MDFXYZ09')])
    assert Alumno.query.count() == 1

    respuesta = _importar(client, [
        _fila('Alumno Nuevo Valido', 'GAHJ990101HDFXYZ02'),
        _fila('Alumno Repetido', 'LORF050101MDFXYZ09'),  # misma CURP que el ya existente
    ])

    assert respuesta.status_code == 200
    # La fila válida SÍ se creó aunque la otra fila del mismo archivo falló.
    assert Alumno.query.count() == 2
    assert Alumno.query.filter_by(nombre_completo='Alumno Nuevo Valido').first() is not None
    assert b'ya existe un alumno con la CURP' in respuesta.data
    assert b'1 fila(s) no se pudieron importar' in respuesta.data


def test_archivo_vacio_da_error_claro_no_500(client, app):
    _preparar(client)

    respuesta = _importar(client, [])  # sin ninguna fila de datos, solo encabezados

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 0


def test_archivo_con_encabezados_alterados_da_error_claro_no_500(client, app):
    _preparar(client)

    respuesta = _importar(
        client,
        filas=[['María Fernanda López', 'LORF050101MDFXYZ09', '2005-01-01', '2023-07-01', 'LEN', 'Femenino']],
        columnas=['Nombre', 'CURP', 'Fecha Nacimiento', 'Fecha Certificado', 'Carrera', 'Sexo'],
    )

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 0
    assert b'faltan columnas obligatorias' in respuesta.data


def test_archivo_no_xlsx_se_rechaza_sin_500(client, app):
    _preparar(client)

    respuesta = client.post(
        '/alumnos/importar',
        data={'archivo_excel': (io.BytesIO(b'esto no es un excel'), 'alumnos.txt')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 0


def test_archivo_xlsx_corrupto_no_tumba_la_vista(client, app):
    _preparar(client)

    respuesta = client.post(
        '/alumnos/importar',
        data={'archivo_excel': (io.BytesIO(b'esto no es un xlsx valido aunque tenga la extension'), 'alumnos.xlsx')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 0
    assert b'No se pudo leer el archivo' in respuesta.data
