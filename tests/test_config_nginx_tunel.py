"""
La variante de Nginx para Cloudflare Tunnel (deploy/nginx_sge_tunel.conf) existe por una razón concreta:
detrás de un túnel todas las visitas llegan desde 127.0.0.1 y, con la config normal, el límite de
intentos de login (por IP) contaría a TODOS los usuarios como uno solo. Estas pruebas fijan lo que la
hace correcta para que nadie lo quite sin darse cuenta.
"""

import os
import re

from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.test import EnvironBuilder

RUTA = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy', 'nginx_sge_tunel.conf')


def _conf():
    with open(RUTA, encoding='utf-8') as f:
        return re.sub(r'#.*', '', f.read())          # sin comentarios


def test_recupera_la_ip_real_solo_si_la_manda_el_tunel_local():
    c = _conf()
    assert 'set_real_ip_from 127.0.0.1;' in c and 'real_ip_header CF-Connecting-IP;' in c


def test_reenvia_la_ip_real_y_no_mezcla_la_local():
    c = _conf()
    assert 'proxy_set_header X-Forwarded-For $remote_addr;' in c
    assert 'proxy_params' not in c and '$proxy_add_x_forwarded_for' not in c   # volverían a sumar 127.0.0.1


def test_solo_escucha_en_local_porque_lo_expone_el_tunel():
    assert 'listen 127.0.0.1:80;' in _conf()


def test_la_app_usa_la_ultima_ip_de_x_forwarded_for_para_el_limite_de_intentos():
    """Con esa cabecera (un solo valor: la IP real) ProxyFix(x_for=1) deja a cada visitante con su propia IP."""
    capturado = {}

    def app(environ, start_response):
        capturado['ip'] = environ['REMOTE_ADDR']
        start_response('200 OK', []); return [b'']

    envuelta = ProxyFix(app, x_for=1, x_proto=1, x_host=1)
    for ip in ('203.0.113.5', '198.51.100.9'):
        entorno = EnvironBuilder(headers={'X-Forwarded-For': ip, 'X-Forwarded-Proto': 'https'}).get_environ()
        entorno['REMOTE_ADDR'] = '127.0.0.1'
        envuelta(entorno, lambda *a: None)
        assert capturado['ip'] == ip
