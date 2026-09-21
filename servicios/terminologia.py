"""
Terminología configurable de la institución para MENSAJES y textos generados en Python.

Cada institución llama distinto a sus periodos ("Cuatrimestre", "Semestre", "Grado"...) y a su programa
("Carrera", "Licenciatura", "Nivel Educativo"...). Las plantillas leen `config_institucion`; los mensajes
que se arman en código (flash, correos, Excel) usan terminos(). Solo LEE (sin commit) y no usa artículos con
el periodo (no se conoce su género gramatical): se escribe "en 3° trimestre", no "en el 3° trimestre".
El género del programa SÍ se configura, para "la carrera" / "el programa", "seleccionada" / "seleccionado".
"""

from types import SimpleNamespace

from modelos import ConfiguracionInstitucion

NOMBRE_INSTITUCION_POR_DEFECTO = ConfiguracionInstitucion.NOMBRE_POR_DEFECTO


def terminos():
    cfg = ConfiguracionInstitucion.query.first()
    if cfg is None:
        institucion, periodo, periodos, programa, programas, femenino = NOMBRE_INSTITUCION_POR_DEFECTO, 'Cuatrimestre', 'Cuatrimestres', 'Carrera', 'Carreras', True
    else:
        institucion, periodo, periodos = cfg.nombre_institucion, cfg.nombre_periodo_singular, cfg.nombre_periodo_plural
        programa, programas, femenino = cfg.nombre_programa_singular, cfg.nombre_programa_plural, cfg.programa_es_femenino
    return SimpleNamespace(
        institucion=institucion,
        periodo=periodo, periodos=periodos, programa=programa, programas=programas,
        periodo_l=periodo.lower(), periodos_l=periodos.lower(), programa_l=programa.lower(), programas_l=programas.lower(),
        art='la' if femenino else 'el', un='una' if femenino else 'un', a_o='a' if femenino else 'o',
    )
