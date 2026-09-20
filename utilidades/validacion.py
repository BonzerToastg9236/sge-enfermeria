"""Validaciones de entrada compartidas."""


def errores_de_longitud(modelo, valores):
    """
    Lista de mensajes para cada valor de texto más largo que su columna.

    SQLite no hace cumplir el largo de un VARCHAR(n); PostgreSQL sí y responde
    con DataError (500). Los formularios que capturan texto libre deben
    revisarlo ANTES de guardar para poder devolver el mensaje junto al campo.
    `valores` mapea nombre de columna -> valor capturado.
    """
    columnas = modelo.__table__.c
    errores = []
    for nombre, valor in valores.items():
        columna = columnas.get(nombre)
        maximo = getattr(getattr(columna, 'type', None), 'length', None)
        if maximo and valor is not None and len(str(valor)) > maximo:
            errores.append(f'El campo {nombre} excede {maximo} caracteres.')
    return errores
