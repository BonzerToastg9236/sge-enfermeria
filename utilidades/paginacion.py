"""
Paginación en memoria para listas que ya no se pueden ordenar en SQL
(ej. cartera vencida, ordenada por saldo pendiente, un valor calculado).
"""

# PERFORMANCE-NOTE: con ~800 alumnos, el filtro "Activos" del buscador
# generaba una página de MÁS DE 1 MB de HTML. El cuello de botella no era
# SQL, era el tamaño de la respuesta -- de ahí la paginación.
def pagina_valida(valor) -> int:
    """Acota ?page= a un rango sensato: un entero gigantesco desbordaba el OFFSET de la consulta (500)."""
    return min(max(valor or 1, 1), 100_000)


ALUMNOS_POR_PAGINA = 24   # 24 = 8 filas de 3 tarjetas en pantalla grande
CARGOS_POR_PAGINA = 50


def _paginar_lista(items, page, per_page):
    """
    Pagina una lista que YA está en memoria.

    Se usa donde el orden final no se puede resolver en SQL. Caso concreto:
    la cartera vencida se ordena por saldo pendiente, que es un valor
    calculado (monto + recargo - pagos), no una columna de la tabla.

    Devuelve (items_de_esta_pagina, info_paginacion). El diccionario de
    info expone las mismas llaves que el objeto Pagination de
    Flask-SQLAlchemy (page, pages, total, has_prev, has_next, prev_num,
    next_num), para que las plantillas usen SIEMPRE la misma sintaxis sin
    importar de cuál de los dos venga.
    """
    total = len(items)
    total_paginas = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), total_paginas)  # una página fuera de rango no truena
    inicio = (page - 1) * per_page

    return items[inicio:inicio + per_page], {
        'page': page,
        'per_page': per_page,
        'total': total,
        'pages': total_paginas,
        'has_prev': page > 1,
        'has_next': page < total_paginas,
        'prev_num': page - 1,
        'next_num': page + 1,
    }
