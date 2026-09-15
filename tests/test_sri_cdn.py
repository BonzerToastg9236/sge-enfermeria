"""
Verifica que todo recurso cargado desde un CDN externo (jsDelivr) en las
plantillas lleve Subresource Integrity (hallazgo #5.5 de la auditoría,
plan de remediación paso 5.5). Sin esto, un CDN comprometido podría
servir JS/CSS distinto al que la app espera sin que nadie lo note.

También congela el path de chart.js: jsDelivr advierte explícitamente
que chart.umd.min.js es una minificación DINÁMICA ("Skipped
minification because the original file appears to be already
minified... Do NOT use SRI with dynamically generated files") -- el
archivo elegible para SRI es el estático chart.umd.js.
"""

import glob
import re

ETIQUETA_CDN_REGEX = re.compile(
    r'<(?:script|link)\b[^>]*(?:src|href)="https://cdn\.jsdelivr\.net/[^"]+"[^>]*>',
    re.IGNORECASE,
)


def _etiquetas_cdn_por_archivo():
    resultado = {}
    for path in sorted(glob.glob('templates/*.html')):
        with open(path, encoding='utf-8') as f:
            contenido = f.read()
        etiquetas = ETIQUETA_CDN_REGEX.findall(contenido)
        if etiquetas:
            resultado[path] = etiquetas
    return resultado


def test_hay_al_menos_una_plantilla_usando_el_cdn():
    """Precondición de esta prueba: si esto da 0, la regex de arriba dejó de funcionar."""
    total = sum(len(v) for v in _etiquetas_cdn_por_archivo().values())
    assert total > 0


def test_todo_recurso_de_jsdelivr_lleva_integrity_y_crossorigin():
    faltantes = []
    for path, etiquetas in _etiquetas_cdn_por_archivo().items():
        for etiqueta in etiquetas:
            if 'integrity="sha' not in etiqueta or 'crossorigin=' not in etiqueta:
                faltantes.append((path, etiqueta))

    assert not faltantes, (
        'Etiquetas de CDN sin integrity/crossorigin:\n' +
        '\n'.join(f'{p}: {e}' for p, e in faltantes)
    )


def test_ninguna_plantilla_usa_la_minificacion_dinamica_de_chart_js():
    for path in sorted(glob.glob('templates/*.html')):
        with open(path, encoding='utf-8') as f:
            contenido = f.read()
        assert 'chart.umd.min.js' not in contenido, (
            f'{path}: chart.umd.min.js es una minificación dinámica de jsDelivr, '
            'no un archivo estático -- no es válido usar SRI con ella. Usa chart.umd.js.'
        )
