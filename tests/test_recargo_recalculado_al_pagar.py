"""
Pruebas anti-regresión del recargo no recalculado al pagar (hallazgo #4
de la auditoría, plan de remediación paso 4).

CONTEXTO: rutas/cobros.py::registrar_pago() calcula el saldo con el
recargo_aplicado YA GUARDADO en BD (cargo.saldo_pendiente()). Ese campo
solo se actualiza cuando alguien visita /alumno/<matricula>/cobros
(ver cobros(), que llama a actualizar_recargo_si_vencido() por cada
cargo). Si un cargo vencido nunca se volvió a consultar así, su
recargo_aplicado sigue en $0.00 -- y un pago por el monto ORIGINAL
(sin recargo) deja el cargo PAGADO, perdiendo el recargo para siempre:
saldo_pendiente() vuelve a dar $0.00 y no hay ninguna otra señal de que
faltó cobrar algo.

La corrección llama a cargo.actualizar_recargo_si_vencido() dentro de
registrar_pago(), después del with_for_update() (para que el recálculo
quede protegido por el mismo bloqueo de fila) y antes de
cargo.saldo_pendiente() (para que el saldo contra el que se valida el
pago ya incluya el recargo del día).
"""

from decimal import Decimal

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from tests.test_recargos_y_conceptos import _crear_cargo_vencido, _configurar_recargo

from app import EstatusCargo, TipoRecargo, RolUsuario


def test_pagar_por_el_monto_original_no_pierde_el_recargo_de_un_cargo_nunca_consultado(client, app):
    """
    El caso exacto del hallazgo: nadie abrió antes la ficha del alumno
    (recargo_aplicado sigue en $0.00 en BD) y llega un pago por el monto
    ORIGINAL del cargo, sin el recargo que ya le correspondía por estar
    vencido.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo_vencido(alumno, dias_vencido=10, monto='1000.00')
    _configurar_recargo(TipoRecargo.MONTO_FIJO, '150.00')
    crear_usuario(rol=RolUsuario.CONTADOR)
    login(client, 'directivo1', 'clave12345')

    assert cargo.recargo_aplicado == Decimal('0.00'), 'Precondición: nadie ha consultado este cargo todavía'

    respuesta = client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '1000.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True,
    )

    assert respuesta.status_code == 200

    assert cargo.recargo_aplicado == Decimal('150.00'), (
        'El recargo debió recalcularse ANTES de validar el pago, no quedarse en $0.00'
    )
    assert cargo.estatus == EstatusCargo.PARCIAL, (
        f'Con el recargo ya incluido, $1000 no cubre el saldo completo ($1150): '
        f'el cargo debería quedar PARCIAL, no {cargo.estatus}. Si quedó PAGADO, '
        'el recargo se perdió para siempre.'
    )
    assert cargo.saldo_pendiente() == Decimal('150.00')


def test_pagar_un_cargo_no_vencido_no_agrega_recargo(client, app):
    """Regresión inversa: el recálculo no debe inventar un recargo donde no corresponde."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    cargo = _crear_cargo_vencido(alumno, dias_vencido=-10, monto='1000.00')  # vence en el futuro
    _configurar_recargo(TipoRecargo.MONTO_FIJO, '150.00')
    crear_usuario(rol=RolUsuario.CONTADOR)
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '1000.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True,
    )

    assert respuesta.status_code == 200
    assert cargo.recargo_aplicado == Decimal('0.00')
    assert cargo.estatus == EstatusCargo.PAGADO
