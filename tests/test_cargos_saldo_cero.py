"""
Pruebas del comportamiento de un cargo con saldo $0.00 (hallazgo
funcional #9).

QUÉ SE OBSERVÓ: un cargo con Monto/Recargo/Pagado/Saldo en $0.00 seguía
mostrando el botón "Registrar Pago".

DE DÓNDE SALÍAN LOS CARGOS DE $0: _generar_cargos_de_periodo() creaba el
cargo con `monto=concepto.monto_sugerido or Decimal('0.00')` -- es decir,
convertía "el concepto todavía no tiene precio capturado" (NULL) en "$0"
sin avisar. Eso ya NO ocurre: hoy, si falta el precio, no se genera el
cargo y se dice qué configuración falta (ver
tests/test_precios_por_institucion.py). Un cargo en $0 solo puede venir
de dos lados: los generados antes de esa corrección, o un precio que la
institución configuró en $0.00 a propósito (concepto gratuito).

QUÉ YA ESTABA BIEN (y estas pruebas lo fijan): el BACKEND nunca deja
registrar un pago sobre saldo 0 -- rechaza los montos <= 0 por inválidos
y cualquier monto positivo por exceder el saldo. Nunca se pudieron crear
pagos basura.

QUÉ ESTABA MAL: solo la vista. cobros.html mostraba el formulario de pago
con la condición `estatus not in (PAGADO, CANCELADO)`, más débil que la
regla real del backend. Encima el input quedaba con min="0.01" y
max="0.00": un botón imposible de usar.
"""

from decimal import Decimal

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, EstatusCargo, Pago


def _crear_cargo(alumno, monto='0.00', estatus=EstatusCargo.PENDIENTE):
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto='Inscripción',
        monto=Decimal(monto),
        estatus=estatus,
    )
    db.session.add(cargo)
    db.session.commit()
    return cargo


def _abrir_cobros(client, alumno):
    return client.get(f'/alumno/{alumno.matricula_id}/cobros')


# ---------------------------------------------------------------------------
# La vista: el formulario de pago solo cuando de verdad se puede pagar
# ---------------------------------------------------------------------------

def test_un_cargo_con_saldo_cero_no_ofrece_el_formulario_de_pago(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='0.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = _abrir_cobros(client, alumno)

    assert respuesta.status_code == 200
    assert f'/cobro/{cargo.id}/pagar'.encode('utf-8') not in respuesta.data, \
        'Se ofrece "Registrar Pago" en un cargo sin saldo por cobrar'


def test_un_cargo_con_saldo_pendiente_si_ofrece_el_formulario_de_pago(client, app):
    """Regresión inversa: el flujo normal de cobro NO debe verse afectado."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1500.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = _abrir_cobros(client, alumno)

    assert f'/cobro/{cargo.id}/pagar'.encode('utf-8') in respuesta.data


def test_un_cargo_pagado_parcialmente_sigue_ofreciendo_el_formulario(client, app):
    """Los pagos parciales son un caso válido y deben seguir funcionando."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '400.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    respuesta = _abrir_cobros(client, alumno)

    assert db.session.get(Cargo, cargo.id).estatus == EstatusCargo.PARCIAL
    assert f'/cobro/{cargo.id}/pagar'.encode('utf-8') in respuesta.data


def test_un_cargo_totalmente_pagado_ya_no_ofrece_el_formulario(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '1000.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    respuesta = _abrir_cobros(client, alumno)

    assert f'/cobro/{cargo.id}/pagar'.encode('utf-8') not in respuesta.data


# ---------------------------------------------------------------------------
# El backend: ya rechazaba estos pagos, y debe seguir haciéndolo aunque
# alguien mande el POST a mano (saltándose la interfaz)
# ---------------------------------------------------------------------------

def test_pagar_cero_sobre_un_cargo_de_saldo_cero_se_rechaza(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='0.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '0.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    assert respuesta.status_code == 200
    assert Pago.query.count() == 0
    assert 'no es v\xe1lido'.encode('utf-8') in respuesta.data


def test_pagar_un_monto_positivo_sobre_un_cargo_de_saldo_cero_se_rechaza(client, app):
    """Enviar el POST a mano tampoco debe crear un pago sin cargo que lo respalde."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='0.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    assert respuesta.status_code == 200
    assert Pago.query.count() == 0
    assert 'mayor al saldo pendiente'.encode('utf-8') in respuesta.data
    assert db.session.get(Cargo, cargo.id).saldo_pendiente() == Decimal('0.00')
