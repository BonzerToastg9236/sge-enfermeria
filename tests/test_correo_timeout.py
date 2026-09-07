"""
Pruebas del timeout SMTP y del aislamiento del correo respecto al pago
(hallazgo #6 de la auditoría).

CONTEXTO: Flask-Mail 0.10.0 crea la conexión con
`smtplib.SMTP(server, port)` SIN parámetro timeout (ver configure_host()
en la librería). Sin timeout, smtplib usa el default global de sockets,
que es "esperar indefinidamente": si Gmail se cuelga o responde lentísimo,
el worker de Gunicorn que atendía ese cobro queda bloqueado. Con solo 3
workers (deploy/sge.service), unos pocos cobros simultáneos con SMTP lento
dejan al sistema sin capacidad para nadie más.

El envío se mantiene SÍNCRONO a propósito (no se introduce Celery, colas
ni infraestructura nueva): lo que se agrega es un timeout explícito y
configurable, MAIL_TIMEOUT.

DISEÑO YA EXISTENTE QUE SE RESPETA: el correo es una notificación
SECUNDARIA. registrar_pago() hace db.session.commit() ANTES de llamar a
enviar_comprobante_pago(), y ese helper nunca deja escapar una excepción
(devuelve (False, motivo)). Un fallo de correo NUNCA debe revertir un
pago ya registrado.
"""

import smtplib
from decimal import Decimal
from unittest.mock import patch

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, mail, Cargo, EstatusCargo, Pago


def _crear_cargo(alumno, monto='1000.00'):
    cargo = Cargo(
        matricula_fk=alumno.matricula_id,
        concepto='Colegiatura de Prueba',
        monto=Decimal(monto),
        estatus=EstatusCargo.PENDIENTE,
    )
    db.session.add(cargo)
    db.session.commit()
    return cargo


def test_la_conexion_smtp_se_abre_con_timeout_explicito(app):
    """
    El fix de fondo: smtplib debe recibir un timeout. Se llama a
    configure_host() directamente porque en pruebas MAIL_SUPPRESS_SEND=True
    hace que Flask-Mail nunca abra una conexión real.
    """
    with patch('smtplib.SMTP') as smtp_falso:
        conexion = mail.connect()
        conexion.configure_host()

    assert smtp_falso.called, 'No se intentó abrir ninguna conexión SMTP'
    _, kwargs = smtp_falso.call_args
    assert 'timeout' in kwargs, 'smtplib.SMTP se abrió SIN timeout: puede bloquear al worker para siempre'
    assert kwargs['timeout'] == app.config['MAIL_TIMEOUT']
    assert kwargs['timeout'] > 0


def test_el_timeout_es_configurable_desde_la_configuracion(app):
    """MAIL_TIMEOUT debe poder ajustarse sin tocar el código."""
    original = app.config['MAIL_TIMEOUT']
    app.config['MAIL_TIMEOUT'] = 3
    try:
        with patch('smtplib.SMTP') as smtp_falso:
            mail.connect().configure_host()
        assert smtp_falso.call_args[1]['timeout'] == 3
    finally:
        app.config['MAIL_TIMEOUT'] = original


def test_correo_exitoso_reporta_exito_y_registra_el_pago(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    alumno.correo = 'alumna@example.com'
    db.session.commit()
    cargo = _crear_cargo(alumno)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    with mail.record_messages() as bandeja:
        respuesta = client.post(
            f'/cobro/{cargo.id}/pagar',
            data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
            follow_redirects=True
        )

    assert respuesta.status_code == 200
    assert len(bandeja) == 1
    assert bandeja[0].recipients == ['alumna@example.com']
    assert db.session.get(Cargo, cargo.id).total_pagado() == Decimal('500.00')


def test_un_timeout_de_smtp_no_revierte_el_pago(client, app):
    """
    LO MÁS IMPORTANTE DE ESTE BLOQUE: el pago ya está commiteado antes de
    intentar el correo. Si el SMTP truena por timeout, el pago debe seguir
    registrado y el usuario debe ver un aviso -- nunca un error 500 ni un
    pago perdido.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)
    alumno.correo = 'alumna@example.com'
    db.session.commit()
    cargo = _crear_cargo(alumno)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    with patch.object(mail, 'send', side_effect=TimeoutError('timed out')):
        respuesta = client.post(
            f'/cobro/{cargo.id}/pagar',
            data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
            follow_redirects=True
        )

    assert respuesta.status_code == 200, 'Un fallo de correo nunca debe romper la petición'
    cargo_actualizado = db.session.get(Cargo, cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('500.00'), 'El pago se perdió por un fallo de correo'
    assert cargo_actualizado.estatus == EstatusCargo.PARCIAL
    assert Pago.query.filter_by(cargo_fk=cargo.id).count() == 1
    assert 'no se pudo enviar el comprobante'.encode('utf-8') in respuesta.data


def test_un_fallo_de_smtp_tampoco_revierte_el_pago(client, app):
    """Mismo caso con una excepción propia de smtplib, no solo TimeoutError."""
    plan = crear_plan()
    alumno = crear_alumno(plan)
    alumno.correo = 'alumna@example.com'
    db.session.commit()
    cargo = _crear_cargo(alumno)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    with patch.object(mail, 'send', side_effect=smtplib.SMTPServerDisconnected('conexión caída')):
        respuesta = client.post(
            f'/cobro/{cargo.id}/pagar',
            data={'monto_pagado': '1000.00', 'metodo_pago': 'EFECTIVO'},
            follow_redirects=True
        )

    assert respuesta.status_code == 200
    cargo_actualizado = db.session.get(Cargo, cargo.id)
    assert cargo_actualizado.total_pagado() == Decimal('1000.00')
    assert cargo_actualizado.estatus == EstatusCargo.PAGADO


def test_alumno_sin_correo_no_intenta_enviar_ni_rompe_el_pago(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)  # sin correo
    cargo = _crear_cargo(alumno)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    with mail.record_messages() as bandeja:
        respuesta = client.post(
            f'/cobro/{cargo.id}/pagar',
            data={'monto_pagado': '400.00', 'metodo_pago': 'EFECTIVO'},
            follow_redirects=True
        )

    assert respuesta.status_code == 200
    assert len(bandeja) == 0
    assert db.session.get(Cargo, cargo.id).total_pagado() == Decimal('400.00')
