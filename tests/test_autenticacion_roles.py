"""
Pruebas de autenticación (Flask-Login) y control de acceso por rol.
Regla de negocio: Administrativo puede consultar/actualizar; SOLO
Directivo puede borrar documentos y gestionar usuarios.
"""

from tests.conftest import crear_plan, crear_alumno, crear_usuario, login
from app import RolUsuario


def test_ruta_protegida_redirige_a_login_si_no_hay_sesion(client, app):
    respuesta = client.get('/', follow_redirects=False)

    assert respuesta.status_code == 302
    assert '/login' in respuesta.headers['Location']


def test_login_con_credenciales_correctas_permite_entrar(client, app):
    crear_usuario(username='ana', password='clave12345')

    respuesta = login(client, 'ana', 'clave12345')

    assert respuesta.status_code == 200
    assert b'Control Escolar' in respuesta.data


def test_login_con_password_incorrecto_no_permite_entrar(client, app):
    crear_usuario(username='ana', password='clave12345')

    respuesta = login(client, 'ana', 'password-incorrecto')

    assert respuesta.status_code == 200

    # No debe haber quedado autenticado: la ruta protegida sigue rechazando
    respuesta_index = client.get('/', follow_redirects=False)
    assert respuesta_index.status_code == 302


def test_login_con_usuario_desactivado_no_permite_entrar(client, app):
    usuario = crear_usuario(username='ana', password='clave12345')
    usuario.activo = False
    from app import db
    db.session.commit()

    login(client, 'ana', 'clave12345')

    respuesta_index = client.get('/', follow_redirects=False)
    assert respuesta_index.status_code == 302  # sigue sin poder entrar


def test_administrativo_no_puede_ver_listado_de_usuarios(client, app):
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    respuesta = client.get('/usuarios', follow_redirects=True)

    assert respuesta.status_code == 200
    assert 'permisos'.encode('utf-8') in respuesta.data.lower()


def test_directivo_si_puede_ver_listado_de_usuarios(client, app):
    crear_usuario(username='dir1', password='clave12345', rol=RolUsuario.DIRECTIVO)
    login(client, 'dir1', 'clave12345')

    respuesta = client.get('/usuarios', follow_redirects=True)

    assert respuesta.status_code == 200
    assert 'Administrativos'.encode('utf-8') in respuesta.data


def test_administrativo_no_puede_eliminar_documentos(client, app):
    """
    Regla de negocio explícita: el Administrativo solo consulta/actualiza,
    NUNCA borra. Aunque mande la petición POST directamente al endpoint.
    """
    plan = crear_plan()
    crear_alumno(plan)
    crear_usuario(username='admin1', password='clave12345', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave12345')

    # Ni siquiera necesitamos que el documento exista de verdad: la ruta
    # debe rechazar por ROL antes de llegar a buscar el documento en la BD.
    respuesta = client.post('/documento/1/eliminar', follow_redirects=True)

    assert respuesta.status_code == 200
    assert 'permisos'.encode('utf-8') in respuesta.data.lower()


def test_administrativo_no_puede_desactivar_cuentas(client, app):
    directivo = crear_usuario(username='dir1', password='clave12345', rol=RolUsuario.DIRECTIVO)
    crear_usuario(username='admin1', password='clave99999', rol=RolUsuario.ADMINISTRATIVO)
    login(client, 'admin1', 'clave99999')

    respuesta = client.post(f'/usuarios/{directivo.id}/toggle', follow_redirects=True)

    assert respuesta.status_code == 200
    assert 'permisos'.encode('utf-8') in respuesta.data.lower()

    from app import Usuario
    assert Usuario.query.get(directivo.id).activo is True  # sigue activo, no se tocó
