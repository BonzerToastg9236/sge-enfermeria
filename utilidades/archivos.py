"""
Validación de archivos subidos (expediente del alumno): extensión
permitida por configuración, y firma real (magic bytes) del contenido
(fix #7 -- la extensión sola no prueba nada, ver docstring movido).
"""

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
