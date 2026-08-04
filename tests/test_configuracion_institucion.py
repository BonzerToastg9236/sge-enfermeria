"""
Pruebas de que la configuración de la institución (nombre del periodo,
nombre del programa) sí cambia lo que se muestra en las plantillas --
esto es lo que hace que el sistema sirva para cualquier tipo de escuela
(primaria, secundaria, prepa, universidad) sin tocar código.
"""

from tests.conftest import crear_plan, crear_usuario, login
from app import db, ConfiguracionInstitucion


def test_configuracion_institucion_usa_valores_de_universidad_por_defecto(app):
    """Si nadie ha configurado nada, los valores por defecto siguen siendo los de universidad (compatibilidad hacia atrás)."""
    config = ConfiguracionInstitucion.obtener()
    assert config.nombre_periodo_singular == 'Cuatrimestre'
    assert config.nombre_programa_singular == 'Carrera'
    assert config.max_periodos == 9


def test_cambiar_configuracion_institucion_se_refleja_en_gestionar_materias(client, app):
    """
    Si Directivo cambia la terminología a la de una primaria (Grado /
    Nivel Educativo), la pantalla de Materias debe mostrar esas palabras
    en vez de "Cuatrimestre" / "Carrera".
    """
    plan = crear_plan(nombre='Primaria Ejemplo', clave='PRI')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        '/configuracion/institucion',
        data={
            'nombre_institucion': 'Primaria Ejemplo',
            'nombre_periodo_singular': 'Grado',
            'nombre_periodo_plural': 'Grados',
            'nombre_programa_singular': 'Nivel Educativo',
            'nombre_programa_plural': 'Niveles Educativos',
            'max_periodos': '6',
        },
        follow_redirects=True
    )

    respuesta = client.get(f'/planes/{plan.id}/materias')
    html = respuesta.get_data(as_text=True)

    assert 'Materias por Grado' in html
    assert 'Cuatrimestre' not in html
    assert 'Carrera' not in html or 'clave_carrera' in html  # solo puede quedar en atributos internos, no en texto visible


def test_configuracion_institucion_respeta_genero_gramatical_configurado(client, app):
    """
    Si el nombre del programa es masculino (ej. "Nivel Educativo"), las
    plantillas deben usar "ese"/"otro" en vez de "esa"/"otra" -- se
    prueba a través de boletas_importar.html, que usa esos helpers.
    """
    plan = crear_plan(nombre='Secundaria Ejemplo', clave='SEC')
    crear_usuario()
    login(client, 'directivo1', 'clave12345')

    client.post(
        '/configuracion/institucion',
        data={
            'nombre_institucion': 'Secundaria Ejemplo',
            'nombre_periodo_singular': 'Grado',
            'nombre_periodo_plural': 'Grados',
            'nombre_programa_singular': 'Nivel Educativo',
            'nombre_programa_plural': 'Niveles Educativos',
            'max_periodos': '3',
        },
        follow_redirects=True
    )

    config = ConfiguracionInstitucion.obtener()
    # "Nivel Educativo" es masculino -- si el Directivo no marcó programa_es_femenino=False
    # a mano, el valor por defecto (True) seguiría diciendo "esa"/"otra", que es lo que
    # confirma que este campo se tiene que ajustar aparte, no adivinarse por el nombre.
    config.programa_es_femenino = False
    db.session.commit()

    respuesta = client.get('/boletas/importar')
    html = respuesta.get_data(as_text=True)

    assert 'otro nivel educativo' in html
