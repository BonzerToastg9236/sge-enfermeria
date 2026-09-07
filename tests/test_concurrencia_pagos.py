"""
Pruebas para el fix de la condición de carrera en registrar_pago() --
antes, el saldo se leía y validaba en Python sin bloquear la fila del
Cargo, así que dos peticiones simultáneas sobre el mismo cargo podían
leer el mismo saldo "viejo" y ambas pasar la validación, dejando el
cargo pagado de más.

NOTA SOBRE EL LÍMITE DE ESTAS PRUEBAS (mismo caso que tests/test_folios.py
para generar_matricula()/siguiente_folio(), el mismo patrón de fix):
corren contra SQLite en memoria (ver tests/conftest.py), donde
with_for_update() se ignora A PROPÓSITO -- SQLite no soporta bloqueo real
por fila, solo PostgreSQL (producción) lo hace. Por diseño del propio
proyecto (ver el comentario en registrar_pago() y en generar_matricula()),
ninguna prueba contra SQLite puede demostrar que el bloqueo funciona de
verdad ante dos transacciones concurrentes reales -- eso requeriría
threads reales contra un PostgreSQL real, con un connection pool que
permita conexiones simultáneas.

Lo que SÍ prueban aquí: (1) el comportamiento normal de un pago dentro del
saldo, (2) que un pago que excede el saldo se rechaza, (3) que el saldo se
recalcula de verdad contra el total ya comprometido -- un segundo pago que
por sí solo cabría en el monto original, pero no en el saldo restante tras
un primer pago, se rechaza (protege contra el error relacionado, más
sutil, de cachear/reusar un saldo leído antes del primer pago en vez de
releerlo), y (4) que un cargo_id inexistente sigue devolviendo 404 con la
consulta bloqueada (regresión: antes se usaba db.get_or_404(), ahora una
consulta manual con with_for_update() + abort(404)).

Antes de confiar en el fix en producción, correr una prueba de
concurrencia equivalente con threads reales contra un PostgreSQL real.
"""
from decimal import Decimal

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, Cargo, EstatusCargo


def _crear_cargo(alumno, concepto='Colegiatura de Prueba', monto='1000.00'):
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto=concepto,
        monto=Decimal(monto),
        estatus=EstatusCargo.PENDIENTE,
    )
    db.session.add(cargo)
    db.session.commit()
    return cargo


def test_pago_normal_dentro_del_saldo_se_acepta(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '400.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    assert respuesta.status_code == 200
    cargo_actualizado = db.session.get(Cargo, cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('400.00')
    assert cargo_actualizado.saldo_pendiente() == Decimal('600.00')


def test_pago_que_excede_el_saldo_se_rechaza(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '1500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    assert respuesta.status_code == 200
    cargo_actualizado = db.session.get(Cargo, cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('0.00')  # no se registró nada
    assert cargo_actualizado.estatus == EstatusCargo.PENDIENTE


def test_dos_pagos_que_juntos_exceden_el_saldo_el_segundo_se_rechaza(client, app):
    """
    Cargo de $1000. Pago 1 de $700 dentro del saldo -> aceptado (saldo
    queda en $300). Pago 2 de $500 -- por sí solo cabría en el monto
    original del cargo ($1000), pero NO en el saldo YA restante ($300) --
    debe rechazarse. Prueba que la validación siempre relee el saldo real
    contra lo ya comprometido, en vez de comparar contra el monto total
    del cargo o un saldo calculado una sola vez.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo(alumno, monto='1000.00')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '700.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )
    respuesta_2 = client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    assert respuesta_2.status_code == 200
    cargo_actualizado = db.session.get(Cargo, cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('700.00')  # solo el primer pago se registró
    assert cargo_actualizado.saldo_pendiente() == Decimal('300.00')
    assert b'mayor al saldo pendiente' in respuesta_2.data


def test_pagar_cargo_inexistente_devuelve_404(client, app):
    """
    Regresión: registrar_pago() dejó de usar db.get_or_404() (que ya
    manejaba el 404 solo) para poder bloquear la fila con
    with_for_update() -- la consulta manual + abort(404) debe seguir
    comportándose igual ante un cargo_id que no existe.
    """
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        '/cobro/999999/pagar',
        data={'monto_pagado': '100.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=False
    )

    assert respuesta.status_code == 404
