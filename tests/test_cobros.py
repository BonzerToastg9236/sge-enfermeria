"""
Pruebas del Sistema de Cobros: cálculo de saldo (siempre derivado de los
pagos reales, nunca guardado a mano), y permisos por rol — Administrativo
puede consultar y registrar pagos, pero SOLO Directivo puede crear o
cancelar cargos.
"""

from decimal import Decimal

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, EstatusCargo, RolUsuario, ConceptoCobro


def _crear_cargo(alumno, concepto='Colegiatura de Prueba', monto='1500.00'):
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto=concepto,
        monto=Decimal(monto),
        estatus=EstatusCargo.PENDIENTE,
    )
    db.session.add(cargo)
    db.session.commit()
    return cargo


def _crear_concepto(nombre='Colegiatura', activo=True):
    """
    Crea un ConceptoCobro del catálogo. La ruta /alumno/<matricula>/cobros/nuevo
    ya no acepta texto libre en 'concepto' -- exige un 'concepto_cobro_id' real
    que exista (y esté activo) en el catálogo.
    """
    concepto = ConceptoCobro(nombre=nombre, activo=activo)
    db.session.add(concepto)
    db.session.commit()
    return concepto


def test_cargo_recien_creado_tiene_saldo_igual_al_monto(app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno)

    assert cargo.saldo_pendiente() == Decimal('1500.00')
    assert cargo.total_pagado() == Decimal('0.00')
    assert cargo.estatus == EstatusCargo.PENDIENTE


def test_pago_parcial_deja_estatus_parcial_y_saldo_correcto(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('500.00')
    assert cargo_actualizado.saldo_pendiente() == Decimal('1000.00')
    assert cargo_actualizado.estatus == EstatusCargo.PARCIAL


def test_pago_completo_deja_estatus_pagado(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '1000.00', 'metodo_pago': 'TRANSFERENCIA'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.saldo_pendiente() == Decimal('0.00')
    assert cargo_actualizado.estatus == EstatusCargo.PAGADO


def test_no_se_puede_pagar_mas_del_saldo_pendiente(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '9999.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('0.00')  # el pago NO se registró
    assert cargo_actualizado.estatus == EstatusCargo.PENDIENTE


def test_administrativo_puede_registrar_pagos(client, app):
    """Regla de negocio: Administrativo SÍ puede actualizar (registrar pagos)."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='500.00')
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.estatus == EstatusCargo.PAGADO


def test_administrativo_no_puede_crear_cargos(client, app):
    """Regla de negocio: SOLO Directivo define la estructura de cobros."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto(nombre='Colegiatura no autorizada')
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    respuesta = client.post(
        f'/alumno/{alumno.matricula_id}/cobros/nuevo',
        data={'concepto_cobro_id': str(concepto.id), 'monto': '1000.00'},
        follow_redirects=True
    )

    assert respuesta.status_code == 200
    assert 'permisos'.encode('utf-8') in respuesta.data.lower()
    assert Cargo.query.filter_by(matricula_fk=alumno.matricula_id).count() == 0


def test_administrativo_no_puede_cancelar_cargos(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno)
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    respuesta = client.post(f'/cobro/{cargo.id}/cancelar', follow_redirects=True)

    assert respuesta.status_code == 200
    assert 'permisos'.encode('utf-8') in respuesta.data.lower()

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.estatus != EstatusCargo.CANCELADO


def test_directivo_si_puede_crear_y_cancelar_cargos(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    concepto = _crear_concepto(nombre='Inscripción')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/alumno/{alumno.matricula_id}/cobros/nuevo',
        data={'concepto_cobro_id': str(concepto.id), 'monto': '2000.00'},
        follow_redirects=True
    )

    cargo = Cargo.query.filter_by(matricula_fk=alumno.matricula_id).first()
    assert cargo is not None
    assert cargo.monto == Decimal('2000.00')

    client.post(f'/cobro/{cargo.id}/cancelar', data={'comentario': 'Duplicado'}, follow_redirects=True)

    cargo_actualizado = Cargo.query.get(cargo.id)
    assert cargo_actualizado.estatus == EstatusCargo.CANCELADO
