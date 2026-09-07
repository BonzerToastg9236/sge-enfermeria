"""
Pruebas de la validación real de archivos del expediente (hallazgo #7).

ANTES: extension_permitida() solo miraba el texto después del último punto
del nombre. Cualquier contenido (un .exe, un HTML con <script>, lo que
fuera) pasaba con solo renombrarlo a "documento.pdf".

AHORA: además de la extensión se comprueban los BYTES INICIALES del
archivo (magic bytes) y que correspondan a la extensión declarada.

Se validan los 3 formatos que el sistema ya aceptaba -- PDF, JPEG y PNG --
sin agregar dependencias nuevas (python-magic necesitaría libmagic, un
paquete del sistema; para 3 formatos de firma fija no hace falta).

Todas estas pruebas escriben en un UPLOAD_FOLDER temporal, nunca en
instance/documentos_alumnos/.
"""

import os
from io import BytesIO

import pytest

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import DocumentoAlumno


# --- Contenidos mínimos con la firma real de cada formato ---
PDF_VALIDO = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n'
JPEG_VALIDO = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00' + b'\x00' * 40 + b'\xff\xd9'
PNG_VALIDO = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'\x00' * 40

# Un ejecutable de Windows renombrado: 'MZ' es la firma de los .exe
EXE_DISFRAZADO = b'MZ\x90\x00\x03\x00\x00\x00' + b'\x00' * 60
HTML_CON_SCRIPT = b'<html><body><script>alert("xss")</script></body></html>'


@pytest.fixture
def carpeta_subidas(app, tmp_path):
    """Aísla las subidas de la prueba en un directorio temporal."""
    original = app.config['UPLOAD_FOLDER']
    app.config['UPLOAD_FOLDER'] = str(tmp_path)
    yield tmp_path
    app.config['UPLOAD_FOLDER'] = original


@pytest.fixture
def alumno_con_sesion(client, app):
    plan = crear_plan()
    alumno = crear_alumno(plan)
    crear_usuario()
    login(client, 'directivo1', 'clave12345')
    return alumno


def _subir(client, alumno, contenido, nombre_archivo, campo='archivo_ine'):
    return client.post(
        f'/alumno/{alumno.matricula_id}/documentos',
        data={campo: (BytesIO(contenido), nombre_archivo)},
        content_type='multipart/form-data',
        follow_redirects=True,
    )


def _archivos_en_disco(carpeta, matricula):
    carpeta_alumno = os.path.join(str(carpeta), matricula)
    if not os.path.isdir(carpeta_alumno):
        return []
    return os.listdir(carpeta_alumno)


# ---------------------------------------------------------------------------
# Formatos legítimos: deben SEGUIR funcionando
# ---------------------------------------------------------------------------

def test_pdf_valido_se_acepta(client, app, alumno_con_sesion, carpeta_subidas):
    respuesta = _subir(client, alumno_con_sesion, PDF_VALIDO, 'ine.pdf')

    assert respuesta.status_code == 200
    assert DocumentoAlumno.query.count() == 1
    assert len(_archivos_en_disco(carpeta_subidas, alumno_con_sesion.matricula_id)) == 1


def test_jpeg_valido_se_acepta(client, app, alumno_con_sesion, carpeta_subidas):
    respuesta = _subir(client, alumno_con_sesion, JPEG_VALIDO, 'ine.jpg')

    assert respuesta.status_code == 200
    assert DocumentoAlumno.query.count() == 1


def test_jpeg_con_extension_jpeg_tambien_se_acepta(client, app, alumno_con_sesion, carpeta_subidas):
    _subir(client, alumno_con_sesion, JPEG_VALIDO, 'ine.jpeg')

    assert DocumentoAlumno.query.count() == 1


def test_png_valido_se_acepta(client, app, alumno_con_sesion, carpeta_subidas):
    _subir(client, alumno_con_sesion, PNG_VALIDO, 'foto.png')

    assert DocumentoAlumno.query.count() == 1


