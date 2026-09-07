"""
Red de seguridad para la reorganización de rutas en blueprints (Plan 2,
docs/superpowers/specs/2026-09-07-organizar-rutas-blueprints-design.md).

Escanea cada plantilla en busca de url_for('endpoint', ...) y confirma que
ese endpoint existe de verdad en app.url_map. Sin esto, un url_for con el
nombre viejo de un endpoint renombrado NO falla al arrancar la app: falla
hasta que alguien abre esa pantalla en concreto -- que puede ser una
pantalla que ninguna de las 166 pruebas visita.

LIMITACIÓN CONOCIDA: solo detecta url_for('literal'), no
url_for(variable). Verificado en las 31 plantillas actuales: las 98
llamadas a url_for() son todas de la forma url_for('endpoint', ...) con
comillas simples y en una sola línea -- no hay ningún caso dinámico hoy.
Si algún día aparece uno, esta prueba simplemente no lo cubre; no lo
reporta como error.
"""

import re
from pathlib import Path

from app import app


PATRON_URL_FOR = re.compile(r"url_for\(\s*'([a-zA-Z_][a-zA-Z0-9_.]*)'")

DIRECTORIO_PLANTILLAS = Path(__file__).resolve().parent.parent / 'templates'


def _endpoints_referenciados_por_plantilla():
    """
    Devuelve una lista de (ruta_relativa, numero_de_linea, endpoint) por
    cada url_for('...') encontrado en cada archivo .html bajo templates/.
    """
    referencias = []
    for plantilla in sorted(DIRECTORIO_PLANTILLAS.rglob('*.html')):
        texto = plantilla.read_text(encoding='utf-8')
        ruta_relativa = plantilla.relative_to(DIRECTORIO_PLANTILLAS)
        for numero_linea, linea in enumerate(texto.splitlines(), start=1):
            for coincidencia in PATRON_URL_FOR.finditer(linea):
                referencias.append((str(ruta_relativa), numero_linea, coincidencia.group(1)))
    return referencias


def test_todas_las_plantillas_referencian_endpoints_que_existen(app):
    referencias = _endpoints_referenciados_por_plantilla()
    assert referencias, 'No se encontró ningún url_for() en las plantillas -- revisa la ruta de escaneo'

    endpoints_validos = {regla.endpoint for regla in app.url_map.iter_rules()}

    rotos = [
        f'{plantilla}:{linea} -> url_for(\'{endpoint}\')'
        for plantilla, linea, endpoint in referencias
        if endpoint not in endpoints_validos
    ]

    assert not rotos, (
        'Estas plantillas referencian endpoints que ya no existen '
        '(url_for roto):\n' + '\n'.join(rotos)
    )
