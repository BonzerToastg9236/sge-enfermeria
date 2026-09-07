"""
Pruebas para el índice único parcial contra cargos duplicados (migración
b7e2c9a41f3d) -- el respaldo en BD de _cargo_duplicado(), que hasta ahora
era solo un check en Python sin ninguna garantía real ante concurrencia.

Llave única: (matricula_fk, concepto_cobro_fk, COALESCE(periodo_escolar, ''))
entre cargos NO cancelados. Ver el docstring de la migración para el porqué
de cada pieza.

NOTA IMPORTANTE, a diferencia de tests/test_folios.py y
tests/test_concurrencia_pagos.py: esta protección SÍ se puede probar de
verdad contra SQLite, aunque sea en memoria y de un solo hilo. La razón es
que aquí la garantía es un ÍNDICE ÚNICO (algo que SQLite sí aplica de
verdad en cada INSERT, sin importar locking), no with_for_update() (que
SQLite ignora). No hace falta threading real para demostrar que el
segundo INSERT choca -- basta con saltarse el check de Python (como haría
una carrera real) e insertar dos veces.
"""
from unittest.mock import patch

from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, EstatusCargo, ConceptoCobro


def _crear_concepto(nombre='Colegiatura', activo=True):
    concepto = ConceptoCobro(nombre=nombre, activo=activo)
    db.session.add(concepto)
    db.session.commit()
    return concepto


# ---------------------------------------------------------------------------
# Nivel aplicación (HTTP): _cargo_duplicado() ya atrapa el caso normal,
# sin necesidad de tocar la BD.
# ---------------------------------------------------------------------------

def test_no_se_puede_crear_el_mismo_cargo_dos_veces_via_formulario(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto('Uniformes')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    datos = {'concepto_cobro_id': str(concepto.id), 'monto': '500.00', 'periodo_escolar': '2026-B'}
    client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data=datos, follow_redirects=True)
    respuesta_2 = client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data=datos, follow_redirects=True)

    assert respuesta_2.status_code == 200
    assert b'Ya existe un cargo' in respuesta_2.data
    cargos = Cargo.query.filter_by(matricula_fk=alumno.matricula_id).all()
    assert len(cargos) == 1  # el segundo NO se creó


def test_cargos_legitimos_con_concepto_distinto_se_permiten(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto_1 = _crear_concepto('Uniformes')
    concepto_2 = _crear_concepto('Material didáctico')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': str(concepto_1.id), 'monto': '500.00', 'periodo_escolar': '2026-B'
    }, follow_redirects=True)
    respuesta_2 = client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': str(concepto_2.id), 'monto': '300.00', 'periodo_escolar': '2026-B'
    }, follow_redirects=True)

    assert respuesta_2.status_code == 200
    assert b'agregado correctamente' in respuesta_2.data
    assert Cargo.query.filter_by(matricula_fk=alumno.matricula_id).count() == 2


def test_cargos_legitimos_con_periodo_distinto_se_permiten(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto('Colegiatura')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': str(concepto.id), 'monto': '1000.00', 'periodo_escolar': '2026-B-Ago'
    }, follow_redirects=True)
    respuesta_2 = client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data={
        'concepto_cobro_id': str(concepto.id), 'monto': '1000.00', 'periodo_escolar': '2026-B-Sep'
    }, follow_redirects=True)

    assert respuesta_2.status_code == 200
    assert b'agregado correctamente' in respuesta_2.data
    assert Cargo.query.filter_by(matricula_fk=alumno.matricula_id).count() == 2


# ---------------------------------------------------------------------------
# Nivel base de datos: el índice único es la garantía FINAL, independiente
# de que el check de Python se haya podido saltar (justo lo que pasaría en
# una carrera real). Se prueba insertando directamente con el ORM, sin
# pasar por _cargo_duplicado() -- simula "dos procesos que ya pasaron el
# check" sin necesitar threads reales.
# ---------------------------------------------------------------------------

def _insertar_cargo_directo(alumno, concepto, periodo_escolar, estatus=EstatusCargo.PENDIENTE):
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto_cobro_fk=concepto.id,
        concepto=concepto.nombre,
        monto=Decimal('1000.00'),
        periodo_escolar=periodo_escolar,
        estatus=estatus,
    )
    db.session.add(cargo)
    db.session.commit()
    return cargo


