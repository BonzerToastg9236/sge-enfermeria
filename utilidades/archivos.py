"""
Validación de archivos subidos (expediente del alumno): extensión
permitida por configuración, y firma real (magic bytes) del contenido
(fix #7 -- la extensión sola no prueba nada, ver docstring movido).
"""

import zipfile

from flask import current_app

FIRMAS_POR_EXTENSION = {
    'pdf': (b'%PDF-',),
    'png': (b'\x89PNG\r\n\x1a\n',),
    'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
}

BYTES_DE_FIRMA = 8  # Suficiente para la firma más larga (PNG)


def extension_permitida(nombre_archivo: str) -> bool:
    return (
        '.' in nombre_archivo
        and nombre_archivo.rsplit('.', 1)[1].lower() in current_app.config['EXTENSIONES_PERMITIDAS']
    )


def contenido_coincide_con_extension(archivo) -> bool:
    """
    True solo si los primeros bytes del archivo corresponden de verdad al
    formato que anuncia su extensión.

    Se exige coherencia (no basta con que el contenido sea "alguno de los
    permitidos"): el archivo se sirve después con el Content-Type derivado
    de su extensión, y con `X-Content-Type-Options: nosniff` activo en
    Nginx el navegador NO adivina -- un PNG guardado como .pdf
    simplemente no se vería. Mejor rechazarlo al subirlo, con un mensaje
    claro, que descubrirlo el día que alguien necesite el documento.

    Deja el puntero del archivo al inicio: si no se rebobina, el
    archivo.save() posterior guardaría el contenido truncado.
    """
    extension = archivo.filename.rsplit('.', 1)[1].lower() if '.' in archivo.filename else ''
    firmas = FIRMAS_POR_EXTENSION.get(extension)
    if not firmas:
        return False

    inicio = archivo.read(BYTES_DE_FIRMA)
    archivo.seek(0)

    return any(inicio.startswith(firma) for firma in firmas)


CARACTERES_FORMULA_EXCEL = ('=', '+', '-', '@')


def valor_seguro_excel(valor):
    """Antepone un apóstrofo si el texto podría leerse como fórmula en Excel/Sheets."""
    if isinstance(valor, str) and valor[:1] in CARACTERES_FORMULA_EXCEL:
        return "'" + valor
    return valor


# ---------------------------------------------------------------------------
# Límites para archivos .xlsx subidos (importaciones masivas)
# ---------------------------------------------------------------------------
# openpyxl carga el libro completo en memoria y iter_rows() fabrica una celda
# por cada hueco hasta la última fila/columna con algo: un .xlsx de 5 KB con
# una sola celda en la fila 100,000 consumía ~420 MB (en la fila 1,048,576,
# gigas). Además un .xlsx es un zip: 8 MB pueden descomprimirse a gigas.
MAX_FILAS_IMPORTACION = 2000        # ~30 s de importación con el timeout de 60 s de Gunicorn
MAX_COLUMNAS_IMPORTACION = 60
MAX_BYTES_DESCOMPRIMIDOS = 40 * 1024 * 1024


def error_de_tamano_xlsx(flujo):
    """Mensaje de error si el .xlsx (zip) se expandiría demasiado o no es un zip; None si está bien. Rebobina el flujo."""
    try:
        with zipfile.ZipFile(flujo) as z:
            total = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile:
        flujo.seek(0)
        return 'No se pudo leer el archivo. Verifica que sea un .xlsx válido generado con la plantilla.'
    flujo.seek(0)
    if total > MAX_BYTES_DESCOMPRIMIDOS:
        return (f'El archivo es demasiado grande una vez descomprimido (máximo '
                f'{MAX_BYTES_DESCOMPRIMIDOS // (1024 * 1024)} MB). Usa la plantilla y divide la carga en varios archivos.')
    return None


def error_de_dimensiones_hoja(ws):
    """Mensaje de error si la hoja tiene demasiadas filas/columnas (la fila 1 son encabezados); None si está bien."""
    if ws.max_row > MAX_FILAS_IMPORTACION + 1:
        return (f'El archivo tiene demasiadas filas (máximo {MAX_FILAS_IMPORTACION} por carga). '
                'Divide la carga en varios archivos o revisa que no haya celdas con datos muy abajo en la hoja.')
    if ws.max_column > MAX_COLUMNAS_IMPORTACION:
        return f'El archivo tiene demasiadas columnas (máximo {MAX_COLUMNAS_IMPORTACION}). Usa la plantilla sin agregar columnas.'
    return None
