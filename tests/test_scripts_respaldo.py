"""
Los scripts de respaldo se probaron a mano contra PostgreSQL real (respaldo -> desastre -> restauración
desde copia cifrada: huellas idénticas). Estas pruebas no repiten eso (necesitan PostgreSQL) pero fijan
las garantías de seguridad para que nadie las quite sin darse cuenta.
"""

import os
import re
import subprocess

import pytest

DEPLOY = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'deploy')
SCRIPTS = ['backup.sh', 'restore.sh', 'verificar_respaldo.sh', 'lib_respaldo.sh']


def _leer(nombre):
    with open(os.path.join(DEPLOY, nombre), encoding='utf-8') as f:
        return f.read()


def _sin_comentarios(nombre):
    return '\n'.join(l for l in _leer(nombre).splitlines() if not l.strip().startswith('#'))


@pytest.mark.parametrize('script', SCRIPTS)
def test_la_sintaxis_de_bash_es_valida(script):
    assert subprocess.run(['bash', '-n', os.path.join(DEPLOY, script)], capture_output=True).returncode == 0


@pytest.mark.parametrize('script', ['backup.sh', 'restore.sh', 'verificar_respaldo.sh'])
def test_los_scripts_paran_ante_el_primer_error_y_no_dejan_archivos_legibles_por_otros(script):
    codigo = _sin_comentarios(script)
    assert 'set -euo pipefail' in codigo and 'umask 077' in codigo


def test_nada_se_sube_a_la_nube_sin_cifrar():
    codigo = _sin_comentarios('backup.sh')
    subidas = re.findall(r'rclone copy\s+(\S+)', codigo)
    assert subidas == ['"$cifrado"'], f'rclone copy solo debe recibir el archivo cifrado, recibe: {subidas}'
    assert '--symmetric' in codigo and 'AES256' in codigo


def test_sin_frase_de_cifrado_no_se_sube_nada_y_se_avisa():
    codigo = _sin_comentarios('backup.sh')
    assert re.search(r'else\s+alertar "Copia externa NO subida"', codigo)


def test_un_dump_cortado_se_detecta_en_backup_restore_y_verificacion():
    for script in ('backup.sh', 'restore.sh', 'verificar_respaldo.sh'):
        assert 'PostgreSQL database dump complete' in _leer(script), f'{script} no comprueba que el dump esté completo'


def test_la_restauracion_se_detiene_ante_el_primer_error_y_guarda_copia_de_seguridad():
    codigo = _sin_comentarios('restore.sh')
    assert 'ON_ERROR_STOP=1' in codigo and 'antes_de_restaurar_' in codigo
    assert codigo.index('gzip -t') < codigo.index('read -r -p')             # valida ANTES de pedir confirmación y de tocar nada


def test_la_verificacion_semanal_restaura_de_verdad_y_limpia_su_base_temporal():
    codigo = _sin_comentarios('verificar_respaldo.sh')
    assert 'createdb' in codigo and 'psql -v ON_ERROR_STOP=1' in codigo and 'dropdb' in codigo


def test_el_cron_documentado_incluye_respaldo_verificacion_diaria_y_prueba_semanal():
    guia = _leer('BACKUPS.md')
    for linea in ('backup.sh\n', 'backup.sh --solo-bd', 'verificar_respaldo.sh --frescura', 'verificar_respaldo.sh\n'):
        assert linea in guia, f'BACKUPS.md no documenta: {linea.strip()}'
