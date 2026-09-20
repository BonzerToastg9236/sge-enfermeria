"""
Auditoría 2026-09-19 (autenticación):
  * una cookie de sesión robada seguía válida tras cambiar la contraseña;
  * el login tardaba 145 ms si el usuario existía y 2.5 ms si no (enumeración);
  * solo había límite por IP (5/min): repartiendo intentos entre muchas IPs se
    podía adivinar la clave de UNA cuenta sin freno;
  * la política de contraseñas era "8 caracteres" (dos DIRECTIVO de la BD de
    desarrollo usaban una clave trivial).
"""

import statistics
import time

import pytest

from tests.conftest import crear_usuario, login
from app import db, limiter, Usuario, RolUsuario
from utilidades.seguridad import validar_password


# ---------------------------------------------------------------------------
# Política de contraseñas
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('clave', [
    '12345678', '1234567890', '0000000000', 'aaaaaaaaaa', 'abcdefghij',
    'password12', 'contrasena1', 'qwertyuiop', 'Enfermeria1',   # lista de comunes / patrones
    'corta1A!',                                                   # < 10
    '98765432101',                                                # solo dígitos
])
def test_validar_password_rechaza_claves_debiles(clave):
    assert validar_password(clave, username='maria') is not None


def test_validar_password_rechaza_una_clave_que_contiene_el_usuario():
    assert validar_password('mariagarcia2026', username='mariagarcia') is not None
    assert validar_password('xx-MARIAGARCIA-xx-77', username='mariagarcia') is not None


@pytest.mark.parametrize('clave', ['Turno-Vespertino#41', 'mi caja abre a las 8am', 'cuatro-cafes-y-un-pan'])
def test_validar_password_acepta_claves_razonables(clave):
    assert validar_password(clave, username='maria') is None


def test_crear_usuario_por_la_ruta_aplica_la_politica(client, app):
    crear_usuario(username='dir1'); login(client, 'dir1', 'clave12345')
    client.post('/usuarios/nuevo', data={'nombre_completo': 'Ana Lopez', 'username': 'ana.lopez', 'rol': 'CONTADOR',
                                         'password': '12345678', 'confirmar_password': '12345678'})
    assert Usuario.query.filter_by(username='ana.lopez').count() == 0
    client.post('/usuarios/nuevo', data={'nombre_completo': 'Ana Lopez', 'username': 'ana.lopez', 'rol': 'CONTADOR',
                                         'password': 'Turno-Vespertino#41', 'confirmar_password': 'Turno-Vespertino#41'})
    assert Usuario.query.filter_by(username='ana.lopez').count() == 1


def test_cambiar_clave_en_el_perfil_aplica_la_politica(client, app):
    crear_usuario(username='dir1'); login(client, 'dir1', 'clave12345')
    client.post('/perfil', data={'nombre_completo': 'Dir Uno', 'password_actual': 'clave12345',
                                 'password_nueva': '12345678', 'password_confirmar': '12345678'})
    assert Usuario.query.filter_by(username='dir1').one().check_password('clave12345')


# ---------------------------------------------------------------------------
# Sesiones
# ---------------------------------------------------------------------------

def _get(cliente, url):
    """La fixture `app` mantiene UN app_context abierto y Flask-Login cachea current_user en `g`:
    sin un contexto propio por petición, la 2ª petición reutiliza al usuario de la 1ª."""
    with cliente.application.app_context():
        return cliente.get(url)


def _copia_de_cookie(client):
    """Otro navegador con la MISMA cookie de sesión (la 'robada')."""
    ladron = client.application.test_client()
    ladron.set_cookie('session', client.get_cookie('session').value)
    return ladron


def test_una_cookie_robada_deja_de_servir_al_cambiar_la_contrasena(client, app):
    crear_usuario(username='victima', rol=RolUsuario.CONTADOR); login(client, 'victima', 'clave12345')
    ladron = _copia_de_cookie(client)
    assert _get(ladron, '/reportes/cartera-vencida').status_code == 200

    client.post('/perfil', data={'nombre_completo': 'Victima', 'password_actual': 'clave12345',
                                 'password_nueva': 'Turno-Vespertino#41', 'password_confirmar': 'Turno-Vespertino#41'})

    assert _get(ladron, '/reportes/cartera-vencida').status_code == 302   # a /login
    # ...y la persona legítima sigue dentro (se le renueva la sesión)
    assert _get(client, '/reportes/cartera-vencida').status_code == 200


def test_una_sesion_del_formato_anterior_sin_huella_no_se_acepta(client, app):
    usuario = crear_usuario(username='victima', rol=RolUsuario.CONTADOR)
    with client.session_transaction() as s:
        s['_user_id'] = str(usuario.id)          # formato viejo: solo el id
        s['_fresh'] = True
    assert client.get('/reportes/cartera-vencida').status_code == 302


# ---------------------------------------------------------------------------
# Login: enumeración por tiempo y fuerza bruta contra una cuenta
# ---------------------------------------------------------------------------

def _mediana_ms(client, username, n=5):
    tiempos = []
    for _ in range(n):
        limiter.reset()
        t = time.perf_counter()
        client.post('/login', data={'username': username, 'password': 'incorrecta-12345'})
        tiempos.append(time.perf_counter() - t)
    return statistics.median(tiempos) * 1000


def test_el_login_tarda_parecido_exista_o_no_el_usuario(client, app):
    crear_usuario(username='existe')
    existente = _mediana_ms(client, 'existe')
    inexistente = _mediana_ms(client, 'no-existe-zzz')
    assert inexistente > existente * 0.4, f'{inexistente:.1f} ms (no existe) vs {existente:.1f} ms (existe)'


def _intentos_fallidos(client, username, cuantos):
    for i in range(cuantos):
        client.post('/login', data={'username': username, 'password': f'mala-{i}-xxxxx'},
                    environ_base={'REMOTE_ADDR': f'10.1.{i // 250}.{i % 250 + 1}'})   # cada intento desde otra IP


def test_muchos_fallos_contra_una_cuenta_desde_ips_distintas_la_bloquean_temporalmente(client, app):
    crear_usuario(username='blanco')
    _intentos_fallidos(client, 'blanco', 12)
    r = client.post('/login', data={'username': 'blanco', 'password': 'clave12345'}, environ_base={'REMOTE_ADDR': '10.9.9.9'})
    assert r.status_code == 302 and '/login' in r.headers['Location']
    assert client.get('/reportes/cartera-vencida').status_code == 302   # no quedó con sesión


def test_los_fallos_de_una_cuenta_no_bloquean_a_las_demas(client, app):
    crear_usuario(username='blanco'); crear_usuario(username='otra')
    _intentos_fallidos(client, 'blanco', 12)
    r = client.post('/login', data={'username': 'otra', 'password': 'clave12345'}, environ_base={'REMOTE_ADDR': '10.9.9.8'})
    assert r.status_code == 302 and '/login' not in r.headers['Location']


def test_los_inicios_de_sesion_correctos_no_cuentan_para_el_bloqueo(client, app):
    crear_usuario(username='ocupada')
    for i in range(15):
        c = app.test_client()
        r = c.post('/login', data={'username': 'ocupada', 'password': 'clave12345'}, environ_base={'REMOTE_ADDR': f'10.2.0.{i + 1}'})
        assert r.status_code == 302 and '/login' not in r.headers['Location'], f'falló el intento {i}'
