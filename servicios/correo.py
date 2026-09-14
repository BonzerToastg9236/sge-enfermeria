"""
Envío de correo transaccional (comprobante de pago, recordatorio de
vencimiento). Nunca revienta el flujo que lo llama: siempre regresa
(ok: bool, motivo: str | None). El detalle técnico de un fallo SMTP se
registra con logger.warning(exc_info=True) y NUNCA se devuelve a la
interfaz -- MOTIVO_GENERICO_FALLO_CORREO es lo único que puede llegar a
pantalla (ver tests/test_errores_smtp_no_expuestos.py).
"""

from flask import current_app
from flask_mail import Message

from extensiones import mail


# SECURITY-NOTE: lo que se le dice al personal cuando falla un envío.
# Antes se devolvía str(error), y ese texto se mostraba tal cual en el
# flash del cobro y en la lista de recordatorios fallidos. Una excepción
# de smtplib puede traer el host del servidor de correo, la cuenta usada
# y códigos internos: información de infraestructura que no le sirve a
# quien está cobrando en ventanilla y que no debe quedar en pantalla.
# El detalle completo NO se pierde -- va al log del servidor con
# app.logger.warning(..., exc_info=True), que es donde lo necesita quien
# administra el VPS (logs/sge.log, ver create_app).
MOTIVO_GENERICO_FALLO_CORREO = (
    'No se pudo conectar con el servidor de correo. Revisa la conexión a internet '
    'o la configuración de correo del sistema; el detalle técnico quedó en el log '
    'del servidor.'
)


def enviar_comprobante_pago(alumno, cargo, pago):
    """
    Envía el comprobante de pago al correo del alumno. Si el alumno no
    tiene correo registrado, o si falla el envío (sin internet, SMTP
    caído, etc.), NO debe romper el registro del pago -- el pago ya
    quedó guardado en la BD; solo se avisa al usuario que el correo no
    se pudo mandar.
    """
    if not alumno.correo:
        return False, 'El alumno no tiene correo registrado.'

    try:
        mensaje = Message(
            subject=f'Comprobante de Pago - Folio {pago.folio or ("#" + str(pago.id))}',
            recipients=[alumno.correo],
            body=(
                f'Hola {alumno.nombre_completo},\n\n'
                f'Se registró tu pago con los siguientes datos:\n\n'
                f'Concepto: {cargo.concepto}\n'
                f'Monto pagado: ${pago.monto_pagado}\n'
                f'Fecha: {pago.fecha_pago.strftime("%d/%m/%Y %H:%M")}\n'
                f'Folio: {pago.folio or ("#" + str(pago.id))}\n'
                f'Saldo pendiente del cargo: ${cargo.saldo_pendiente()}\n\n'
                f'Este es un correo automático del Sistema de Gestión Escolar.'
            ),
        )
        mail.send(mensaje)
        return True, None
    except Exception:
        current_app.logger.warning(
            'Falló el envío del comprobante del pago %s (cargo %s, alumno %s).',
            pago.id, cargo.id, alumno.matricula_id, exc_info=True
        )
        return False, MOTIVO_GENERICO_FALLO_CORREO


DIAS_AVISO_VENCIMIENTO = 3  # Manda el recordatorio cuando falten esta cantidad de días (o menos) para vencer


def enviar_recordatorio_vencimiento(alumno, cargo):
    """Igual que enviar_comprobante_pago: nunca truena, solo reporta si pudo o no."""
    if not alumno.correo:
        return False, 'El alumno no tiene correo registrado.'

    try:
        mensaje = Message(
            subject=f'Recordatorio: {cargo.concepto} próximo a vencer',
            recipients=[alumno.correo],
            body=(
                f'Hola {alumno.nombre_completo},\n\n'
                f'Te recordamos que tienes un pago próximo a vencer:\n\n'
                f'Concepto: {cargo.concepto}\n'
                f'Periodo: {cargo.periodo_escolar or "N/A"}\n'
                f'Saldo pendiente: ${cargo.saldo_pendiente()}\n'
                f'Fecha límite: {cargo.fecha_vencimiento.strftime("%d/%m/%Y")}\n\n'
                f'Después de esta fecha se aplica un recargo por atraso.\n\n'
                f'Este es un correo automático del Sistema de Gestión Escolar.'
            ),
        )
        mail.send(mensaje)
        return True, None
    except Exception:
        current_app.logger.warning(
            'Falló el envío del recordatorio de vencimiento del cargo %s (alumno %s).',
            cargo.id, alumno.matricula_id, exc_info=True
        )
        return False, MOTIVO_GENERICO_FALLO_CORREO
