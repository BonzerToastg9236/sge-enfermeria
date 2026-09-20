#!/usr/bin/env python3
"""
Avisa de un fallo de respaldo por correo (si hay ALERTA_CORREO y credenciales SMTP en .env).

    avisar.py "Asunto" "Mensaje"

Nunca falla ni bloquea al respaldo: si no hay correo configurado o el envío no funciona, el
aviso igual queda escrito en ALERTA_ULTIMO_FALLO.txt (lo escribe alertar() en lib_respaldo.sh).
Variables (en el .env de la app): ALERTA_CORREO (destinatario), MAIL_USERNAME, MAIL_PASSWORD;
opcionales MAIL_SERVER (smtp.gmail.com) y MAIL_PORT (587).
"""
import os
import smtplib
import sys
from email.message import EmailMessage

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.environ.get('APP_DIR', os.path.join(os.path.dirname(__file__), '..')), '.env'))
except Exception:
    pass  # sin python-dotenv se usan solo las variables ya presentes en el entorno


def main():
    asunto = sys.argv[1] if len(sys.argv) > 1 else 'Aviso del SGE'
    mensaje = sys.argv[2] if len(sys.argv) > 2 else ''
    destino = os.environ.get('ALERTA_CORREO', '').strip()
    usuario, clave = os.environ.get('MAIL_USERNAME', ''), os.environ.get('MAIL_PASSWORD', '')
    if not (destino and usuario and clave) or 'tu-contraseña' in clave:
        print('avisar.py: sin ALERTA_CORREO / credenciales de correo; el aviso queda solo en el archivo de alerta.')
        return
    correo = EmailMessage()
    correo['Subject'] = f'[SGE] {asunto}'
    correo['From'] = usuario
    correo['To'] = destino
    correo.set_content(f'{mensaje}\n\nServidor: {os.uname().nodename}\nRevisa backup.log en la carpeta de respaldos.')
    try:
        with smtplib.SMTP(os.environ.get('MAIL_SERVER', 'smtp.gmail.com'), int(os.environ.get('MAIL_PORT', '587')), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(usuario, clave)
            smtp.send_message(correo)
        print(f'avisar.py: correo enviado a {destino}.')
    except Exception as error:  # noqa: BLE001 - un aviso nunca debe romper el respaldo
        print(f'avisar.py: no se pudo enviar el correo ({type(error).__name__}).')


if __name__ == '__main__':
    main()
