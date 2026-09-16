"""
Pruebas funcionales de avanzar_cuatrimestre_lote() (hallazgo #6 de la
auditoría, plan de remediación paso 6): avanza de golpe a TODOS los
alumnos Activos de una carrera+cuatrimestre. Dos comportamientos
documentados en el código pero sin prueba hasta ahora:

1. Es una sola transacción -- si el cargo de UN alumno choca con el
   índice único (migración b7e2c9a41f3d), el LOTE COMPLETO se descarta,
   nunca a medias (a diferencia de importar_boletas(), que sí comete
   por alumno).
2. Si a varios alumnos les falta la misma configuración de precio, el
   aviso se junta sin repetir -- un solo mensaje, no uno por alumno.
"""

from decimal import Decimal
from unittest.mock import patch

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Alumno, Cargo, EstatusCargo, EstatusAlumno, ConceptoCobro, periodo_escolar_actual


def _avanzar_lote(client, plan, cuatrimestre_actual=1):
    return client.post(
        '/alumnos/avanzar-cuatrimestre-lote',
        data={'plan_id': str(plan.id), 'cuatrimestre_actual': str(cuatrimestre_actual)},
        follow_redirects=True,
    )


def test_lote_es_todo_o_nada_ante_choque_de_indice_a_mitad_de_la_lista(client, app):
    plan = crear_plan(clave='LOT')
    alumno_1 = crear_alumno(plan, curp='AAAA010101HDFXYZ01', matricula_id='LOT2026-00001', estatus=EstatusAlumno.ACTIVO)
    alumno_2 = crear_alumno(plan, curp='BBBB010101HDFXYZ02', matricula_id='LOT2026-00002', estatus=EstatusAlumno.ACTIVO)
    concepto = ConceptoCobro(nombre='Reinscripción', activo=True, monto_sugerido=Decimal('500.00'))
    db.session.add(concepto)
    db.session.commit()

    # "El otro proceso" ya generó el cargo de reinscripción de alumno_2
    # para el periodo actual -- justo lo que el lote intentaría crear de
    # nuevo si su check en Python leyera estado obsoleto.
    db.session.add(Cargo(
        matricula_fk=alumno_2.matricula_id,
        concepto_cobro_fk=concepto.id,
        concepto=concepto.nombre,
        monto=Decimal('500.00'),
        periodo_escolar=periodo_escolar_actual(),
        estatus=EstatusCargo.PENDIENTE,
    ))
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    with patch('servicios.cobros._cargo_duplicado', return_value=None):  # simula el check leyendo estado obsoleto
        respuesta = _avanzar_lote(client, plan)

    assert respuesta.status_code == 200  # nunca un 500
    assert b'No se aplic' in respuesta.data  # "No se aplic\xc3\xb3 el avance..."

    db.session.refresh(alumno_1)
    db.session.refresh(alumno_2)
    assert alumno_1.cuatrimestre_actual == 1, 'El lote debió descartarse completo: ni siquiera alumno_1 debió avanzar'
    assert alumno_2.cuatrimestre_actual == 1
    assert Cargo.query.filter_by(matricula_fk=alumno_1.matricula_id).count() == 0, (
        'El cargo de alumno_1 (que no chocaba con nada) tampoco debió quedar guardado: todo o nada'
    )
    assert Cargo.query.filter_by(matricula_fk=alumno_2.matricula_id).count() == 1  # el que ya existía, sin duplicar


def test_aviso_de_precio_faltante_sale_una_sola_vez_no_por_alumno(client, app):
    plan = crear_plan(clave='AVI')
    crear_alumno(plan, curp='CCCC010101HDFXYZ03', matricula_id='AVI2026-00001', estatus=EstatusAlumno.ACTIVO)
    crear_alumno(plan, curp='DDDD010101HDFXYZ04', matricula_id='AVI2026-00002', estatus=EstatusAlumno.ACTIVO)
    crear_alumno(plan, curp='EEEE010101HDFXYZ05', matricula_id='AVI2026-00003', estatus=EstatusAlumno.ACTIVO)
    # Reinscripción SIN precio configurado: los 3 alumnos disparan el MISMO aviso.
    db.session.add(ConceptoCobro(nombre='Reinscripción', activo=True, monto_sugerido=None))
    db.session.commit()

    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = _avanzar_lote(client, plan)

    assert respuesta.status_code == 200
    assert Alumno.query.filter_by(id_plan_fk=plan.id, cuatrimestre_actual=2).count() == 3  # sí avanzaron, solo faltó el cargo
    assert respuesta.data.count(b'no tiene precio configurado') == 1