def test_el_archivo_guardado_conserva_su_contenido_completo(client, app, alumno_con_sesion, carpeta_subidas):
    """
    Leer los primeros bytes para validarlos NO debe dejar el puntero
    adelantado: el archivo debe guardarse íntegro, no truncado.
    """
    _subir(client, alumno_con_sesion, PDF_VALIDO, 'ine.pdf')

    documento = DocumentoAlumno.query.first()
    ruta = os.path.join(str(carpeta_subidas), documento.ruta_archivo)
    with open(ruta, 'rb') as archivo_guardado:
        assert archivo_guardado.read() == PDF_VALIDO


# ---------------------------------------------------------------------------
# Contenido que NO corresponde a la extensión: debe rechazarse
# ---------------------------------------------------------------------------

def test_ejecutable_renombrado_como_pdf_se_rechaza(client, app, alumno_con_sesion, carpeta_subidas):
    respuesta = _subir(client, alumno_con_sesion, EXE_DISFRAZADO, 'documento.pdf')

    assert respuesta.status_code == 200
    assert DocumentoAlumno.query.count() == 0, 'Se registró un archivo con contenido no permitido'
    assert _archivos_en_disco(carpeta_subidas, alumno_con_sesion.matricula_id) == [], \
        'El archivo se escribió en disco pese a ser inválido'
    assert 'contenido'.encode('utf-8') in respuesta.data.lower()


def test_html_con_script_renombrado_como_jpg_se_rechaza(client, app, alumno_con_sesion, carpeta_subidas):
    _subir(client, alumno_con_sesion, HTML_CON_SCRIPT, 'foto.jpg')

    assert DocumentoAlumno.query.count() == 0
    assert _archivos_en_disco(carpeta_subidas, alumno_con_sesion.matricula_id) == []


def test_png_renombrado_como_pdf_se_rechaza(client, app, alumno_con_sesion, carpeta_subidas):
    """
    Ambos formatos están permitidos, pero el contenido debe coincidir con
    la extensión: si no, el archivo se serviría después con el Content-Type
    equivocado y (con nosniff activo en Nginx) el navegador no lo mostraría.
    """
    _subir(client, alumno_con_sesion, PNG_VALIDO, 'documento.pdf')

    assert DocumentoAlumno.query.count() == 0


def test_archivo_vacio_se_rechaza(client, app, alumno_con_sesion, carpeta_subidas):
    _subir(client, alumno_con_sesion, b'', 'ine.pdf')

    assert DocumentoAlumno.query.count() == 0


# ---------------------------------------------------------------------------
# Extensión prohibida y tamaño
# ---------------------------------------------------------------------------

def test_extension_prohibida_se_rechaza(client, app, alumno_con_sesion, carpeta_subidas):
    respuesta = _subir(client, alumno_con_sesion, PDF_VALIDO, 'documento.exe')

    assert respuesta.status_code == 200
    assert DocumentoAlumno.query.count() == 0
    assert 'formato no permitido'.encode('utf-8') in respuesta.data.lower()


def test_svg_no_se_permite(client, app, alumno_con_sesion, carpeta_subidas):
    """SVG puede contener <script>; no está en la lista y debe seguir fuera."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    _subir(client, alumno_con_sesion, svg, 'foto.svg')

    assert DocumentoAlumno.query.count() == 0


def test_archivo_demasiado_grande_se_rechaza_con_mensaje_claro(client, app, alumno_con_sesion, carpeta_subidas):
    """
    MAX_CONTENT_LENGTH (8 MB) lo aplica Werkzeug antes de llegar a la vista.
    Sin un handler propio, el usuario ve la página cruda de error 413.
    """
    demasiado_grande = PDF_VALIDO + b'\x00' * (9 * 1024 * 1024)

    respuesta = _subir(client, alumno_con_sesion, demasiado_grande, 'ine.pdf')

    assert DocumentoAlumno.query.count() == 0
    assert respuesta.status_code == 200, 'Debe redirigir con un mensaje, no dejar el 413 crudo'
    assert 'demasiado grande'.encode('utf-8') in respuesta.data.lower()