def test_indice_unico_rechaza_duplicado_exacto_a_nivel_de_bd(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto()

    _insertar_cargo_directo(alumno, concepto, '2026-B')

    cargo_2 = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto_cobro_fk=concepto.id,
        concepto=concepto.nombre,
        monto=Decimal('1000.00'),
        periodo_escolar='2026-B',
        estatus=EstatusCargo.PENDIENTE,
    )
    db.session.add(cargo_2)
    try:
        db.session.commit()
        assert False, 'La BD debió rechazar el segundo INSERT con la misma llave'
    except IntegrityError:
        db.session.rollback()

    assert Cargo.query.filter_by(matricula_fk=alumno.matricula_id).count() == 1


def test_indice_unico_permite_dos_cargos_cancelados_con_la_misma_llave(app):
    """
    _cargo_duplicado() ignora a propósito los cargos CANCELADO (cancelar y
    volver a cobrar el mismo concepto/periodo es un flujo legítimo, ver
    cancelar_cargo()). El índice único debe respetar la misma exclusión.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto()

    _insertar_cargo_directo(alumno, concepto, '2026-B', estatus=EstatusCargo.CANCELADO)
    _insertar_cargo_directo(alumno, concepto, '2026-B', estatus=EstatusCargo.CANCELADO)  # no debe chocar

    assert Cargo.query.filter_by(matricula_fk=alumno.matricula_id).count() == 2


def test_indice_unico_trata_periodo_escolar_nulo_como_comparable(app):
    """
    nuevo_cargo() permite periodo_escolar=None (cargos de un solo pago,
    ej. "Uniformes"). _cargo_duplicado() SÍ trata dos NULL como la misma
    llave (Cargo.periodo_escolar == None genera "IS NULL" en SQL, que
    empareja con cualquier otro NULL) -- el índice usa
    COALESCE(periodo_escolar, '') exactamente para replicar eso, en vez
    del comportamiento estándar de SQL donde NULL nunca es igual a NULL.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto()

    _insertar_cargo_directo(alumno, concepto, periodo_escolar=None)

    cargo_2 = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto_cobro_fk=concepto.id,
        concepto=concepto.nombre,
        monto=Decimal('500.00'),
        periodo_escolar=None,
        estatus=EstatusCargo.PENDIENTE,
    )
    db.session.add(cargo_2)
    try:
        db.session.commit()
        assert False, 'La BD debió rechazar el segundo cargo con periodo_escolar NULL repetido'
    except IntegrityError:
        db.session.rollback()

    assert Cargo.query.filter_by(matricula_fk=alumno.matricula_id).count() == 1


# ---------------------------------------------------------------------------
# El manejo del IntegrityError en la ruta: simula que _cargo_duplicado()
# corrió sobre datos ya obsoletos (justo lo que pasaría si otra petición
# insertó el cargo una fracción de segundo antes, entre el check y el
# INSERT) forzándolo a devolver "no hay duplicado" con mock, mientras el
# duplicado YA existe de verdad en la BD. Así se prueba el comportamiento
# REAL de la ruta ante el IntegrityError -- no se simula la respuesta,
# se simula la condición de carrera que la dispara.
# ---------------------------------------------------------------------------

def test_ruta_nuevo_cargo_maneja_bien_el_integrityerror_de_una_carrera(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto()
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    _insertar_cargo_directo(alumno, concepto, '2026-B')  # "el otro proceso" ya ganó la carrera

    with patch('app._cargo_duplicado', return_value=None):  # simula el check leyendo estado obsoleto
        respuesta = client.post(f'/alumno/{alumno.matricula_id}/cobros/nuevo', data={
            'concepto_cobro_id': str(concepto.id), 'monto': '1000.00', 'periodo_escolar': '2026-B'
        }, follow_redirects=True)

    assert respuesta.status_code == 200  # nunca un 500 -- el IntegrityError se atrapó
    assert b'se gener\xc3\xb3 justo ahora' in respuesta.data
    assert Cargo.query.filter_by(matricula_fk=alumno.matricula_id).count() == 1  # sigue habiendo solo uno
