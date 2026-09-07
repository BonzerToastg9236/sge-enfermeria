"""
Pruebas del rate limiting de /registro (hallazgo #4 de la auditoría).

/registro es la ÚNICA ruta pública sin login que escribe en la base de
datos (INSERT en alumnos). Sin límite, cualquiera puede automatizar el
formulario y llenar la tabla de aspirantes con basura -- el token CSRF no
lo impide (se obtiene con un GET previo y se reutiliza en un bucle).

Se reutiliza el MISMO mecanismo que ya usa /login (Flask-Limiter, con el
storage compartido de Redis en producción); no se introduce nada nuevo.

NOTA: el fixture `app` (tests/conftest.py) llama a limiter.reset() antes
de cada prueba, así que los intentos nunca se arrastran de una prueba a
otra.
"""

from tests.conftest import crear_plan
from tests.test_registro_publico import _datos_con_plan

from app import Alumno


# El límite por minuto configurado en la ruta. Se prueba éste (no el de
# por hora) porque es el que se puede disparar de forma determinista sin
# depender del reloj.
LIMITE_POR_MINUTO = 5


def test_registro_normal_sigue_funcionando_con_el_limite_activo(client, app):
    """Un aspirante legítimo (una sola solicitud) no debe verse afectado."""
    plan = crear_plan()

    respuesta = client.post('/registro', data=_datos_con_plan(plan.id), follow_redirects=True)

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 1


def test_registro_bloquea_solicitudes_excesivas_desde_la_misma_ip(client, app):
    """
    Pasado el límite por minuto, la siguiente solicitud debe rechazarse
    ANTES de tocar la base de datos (Flask-Limiter corre antes de la vista).
    """
    plan = crear_plan()

    for i in range(LIMITE_POR_MINUTO):
        client.post(
            '/registro',
            data=_datos_con_plan(plan.id, curp=f'LORF05010{i}MDFXYZ09'),
            follow_redirects=True
        )

    alumnos_antes = Alumno.query.count()

    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, curp='LORF050199MDFXYZ09'),
        follow_redirects=True
    )

    assert Alumno.query.count() == alumnos_antes, 'La solicitud bloqueada NO debió crear ningún alumno'
    assert 'demasiadas solicitudes'.encode('utf-8') in respuesta.data.lower()


def test_el_bloqueo_de_registro_no_manda_al_usuario_a_la_pantalla_de_login(client, app):
    """
    El aspirante bloqueado es una persona SIN cuenta: mandarlo a /login con
    el mensaje de "intentos de inicio de sesión" (el handler 429 original,
    cableado al login) sería desconcertante. Debe regresar a /registro.
    """
    plan = crear_plan()

    for i in range(LIMITE_POR_MINUTO):
        client.post(
            '/registro',
            data=_datos_con_plan(plan.id, curp=f'LORF05010{i}MDFXYZ09'),
            follow_redirects=True
        )

    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, curp='LORF050199MDFXYZ09'),
        follow_redirects=False
    )

    assert respuesta.status_code == 302
    assert '/registro' in respuesta.headers['Location']
    assert '/login' not in respuesta.headers['Location']


def test_el_limite_de_login_sigue_intacto(client, app):
    """
    Regresión: el rate limiting de /login (5 por minuto) NO debe verse
    afectado por el nuevo límite de /registro, y su 429 debe seguir
    llevando a la pantalla de login con su mensaje propio.
    """
    for _ in range(5):
        client.post('/login', data={'username': 'nadie', 'password': 'x'}, follow_redirects=True)

    respuesta = client.post(
        '/login',
        data={'username': 'nadie', 'password': 'x'},
        follow_redirects=False
    )

    assert respuesta.status_code == 302
    assert '/login' in respuesta.headers['Location']


def test_ver_el_formulario_de_registro_no_consume_el_limite(client, app):
    """
    El límite se aplica solo al POST (igual que en /login): abrir la página
    varias veces para leerla no debe agotar los intentos de nadie.
    """
    plan = crear_plan()

    for _ in range(LIMITE_POR_MINUTO + 3):
        respuesta_get = client.get('/registro')
        assert respuesta_get.status_code == 200

    respuesta = client.post('/registro', data=_datos_con_plan(plan.id), follow_redirects=True)

    assert respuesta.status_code == 200
    assert Alumno.query.count() == 1
