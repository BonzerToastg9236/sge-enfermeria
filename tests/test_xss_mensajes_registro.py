"""
Pruebas anti-regresión de XSS en los mensajes flash de /registro
(hallazgo #5 de la auditoría).

CONTEXTO: registro.html es la ÚNICA vista pública sin login, y renderizaba
sus mensajes con {{ message|safe }} -- es decir, confiaba en HTML libre
para TODOS los mensajes, no solo para el de éxito que sí lo necesita
(lleva <strong> alrededor de la matrícula).

Hoy no era explotable: el único mensaje con dato de usuario (la CURP
repetida) solo se arma DESPUÉS de que la CURP pasó el regex
^[A-Z0-9]{18}$, así que no puede traer < > ni comillas. Pero bastaba con
que alguien agregara un flash(f'...{nombre_completo}...') -- texto libre
de verdad -- para convertirlo en XSS reflejado real.

La corrección quita |safe. El mensaje de éxito sigue mostrándose en
negritas porque es un objeto Markup, y Jinja2 renderiza sin escapar
cualquier objeto con __html__() aunque no se use |safe. Cualquier
mensaje que sea str normal ahora se escapa solo.
"""

from markupsafe import Markup

from tests.conftest import crear_plan
from tests.test_registro_publico import _datos_con_plan

from app import Alumno


PAYLOAD = '<script>alert("xss")</script>'


def _encolar_flash(client, mensaje, categoria='danger'):
    """
    Mete un mensaje en la cola de flashes de la sesión, tal como lo haría
    flash() dentro de una vista. Se usa para poder renderizar CUALQUIER
    mensaje en la plantilla real, incluidos los hostiles que hoy ninguna
    vista genera (justo lo que queremos que siga siendo imposible).
    """
    with client.session_transaction() as sesion:
        sesion['_flashes'] = [(categoria, mensaje)]


def test_un_mensaje_de_texto_con_html_se_escapa_en_registro(client, app):
    """El caso que el |safe dejaba pasar: un str normal NO debe ejecutarse."""
    _encolar_flash(client, PAYLOAD)

    respuesta = client.get('/registro')

    assert respuesta.status_code == 200
    assert PAYLOAD.encode('utf-8') not in respuesta.data, 'El HTML del mensaje se renderizó crudo: XSS'
    assert b'&lt;script&gt;' in respuesta.data, 'El mensaje debería aparecer escapado, no desaparecer'


def test_el_mensaje_de_exito_conserva_sus_negritas(client, app):
    """
    Regresión inversa: quitar |safe NO debe convertir en texto el HTML
    legítimo del mensaje de éxito (Markup), que es el único que lo usa.
    """
    plan = crear_plan()

    respuesta = client.post('/registro', data=_datos_con_plan(plan.id), follow_redirects=True)

    alumno = Alumno.query.filter_by(curp='LORF050101MDFXYZ09').first()
    assert alumno is not None
    assert b'<strong>' in respuesta.data, 'El mensaje de exito perdio su formato'
    assert alumno.matricula_id.encode('utf-8') in respuesta.data
    assert b'&lt;strong&gt;' not in respuesta.data, 'El Markup legitimo se escapo por error'


def test_un_markup_con_dato_de_usuario_escapado_sigue_siendo_seguro(client, app):
    """
    El patrón que usa la vista de éxito: HTML fijo + escape() del dato
    variable. El HTML fijo se conserva y el dato hostil queda neutralizado.
    """
    from markupsafe import escape

    _encolar_flash(client, Markup(f'Registro de <strong>{escape(PAYLOAD)}</strong>'), 'success')

    respuesta = client.get('/registro')

    assert b'<strong>' in respuesta.data
    assert PAYLOAD.encode('utf-8') not in respuesta.data


def test_los_errores_normales_del_formulario_se_siguen_viendo(client, app):
    """El texto de los mensajes de error no debe alterarse por el cambio."""
    plan = crear_plan()

    respuesta = client.post(
        '/registro',
        data=_datos_con_plan(plan.id, curp='CORTA123'),
        follow_redirects=True
    )

    assert respuesta.status_code == 400
    assert 'La CURP debe tener exactamente 18 caracteres'.encode('utf-8') in respuesta.data
