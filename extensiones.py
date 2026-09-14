"""
Instancias compartidas de las extensiones de Flask.

Viven en su propio módulo (en vez de en app.py) para que modelos/,
utilidades/, servicios/ y rutas/ puedan importar `db`, `mail`, etc. sin
crear un import circular con app.py (que es quien las inicializa con
init_app() dentro de create_app()).
"""

import smtplib

from flask import current_app
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_mail import Mail, Connection

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# CORREO CON TIMEOUT (hallazgo #6 de la auditoría)
# ---------------------------------------------------------------------------
# Flask-Mail 0.10.0 abre la conexión con `smtplib.SMTP(server, port)`, SIN
# timeout y sin ninguna opción de configuración para agregarlo (ver
# Connection.configure_host() en la librería). Sin timeout, smtplib hereda
# el default global de sockets: esperar indefinidamente. Como el envío del
# comprobante es SÍNCRONO dentro del flujo de cobro, un SMTP colgado
# bloquearía al worker de Gunicorn que atiende ese pago.
#
# La única forma de agregarlo sin cambiar de librería ni de arquitectura es
# sobrescribir ese método. Se replica tal cual el original y solo se agrega
# `timeout=`. NOTA DE MANTENIMIENTO: esto queda acoplado a la
# implementación de Flask-Mail 0.10.0 -- si algún día se actualiza la
# librería, revisar que configure_host() siga teniendo esta forma (o
# quitar este parche si para entonces ya soporta timeout de fábrica).
class _ConexionSMTPConTimeout(Connection):
    def configure_host(self):
        timeout = current_app.config['MAIL_TIMEOUT']

        if self.mail.use_ssl:
            host = smtplib.SMTP_SSL(self.mail.server, self.mail.port, timeout=timeout)
        else:
            host = smtplib.SMTP(self.mail.server, self.mail.port, timeout=timeout)

        host.set_debuglevel(int(self.mail.debug))

        if self.mail.use_tls:
            host.starttls()

        if self.mail.username and self.mail.password:
            host.login(self.mail.username, self.mail.password)

        return host


class _CorreoConTimeout(Mail):
    """Idéntico a Flask-Mail salvo que sus conexiones llevan MAIL_TIMEOUT."""

    def connect(self):
        app_actual = getattr(self, 'app', None) or current_app
        try:
            return _ConexionSMTPConTimeout(app_actual.extensions['mail'])
        except KeyError as error:
            raise RuntimeError(
                'La aplicación no está configurada con Flask-Mail.'
            ) from error


mail = _CorreoConTimeout()
