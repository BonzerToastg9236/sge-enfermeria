"""
El detalle técnico de un fallo de correo no debe llegar a la interfaz.

PROBLEMA: enviar_comprobante_pago() y enviar_recordatorio_vencimiento()
devolvían `str(error)` -- el texto crudo de la excepción -- y ese texto se
mostraba tal cual en dos lugares:
  * el flash de registrar_pago ("...por correo ({error_correo})")
  * la lista de fallidos de recordatorios_vencimiento.html

Un error de smtplib puede incluir el host del servidor de correo, la
cuenta usada, códigos internos y rutas del sistema. Es información de
infraestructura que no aporta nada a quien está cobrando en ventanilla, y
que no tiene por qué quedar en pantalla (ni en una captura, ni en un
correo de soporte reenviado).

AHORA: el personal ve un mensaje genérico y accionable; el detalle
completo se registra con app.logger para quien administra el servidor.
"""

from decimal import Decimal
from unittest.mock import patch

import smtplib

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import db, mail, Cargo, EstatusCargo


# Excepción con pinta de la real: incluye host, cuenta y un código interno.
DETALLE_TECNICO = (
    "(535, b'5.7.8 Username and Password not accepted. "
    "smtp.gmail.com[74.125.20.109] cuenta-real@escuela.edu.mx')"
)


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


def _alumno_con_correo_y_sesion(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    alumno.correo = 'alumna@example.com'
    db.session.commit()
    crear_usuario()
    login(client, 'directivo1', 'clave12345')
    return alumno


def test_el_detalle_de_la_excepcion_smtp_no_aparece_en_la_respuesta(client, app):
    alumno = _alumno_con_correo_y_sesion(client, app)
    cargo = _crear_cargo(alumno)

    with patch.object(mail, 'send', side_effect=smtplib.SMTPAuthenticationError(535, DETALLE_TECNICO)):
        respuesta = client.post(
            f'/cobro/{cargo.id}/pagar',
            data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
            follow_redirects=True
        )

    assert respuesta.status_code == 200
    assert b'smtp.gmail.com' not in respuesta.data, 'Se filtró el host del servidor de correo'
    assert b'Username and Password not accepted' not in respuesta.data
    assert b'cuenta-real@escuela.edu.mx' not in respuesta.data
    assert b'535' not in respuesta.data


def test_aun_asi_se_avisa_que_el_comprobante_no_se_envio(client, app):
    """El mensaje genérico debe seguir siendo útil: decir qué pasó y qué hacer."""
    alumno = _alumno_con_correo_y_sesion(client, app)
    cargo = _crear_cargo(alumno)

    with patch.object(mail, 'send', side_effect=smtplib.SMTPAuthenticationError(535, DETALLE_TECNICO)):
        respuesta = client.post(
            f'/cobro/{cargo.id}/pagar',
            data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
            follow_redirects=True
        )

    assert 'no se pudo enviar el comprobante'.encode('utf-8') in respuesta.data
    # Y el pago sigue registrado (fix #6, no debe cambiar)
    assert db.session.get(Cargo, cargo.id).total_pagado() == Decimal('500.00')


def test_el_detalle_tecnico_si_queda_en_el_log_del_servidor(client, app, caplog):
    """
    Lo que se quita de la pantalla NO se pierde: tiene que quedar
    disponible para quien administra el servidor.
    """
    alumno = _alumno_con_correo_y_sesion(client, app)
    cargo = _crear_cargo(alumno)

    with caplog.at_level('WARNING'):
        with patch.object(mail, 'send', side_effect=smtplib.SMTPAuthenticationError(535, DETALLE_TECNICO)):
            client.post(
                f'/cobro/{cargo.id}/pagar',
                data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
                follow_redirects=True
            )

    assert 'Username and Password not accepted' in caplog.text, \
        'El detalle técnico no quedó registrado en ningún lado'


def test_los_recordatorios_tampoco_exponen_el_detalle_tecnico(client, app):
    """
    Misma fuga por otra pantalla: recordatorios_vencimiento.html renderiza
    el motivo de cada envío fallido.
    """
    from datetime import timedelta
    from app import hoy_local, DIAS_AVISO_VENCIMIENTO

    alumno = _alumno_con_correo_y_sesion(client, app)
    cargo = _crear_cargo(alumno)
    cargo.fecha_vencimiento = hoy_local() + timedelta(days=DIAS_AVISO_VENCIMIENTO - 1)
    db.session.commit()

    with patch.object(mail, 'send', side_effect=smtplib.SMTPAuthenticationError(535, DETALLE_TECNICO)):
        respuesta = client.post('/cobros/recordatorios-vencimiento', follow_redirects=True)

    assert respuesta.status_code == 200
    assert b'smtp.gmail.com' not in respuesta.data
    assert b'cuenta-real@escuela.edu.mx' not in respuesta.data


def test_el_alumno_sin_correo_sigue_dando_su_mensaje_propio(client, app):
    """
    Regresión: 'El alumno no tiene correo registrado' NO es un detalle
    técnico -- es información útil y debe seguir mostrándose.
    """
    plan = crear_plan()
    alumno = crear_alumno(plan)  # sin correo
    cargo = _crear_cargo(alumno)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.post(
        f'/cobro/{cargo.id}/pagar',
        data={'monto_pagado': '500.00', 'metodo_pago': 'EFECTIVO'},
        follow_redirects=True
    )

    assert 'no tiene correo registrado'.encode('utf-8') in respuesta.data
