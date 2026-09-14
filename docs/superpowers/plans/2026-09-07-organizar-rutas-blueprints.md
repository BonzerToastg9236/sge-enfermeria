# Organizar rutas en blueprints — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `app.py` (5,028 lines, 51 routes) into 9 Flask blueprints plus supporting `modelos/`, `utilidades/` and `servicios/` packages, with zero behavior change and the full test suite green after every task.

**Architecture:** Layered, one-directional dependency chain: `config → extensiones → modelos → utilidades → servicios → rutas → app.py`. No module below `rutas/` ever imports from `app.py`; `app.py` becomes a thin assembler (`create_app()`, error handlers, context processor, blueprint registration, compatibility re-exports).

**Tech Stack:** Flask 3.0.3, Flask-SQLAlchemy 3.1.1, Flask-Migrate/Alembic, Flask-Login, Flask-WTF, Flask-Limiter, Flask-Mail, pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-organizar-rutas-blueprints-design.md`

## Global Constraints

- Do not change any public URL. Only endpoint *names* change (e.g. `login` → `auth.login`).
- Do not change business logic. Every moved function body is relocated verbatim; the only edits allowed are the mechanical substitutions catalogued in Appendix C (`app.config`/`app.logger` → `current_app.config`/`current_app.logger`, required only to avoid a circular import — behavior is identical because both resolve to the same active Flask app inside a request/app context).
- Do not modify existing data. No task touches `instance/sge_dev.db` or any real database.
- Do not undo fixes #1–#9 from the production audit (role guard on `ver_expediente`/`ficha_inscripcion`, the `with_for_update()` row lock in `registrar_pago`, the partial unique index on `Cargo`, the `/registro` rate limit, the endpoint-aware 429 handler, `MAIL_TIMEOUT`, magic-bytes file validation, the 413 handler, `MOTIVO_GENERICO_FALLO_CORREO`). Every task that touches one of these is flagged explicitly.
- None of the 166 existing tests are modified. If a task appears to require editing one, stop — that means the task broke compatibility; fix the task, not the test.
- `app.py` keeps re-exporting the 34 names listed in Appendix A, so `tests/`, `seed.py` and `crear_admin.py` keep working unmodified.
- Run the full test suite (`./venv/bin/python -m pytest -q`) after every task and confirm the count only grows (166 at Task 0 start, 169 from Task 0 onward (Task 0 adds 3 new test functions: 1 in test_integridad_urls.py + 2 in test_inventario_rutas.py)). A task is not done while any test is red.
- Every commit message ends with the required attribution footer (see repo convention already in use).
- **Every `app.py:X-Y` line reference in this plan is anchored to the frozen baseline snapshot described immediately below — never to the live, currently-mutating `app.py`.** Deleting lines in one step shifts every later line number in the file; without a frozen reference, later tasks' line numbers silently go stale.

## Line-number baseline (read this before Task 1)

Before starting Task 1 (Task 0 doesn't touch `app.py`, so this can wait
until just before Task 1's first step), take a frozen copy of `app.py`
as it stands right now — this is what every `app.py:X-Y` citation in
Tasks 1–13 means, regardless of how many lines earlier tasks have
already deleted from the real, live `app.py`:

```bash
cp app.py app.py.baseline-plan2
```

From then on, to pull a cited range, use the baseline, never the live
file:

```bash
sed -n 'X,Yp' app.py.baseline-plan2
```

Every "delete `app.py:X-Y`" instruction in this plan still means: delete
those lines **from the live, currently-mutating `app.py`** — but find
*which* lines by content (the function/class signature named in the
instruction, e.g. `grep -n "^class RolUsuario"`), not by trusting that
line X in the live file today is still the same line X the baseline had.
The baseline is only for *reading* (extracting the verbatim content to
paste elsewhere); deletions always target the live file by matching the
actual code, confirmed by eye before deleting.

Delete `app.py.baseline-plan2` in Task 14 (never commit it — add it to
the `git status --short` check in Task 14 Step 7 to confirm it's gone
before the final commit).

---

## Appendix A — the 34 re-exports (verbatim, for Task 14)

```
app, db, limiter, mail,
Alumno, Calificacion, Cargo, ConceptoCobro, ConfiguracionCobros,
ConfiguracionInstitucion, ContadorFolio, DocumentoAlumno,
HistorialCalificacion, InscripcionMateria, Materia, Pago, PlanEstudio,
Usuario,
EstatusAlumno, EstatusCargo, MetodoPago, RolUsuario, TipoRecargo,
DIAS_AVISO_VENCIMIENTO,
a_local, ahora_utc, hoy_local, periodo_escolar_actual,
rango_utc_del_dia, siguiente_folio, generar_matricula,
crear_alumno_generando_matricula, _vencimiento_dia_10_sugerido,
_calcular_reporte_cobros_del_dia
```

## Appendix B — endpoint rename rule (used by every `rutas/*` task)

Flask sets a blueprint route's endpoint to `<blueprint_name>.<function_name>`
automatically, with no other change needed to the function itself. So for
every endpoint `E` moved into blueprint `B` in a task, the rename is
always the same mechanical operation:

```bash
grep -rl "url_for('E'" app.py templates/ | xargs sed -i "s/url_for('E'/url_for('B.E'/g"
```

This is safe (not a broad string replace) because `url_for('E'` — the
literal substring including the opening quote and the trailing quote
right after `E` — only ever matches a call to endpoint `E`. It does not
match `url_for('E_algo'` or `url_for('algo_E'`. Verify after each
replacement with:

```bash
grep -rn "url_for('E'" app.py templates/   # must return nothing
```

The full old → new table (grouped by blueprint, in the order tasks
execute them) is given at the top of each `rutas/*` task below.

## Appendix C — `current_app` substitutions (exactly 7 sites, verified by grep)

Moving code out of `app.py` means the global name `app` is no longer in
scope (importing it back would create the exact circular import this
whole plan exists to avoid). Every one of the 7 places in the file that
references the global `app` object from *outside* `create_app()`/the
error handlers needs `from flask import current_app` and `app.` →
`current_app.`. Behavior is identical: inside a request or app context,
`current_app` resolves to the same object as the module-level `app`.

| # | Current line | Moves to | Change |
|---|---|---|---|
| 1 | `app.py:55` (`_zona_horaria`) | `utilidades/fechas.py` | `app.config.get(...)` → `current_app.config.get(...)` |
| 2 | `app.py:2367` (`extension_permitida`) | `utilidades/archivos.py` | `app.config['EXTENSIONES_PERMITIDAS']` → `current_app.config['EXTENSIONES_PERMITIDAS']` |
| 3 | `app.py:2451` (`documentos()` view) | `rutas/documentos.py` | `app.config['UPLOAD_FOLDER']` → `current_app.config['UPLOAD_FOLDER']` |
| 4 | `app.py:2541` (`ver_documento()` view) | `rutas/documentos.py` | same |
| 5 | `app.py:2566` (`eliminar_documento()` view) | `rutas/documentos.py` | same |
| 6 | `app.py:3297` (`enviar_comprobante_pago`) | `servicios/correo.py` | `app.logger.warning(...)` → `current_app.logger.warning(...)` |
| 7 | `app.py:3330` (`enviar_recordatorio_vencimiento`) | `servicios/correo.py` | same |

No other moved function references the global `app` object (verified
with `grep -n "\bapp\." app.py` against the full file and manually
excluding the lines that stay in `create_app()`/error handlers).

---

## Task 0: Safety-net tests (write against today's `app.py`, before any move)

**Files:**
- Create: `tests/test_integridad_urls.py`
- Create: `tests/test_inventario_rutas.py`

**Interfaces:**
- Consumes: `app.url_map` (from `app.py`, unchanged interface throughout the whole plan — `app.url_map.iter_rules()` and `app.view_functions` work identically whether routes are registered directly or via blueprints).
- Produces: two standing regression tests that stay in the suite through every later task. `test_integridad_urls.py` is never edited again. `test_inventario_rutas.py` gets exactly one endpoint-name edit per later blueprint task (documented in that task).

- [ ] **Step 1: Write `tests/test_integridad_urls.py`**

```python
"""
Red de seguridad para la reorganización de rutas en blueprints (Plan 2,
docs/superpowers/specs/2026-09-07-organizar-rutas-blueprints-design.md).

Escanea cada plantilla en busca de url_for('endpoint', ...) y confirma que
ese endpoint existe de verdad en app.url_map. Sin esto, un url_for con el
nombre viejo de un endpoint renombrado NO falla al arrancar la app: falla
hasta que alguien abre esa pantalla en concreto -- que puede ser una
pantalla que ninguna de las 166 pruebas visita.

LIMITACIÓN CONOCIDA: solo detecta url_for('literal'), no
url_for(variable). Verificado en las 31 plantillas actuales: las 98
llamadas a url_for() son todas de la forma url_for('endpoint', ...) con
comillas simples y en una sola línea -- no hay ningún caso dinámico hoy.
Si алgún día aparece uno, esta prueba simplemente no lo cubre; no lo
reporta como error.
"""

import re
from pathlib import Path

from app import app


PATRON_URL_FOR = re.compile(r"url_for\(\s*'([a-zA-Z_][a-zA-Z0-9_.]*)'")

DIRECTORIO_PLANTILLAS = Path(__file__).resolve().parent.parent / 'templates'


def _endpoints_referenciados_por_plantilla():
    """
    Devuelve una lista de (ruta_relativa, numero_de_linea, endpoint) por
    cada url_for('...') encontrado en cada archivo .html bajo templates/.
    """
    referencias = []
    for plantilla in sorted(DIRECTORIO_PLANTILLAS.rglob('*.html')):
        texto = plantilla.read_text(encoding='utf-8')
        ruta_relativa = plantilla.relative_to(DIRECTORIO_PLANTILLAS)
        for numero_linea, linea in enumerate(texto.splitlines(), start=1):
            for coincidencia in PATRON_URL_FOR.finditer(linea):
                referencias.append((str(ruta_relativa), numero_linea, coincidencia.group(1)))
    return referencias


def test_todas_las_plantillas_referencian_endpoints_que_existen(app):
    referencias = _endpoints_referenciados_por_plantilla()
    assert referencias, 'No se encontró ningún url_for() en las plantillas -- revisa la ruta de escaneo'

    endpoints_validos = {regla.endpoint for regla in app.url_map.iter_rules()}

    rotos = [
        f'{plantilla}:{linea} -> url_for(\'{endpoint}\')'
        for plantilla, linea, endpoint in referencias
        if endpoint not in endpoints_validos
    ]

    assert not rotos, (
        'Estas plantillas referencian endpoints que ya no existen '
        '(url_for roto):\n' + '\n'.join(rotos)
    )
```

- [ ] **Step 2: Run it against the current, unmodified `app.py`**

Run: `./venv/bin/python -m pytest tests/test_integridad_urls.py -v`
Expected: **PASS** (1 test). This is deliberate — the test is a standing
auditor, not a red/green TDD test for new behavior. It must pass today,
and it must keep passing after every future task; that's what makes a
broken rename visible immediately instead of silently.

- [ ] **Step 3: Write `tests/test_inventario_rutas.py`**

```python
"""
Segunda parte de la red de seguridad de Plan 2 (ver
test_integridad_urls.py). Fija el inventario completo de las 51 rutas de
hoy: URL, métodos HTTP y roles exigidos por rol_requerido(). Si un paso de
la migración cambia por accidente la URL pública, agrega o quita un
método, o afloja/endurece el control de acceso de una ruta, esta prueba
lo detecta.

Esta tabla se generó leyendo app.url_map en vivo (no a mano), así que es
un reflejo exacto del comportamiento real, no de lo que alguien recuerda
que debería ser.

MANTENIMIENTO DURANTE PLAN 2: cuando una ruta se mueve a un blueprint, su
endpoint cambia de 'x' a 'blueprint.x' (Flask lo hace automático). Cada
tarea que mueve rutas actualiza en este archivo -ÚNICAMENTE- el campo de
endpoint de las filas que movió, nunca la URL, los métodos ni los roles.
Eso es lo que la prueba está diseñada para permitir: rastrear el
renombrado intencional, no ocultar un cambio de comportamiento.
"""

from app import app


def _roles_de_vista(func):
    """
    rol_requerido() envuelve a la vista con @wraps + @login_required, así
    que la tupla de roles no vive en el primer nivel de closure -- hay que
    recorrer los closures anidados hasta encontrarla. Devuelve None si la
    vista no usa rol_requerido (pública, o solo @login_required).
    """
    visitados = set()
    pendientes = [func]
    while pendientes:
        actual = pendientes.pop()
        if id(actual) in visitados or not hasattr(actual, '__closure__'):
            continue
        visitados.add(id(actual))
        cierre = actual.__closure__
        if not cierre:
            continue
        for nombre, celda in zip(actual.__code__.co_freevars, cierre):
            try:
                valor = celda.cell_contents
            except ValueError:
                continue
            if nombre == 'roles_permitidos' and isinstance(valor, tuple):
                return valor
            if callable(valor):
                pendientes.append(valor)
    return None


# (URL, métodos, endpoint, roles) -- generado desde app.url_map el
# 2026-09-07, antes de mover cualquier ruta. `None` en roles significa
# "sin rol_requerido" (público o solo @login_required).
INVENTARIO_ESPERADO = [
    ('/', ('GET',), 'index', None),
    ('/alumno/<matricula>/avanzar-cuatrimestre', ('POST',), 'avanzar_cuatrimestre', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/becas', ('GET', 'POST'), 'becas_alumno', ('DIRECTIVO', 'CONTADOR')),
    ('/alumno/<matricula>/boleta', ('GET', 'POST'), 'boleta', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/cambiar-estatus', ('POST',), 'cambiar_estatus', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/carga-academica', ('GET',), 'carga_academica', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/cobros', ('GET',), 'cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/alumno/<matricula>/cobros/nuevo', ('POST',), 'nuevo_cargo', ('DIRECTIVO', 'CONTADOR')),
    ('/alumno/<matricula>/documento/<int:doc_id>/ver', ('GET',), 'ver_documento', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/documentos', ('GET', 'POST'), 'documentos', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/estado-cuenta', ('GET',), 'estado_cuenta', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/alumno/<matricula>/expediente', ('GET',), 'ver_expediente', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/ficha', ('GET',), 'ficha_inscripcion', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumno/<matricula>/historial-calificaciones', ('GET',), 'historial_calificaciones', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/alumnos/avanzar-cuatrimestre-lote', ('GET', 'POST'), 'avanzar_cuatrimestre_lote', ('DIRECTIVO', 'ADMINISTRATIVO')),
    ('/alumnos/importar', ('GET', 'POST'), 'importar_alumnos', ('DIRECTIVO',)),
    ('/alumnos/importar/plantilla', ('GET',), 'plantilla_importacion', ('DIRECTIVO',)),
    ('/becas/<int:beca_id>/desactivar', ('POST',), 'desactivar_beca', ('DIRECTIVO', 'CONTADOR')),
    ('/boletas/importar', ('GET',), 'boletas_importar', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/boletas/importar', ('POST',), 'importar_boletas', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/boletas/importar/plantilla', ('GET',), 'plantilla_boletas', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
    ('/buscar', ('POST',), 'buscar', None),
    ('/cobro/<int:cargo_id>/cancelar', ('POST',), 'cancelar_cargo', ('DIRECTIVO', 'CONTADOR')),
    ('/cobro/<int:cargo_id>/condonar-recargo', ('POST',), 'condonar_recargo', ('DIRECTIVO',)),
    ('/cobro/<int:cargo_id>/pagar', ('POST',), 'registrar_pago', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/dashboard', ('GET',), 'dashboard_cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/dashboard/exportar', ('GET',), 'exportar_dashboard_cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/cobros/generar-mensualidades', ('GET', 'POST'), 'generar_mensualidades', ('DIRECTIVO', 'CONTADOR')),
    ('/cobros/recordatorios-vencimiento', ('GET', 'POST'), 'recordatorios_vencimiento', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro', ('GET', 'POST'), 'conceptos_cobro', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro/<int:concepto_id>/editar-precio', ('POST',), 'editar_precio_concepto', ('DIRECTIVO', 'CONTADOR')),
    ('/conceptos-cobro/<int:concepto_id>/toggle', ('POST',), 'toggle_concepto_cobro', ('DIRECTIVO', 'CONTADOR')),
    ('/configuracion/cobros', ('GET', 'POST'), 'configuracion_cobros', ('DIRECTIVO', 'CONTADOR')),
    ('/configuracion/institucion', ('GET', 'POST'), 'configuracion_institucion', ('DIRECTIVO',)),
    ('/documento/<int:doc_id>/eliminar', ('POST',), 'eliminar_documento', ('DIRECTIVO',)),
    ('/login', ('GET', 'POST'), 'login', None),
    ('/logout', ('GET',), 'logout', None),
    ('/materias/<int:materia_id>/eliminar', ('POST',), 'eliminar_materia', ('DIRECTIVO',)),
    ('/pago/<int:pago_id>/anular', ('POST',), 'anular_pago', ('DIRECTIVO', 'CONTADOR')),
    ('/pago/<int:pago_id>/recibo', ('GET',), 'recibo_pago', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/perfil', ('GET', 'POST'), 'perfil', None),
    ('/planes/<int:plan_id>/materias', ('GET', 'POST'), 'gestionar_materias', ('DIRECTIVO',)),
    ('/planes/mensualidades', ('GET', 'POST'), 'planes_mensualidades', ('DIRECTIVO',)),
    ('/registro', ('GET', 'POST'), 'registro', None),
    ('/reportes/cartera-vencida', ('GET',), 'cartera_vencida', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cartera-vencida/exportar', ('GET',), 'exportar_cartera_vencida', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cobros-del-dia', ('GET',), 'reporte_cobros_del_dia', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/reportes/cobros-del-dia/exportar', ('GET',), 'exportar_reporte_cobros_del_dia', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
    ('/usuarios', ('GET',), 'usuarios', ('DIRECTIVO',)),
    ('/usuarios/<int:user_id>/toggle', ('POST',), 'toggle_usuario', ('DIRECTIVO',)),
    ('/usuarios/nuevo', ('GET', 'POST'), 'nuevo_usuario', ('DIRECTIVO',)),
]


def test_el_numero_de_rutas_no_cambia(app):
    reglas = [r for r in app.url_map.iter_rules() if r.endpoint != 'static']
    assert len(reglas) == len(INVENTARIO_ESPERADO), (
        f'Se esperaban {len(INVENTARIO_ESPERADO)} rutas, hay {len(reglas)}. '
        'Si agregaste o quitaste una ruta a propósito, actualiza '
        'INVENTARIO_ESPERADO explícitamente -- esta prueba no debe '
        'ajustarse en silencio.'
    )


def test_cada_ruta_conserva_url_metodos_y_control_de_acceso(app):
    reglas_por_endpoint = {
        regla.endpoint: regla
        for regla in app.url_map.iter_rules()
        if regla.endpoint != 'static'
    }

    fallas = []
    for url_esperada, metodos_esperados, endpoint, roles_esperados in INVENTARIO_ESPERADO:
        regla = reglas_por_endpoint.get(endpoint)
        if regla is None:
            fallas.append(f'Endpoint "{endpoint}" ya no existe (¿se renombró sin actualizar esta tabla?)')
            continue

        if str(regla) != url_esperada:
            fallas.append(f'{endpoint}: URL cambió de "{url_esperada}" a "{regla}"')

        metodos_reales = tuple(sorted(regla.methods - {'HEAD', 'OPTIONS'}))
        if metodos_reales != metodos_esperados:
            fallas.append(f'{endpoint}: métodos cambiaron de {metodos_esperados} a {metodos_reales}')

        roles_reales = _roles_de_vista(app.view_functions[endpoint])
        if roles_reales != roles_esperados:
            fallas.append(f'{endpoint}: roles cambiaron de {roles_esperados} a {roles_reales}')

    assert not fallas, 'Cambios no esperados en el control de rutas:\n' + '\n'.join(fallas)
```

- [ ] **Step 4: Run it against the current, unmodified `app.py`**

Run: `./venv/bin/python -m pytest tests/test_inventario_rutas.py -v`
Expected: **PASS** (2 tests). Same reasoning as Step 2 — this is the
baseline snapshot, not a red test.

- [ ] **Step 5: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed` (166 existing + 3 new: 1 in test_integridad_urls.py + 2 in test_inventario_rutas.py).

- [ ] **Step 6: Commit**

```bash
git add tests/test_integridad_urls.py tests/test_inventario_rutas.py
git commit -m "test: red de seguridad para Plan 2 (integridad de url_for + inventario de rutas)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 1: `extensiones.py`

**Files:**
- Create: `extensiones.py`
- Modify: `app.py:1-32` (imports), `app.py:158-208` (SMTP timeout classes + `mail = _CorreoConTimeout()`)

**Interfaces:**
- Produces: `db`, `migrate`, `login_manager`, `csrf`, `limiter`, `mail` — the exact same 6 extension instances every later module imports instead of defining locally.

- [ ] **Step 0: Freeze the line-number baseline**

```bash
cp app.py app.py.baseline-plan2
```

Every `app.py:X-Y` reference from here through Task 13 means "lines X-Y
of `app.py.baseline-plan2`" — see "Line-number baseline" above. Do this
once, now, before any edit to `app.py` in this plan.

- [ ] **Step 1: Create `extensiones.py`**

Move verbatim from `app.py`:
- Lines 158–208 (the full "CORREO CON TIMEOUT" section: the comment
  block, `class _ConexionSMTPConTimeout(Connection)`, `class
  _CorreoConTimeout(Mail)`, and `mail = _CorreoConTimeout()`).

```python
"""
Instancias compartidas de las extensiones de Flask.

Viven en su propio módulo (en vez de en app.py) para que modelos/,
utilidades/, servicios/ y rutas/ puedan importar `db`, `mail`, etc. sin
crear un import circular con app.py (que es quien las inicializa con
init_app() dentro de create_app()).
"""

import smtplib

from flask import current_app
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_mail import Mail, Connection

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# CORREO CON TIMEOUT (hallazgo #6 de la auditoría)
# ---------------------------------------------------------------------------
# Flask-Mail 0.10.0 abre la conexión con `smtplib.SMTP(server, port)`, SIN
# timeout y sin ninguna opción de configuración para agregarlo (ver
# Connection.configure_host() en la librería). Sin timeout, smtplib hereda
# el default global de sockets: esperar indefinidamente. Como el envío del
# comprobante es SÍNCRONO dentro del flujo de cobro, un SMTP colgado
# bloquearía al worker de Gunicorn que atiende ese pago.
#
# La única forma de agregarlo sin cambiar de librería ni de arquitectura es
# sobrescribir ese método. Se replica tal cual el original y solo se agrega
# `timeout=`. NOTA DE MANTENIMIENTO: esto queda acoplado a la
# implementación de Flask-Mail 0.10.0 -- si algún día se actualiza la
# librería, revisar que configure_host() siga teniendo esta forma (o
# quitar este parche si para entonces ya soporta timeout de fábrica).
class _ConexionSMTPConTimeout(Connection):
    def configure_host(self):
        timeout = current_app.config['MAIL_TIMEOUT']

        if self.mail.use_ssl:
            host = smtplib.SMTP_SSL(self.mail.server, self.mail.port, timeout=timeout)
        else:
            host = smtplib.SMTP(self.mail.server, self.mail.port, timeout=timeout)

        host.set_debuglevel(int(self.mail.debug))

        if self.mail.use_tls:
            host.starttls()

        if self.mail.username and self.mail.password:
            host.login(self.mail.username, self.mail.password)

        return host


class _CorreoConTimeout(Mail):
    """Idéntico a Flask-Mail salvo que sus conexiones llevan MAIL_TIMEOUT."""

    def connect(self):
        app_actual = getattr(self, 'app', None) or current_app
        try:
            return _ConexionSMTPConTimeout(app_actual.extensions['mail'])
        except KeyError as error:
            raise RuntimeError(
                'La aplicación no está configurada con Flask-Mail.'
            ) from error


mail = _CorreoConTimeout()
```

Note: `current_app` replaces nothing here — the original code at
`app.py:180` (`current_app.config['MAIL_TIMEOUT']`) and `app.py:203`
(`getattr(self, 'app', None) or current_app`) already used
`current_app`, never the global `app`. This class moves with **zero**
substitutions, unlike the 7 sites in Appendix C.

- [ ] **Step 2: Remove the moved lines from `app.py`**

Delete `app.py:158-208` (the section just moved). Delete the now-unused
`import smtplib` from the top-level import block (`app.py`) — check
first with `grep -n "smtplib" app.py` that no other remaining code in
`app.py` still uses it (it shouldn't; the only other `smtplib` usage is
inside the moved classes).

- [ ] **Step 3: Update `app.py`'s imports**

Add near the top of `app.py`, right after `from config import
config_by_name`:

```python
from extensiones import db, migrate, login_manager, csrf, limiter, mail
```

Remove the now-duplicate lines from `app.py`'s own import block:
```python
db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
```
and the `from flask_sqlalchemy import SQLAlchemy`, `from flask_migrate
import Migrate`, `from flask_wtf.csrf import CSRFProtect`, `from
flask_limiter import Limiter`, `from flask_limiter.util import
get_remote_address`, `from flask_mail import Mail, Message, Connection`
lines — but keep `Message` importable: `app.py` doesn't use it directly
after this task (it's only used inside `enviar_comprobante_pago`/
`enviar_recordatorio_vencimiento`, which move to `servicios/correo.py`
in Task 4), so this import is simply dropped here and re-added in Task 4
where it's actually used.

- [ ] **Step 4: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

- [ ] **Step 5: Commit**

```bash
git add extensiones.py app.py
git commit -m "refactor: extraer extensiones.py (db, mail con timeout, limiter, etc.)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 2: `modelos/` (+ `utilidades/fechas.py`, pulled forward — see note)

**Confirmed dependency, resolved:** `Alumno.fecha_registro` (`app.py:465`)
is declared `db.Column(db.DateTime, default=ahora_utc)` — a real column
default, evaluated by SQLAlchemy whenever a new `Alumno` is inserted.
`modelos/academico.py` therefore needs `ahora_utc` to exist and be
importable at the moment `Alumno` is defined. Since `utilidades/fechas.py`
has **zero** dependency on `modelos/` (checked: it only imports stdlib +
`flask.current_app`), the one-directional chain in Global Constraints
isn't violated by creating it first — it just means this task creates it
*before* `modelos/academico.py`, ahead of the rest of `utilidades/`
(`seguridad.py`, `archivos.py`, `folios.py`, `paginacion.py`), which stay
in Task 3 as originally planned. This is Step 0 below, done first.

**Files:**
- Create: `utilidades/__init__.py`, `utilidades/fechas.py` (Step 0 only — the
  rest of `utilidades/` still happens in Task 3)
- Create: `modelos/__init__.py`, `modelos/usuarios.py`, `modelos/academico.py`, `modelos/cobros.py`
- Modify: `app.py:30`, `app.py:33-156`, `app.py:210-1184` (moved model/enum classes; `siguiente_folio` stays for Task 3, everything else in this line range moves here or to Step 0)

**Interfaces:**
- Consumes: `extensiones.db` (Task 1).
- Produces: `ahora_utc`, `hoy_local`, `a_local`, `rango_utc_del_dia`,
  `periodo_escolar_actual` (from Step 0's `utilidades/fechas.py` — Task 3
  only adds the 4 template filters and `MESES_LARGOS_ES` to this same
  file, it does not recreate it), plus every model/enum class,
  importable as `from modelos import Usuario, RolUsuario, Alumno, Cargo,
  ...` (re-exported from `modelos/__init__.py`) — this is what
  Task 3/4/5-13 import instead of defining locally.

- [ ] **Step 0: Create `utilidades/__init__.py` and the fechas-only part of `utilidades/fechas.py`**

```python
# utilidades/__init__.py
"""Paquete de utilidades sin estado de negocio: fechas, seguridad, archivos, folios, paginación."""
```

Move verbatim from `app.py`:
- Line 30 (`ZONA_HORARIA_DEFAULT = 'America/Mexico_City'`)
- Lines 33–46 (`ahora_utc`)
- Lines 48–56 (`_zona_horaria` — **apply Appendix C substitution #1**: `app.config.get(...)` → `current_app.config.get(...)`)
- Lines 58–66 (`hoy_local`)
- Lines 68–79 (`a_local`)
- Lines 81–98 (`rango_utc_del_dia`)
- Lines 100–156 (`periodo_escolar_actual`)

```python
"""
Fechas y horas en hora local (México). Los 4 filtros de plantilla que las
exponen (|fecha, |fechahora, |hora, |fecha_larga) se agregan a este mismo
archivo en Task 3 -- las funciones de esta primera parte se crean ahora,
antes que el resto de utilidades/, porque modelos/academico.py necesita
ahora_utc como default de columna (Alumno.fecha_registro) y modelos/ se
crea en esta misma tarea.
"""

from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

from flask import current_app

ZONA_HORARIA_DEFAULT = 'America/Mexico_City'

# <-- pegar aquí ahora_utc, verbatim (app.py:33-46) -->

# <-- pegar aquí _zona_horaria, CON la sustitución de Apéndice C #1 -->

# <-- pegar aquí hoy_local, a_local, rango_utc_del_dia, periodo_escolar_actual, verbatim -->
```

Delete `app.py:30` and `app.py:33-156` now (not in Task 3 — they're
already gone from `app.py` by the end of this step). Task 3 later
**appends** to this same file; it does not overwrite it.

**Add the import back to `app.py` immediately, in this same step —
do not defer it to Task 3.** Verified with `grep -n "ahora_utc(\|hoy_local(\|periodo_escolar_actual(\|rango_utc_del_dia(" app.py`
(restricted to lines below 1465, i.e. code that has **not** moved yet):
`login()` (soon to move in Task 5, still in `app.py` right now) calls
`ahora_utc()` directly at `app.py:1487`, and dozens of other not-yet-moved
routes call `hoy_local()`/`periodo_escolar_actual()`/`rango_utc_del_dia()`.
If the import isn't added now, this task's own Step 8 (`run the full
suite`) fails with `NameError: name 'ahora_utc' is not defined` the
moment any test hits `/login`. Add, near the top of `app.py` (next to the
`extensiones`/other new imports from Task 1):

```python
from utilidades.fechas import (
    ZONA_HORARIA_DEFAULT, ahora_utc, hoy_local, a_local,
    rango_utc_del_dia, periodo_escolar_actual,
)
```

(Task 3, Step 8 later **extends** this same import statement with the 4
filter-function names once they exist — it does not duplicate it.)

- [ ] **Step 1: Create `modelos/usuarios.py`**

(This is Step 1 of the *modelos* work, following Step 0 above which
already created `utilidades/fechas.py`.)

Move verbatim from `app.py`:
- Lines 239–259 (`class RolUsuario(enum.Enum)`)
- Lines 264–310 (`class Usuario(UserMixin, db.Model)`)

```python
"""Modelos de autenticación: quién puede entrar y con qué rol."""

import enum

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensiones import db
from utilidades.fechas import ahora_utc


class RolUsuario(enum.Enum):
    # <-- pegar aquí, verbatim, el cuerpo de app.py:239-259 -->


class Usuario(UserMixin, db.Model):
    # <-- pegar aquí, verbatim, el cuerpo de app.py:264-310 -->
```

(The `# <-- pegar aquí -->` markers above mark exactly where to paste
the verbatim block copied from the given `app.py` line range — read
those lines from `app.py` before deleting them in Step 4, and paste
their unmodified content in place of the marker. No line inside the
range is edited.)

**Confirmed dependency:** `Usuario.fecha_creacion` is `db.Column(db.DateTime,
default=ahora_utc)` — same pattern as `Alumno.fecha_registro` below. The
import above (`from utilidades.fechas import ahora_utc`) covers it.

- [ ] **Step 2: Create `modelos/academico.py`**

Move verbatim from `app.py`, in this order:
- Lines 214–226 (`EstatusAlumno`)
- Lines 227–232 (`TurnoAlumno`)
- Lines 233–238 (`ModalidadEstudio`)
- Lines 315–353 (`PlanEstudio`)
- Lines 354–393 (`Materia`)
- Lines 394–524 (`Alumno`)
- Lines 525–535 (`TipoDocumento`)
- Lines 536–561 (`DocumentoAlumno`)
- Lines 562–609 (`Calificacion`)
- Lines 610–642 (`HistorialEstatus`)
- Lines 643–670 (`HistorialCalificacion`)
- Lines 671–720 (`InscripcionMateria`)

```python
"""
Modelos académicos: planes de estudio, materias, alumnos, documentos del
expediente, calificaciones e inscripciones. "Escudo del Plan de Estudios"
vive aquí (ver docstring de Alumno/Materia en el cuerpo movido).
"""

import enum
from datetime import date

from extensiones import db
from modelos.usuarios import Usuario
from utilidades.fechas import ahora_utc

# ... (imports adicionales exactos que usen las clases movidas -- revisar
# cada bloque copiado por nombres no definidos localmente, ej. `relationship`,
# `UniqueConstraint`, `CheckConstraint`, `func`, `or_` si algún modelo los usa)

# <-- pegar aquí, en el orden de arriba, cada clase verbatim -->
```

**Dependency confirmed and already resolved:** `Alumno.fecha_registro`
(`app.py:465`) is `db.Column(db.DateTime, default=ahora_utc)`. That's
why Step 0 above created `utilidades/fechas.py` first, in this same
task, before this step — `from utilidades.fechas import ahora_utc`
(already in this file's header above) resolves cleanly because that
module already exists on disk by the time this step runs. No forward
reference, no reordering needed.

**A second, more serious dependency, found by an AST-level check of the
pasted body — this one IS circular, and needs a different fix.**
`Alumno.saldo_total_adeudado()` (`app.py:517`, inside the `Alumno` class
body) reads `EstatusCargo.CANCELADO`. `EstatusCargo` is defined in
`modelos/cobros.py` (Step 3, below) — but `modelos/cobros.py` already
imports `Alumno` and `PlanEstudio` from `modelos/academico.py` at module
level (see Step 3's header). A module-level `from modelos.cobros import
EstatusCargo` here would make the two files import each other at load
time — Python raises `ImportError: cannot import name ... (most likely
due to a circular import)` the moment either module is first imported.

**Fix: a local import inside the one method that needs it**, not a
module-level import. This is the standard, minimal way to break a model
circular-import in SQLAlchemy codebases — the import only executes when
the method is *called*, by which point both modules have already
finished loading:

```python
def saldo_total_adeudado(self):
    from modelos.cobros import EstatusCargo  # import local a propósito:
    # evita el ciclo con modelos/cobros.py, que sí importa Alumno a
    # nivel de módulo. Ver la nota de Task 2, Step 2 en el plan.
    ...  # el resto del cuerpo, sin más cambios
```

This is the **one** exception in this entire plan to "move verbatim,
zero edits inside the function body" — everywhere else in Tasks 1–13,
moving code means literally zero lines changed inside a function. Here,
one `import` line is added at the top of one method, because the plan's
own file split (an artifact of Task 2, not of the original code) is what
introduces the circularity — the line doesn't exist in `app.py` today
because `app.py` never had this problem (everything was one file). Do
not "clean this up" by moving the import to module level later; it must
stay local to break the cycle for as long as `academico.py` and
`cobros.py` stay separate files.

- [ ] **Step 3: Create `modelos/cobros.py`**

Move verbatim from `app.py`, in this order:
- Lines 728–734 (`EstatusCargo`)
- Lines 735–742 (`MetodoPago`)
- Lines 743–750 (`TipoRecargo`)
- Lines 751–756 (`TipoDescuentoBeca`)
- Lines 757–779 (`ConceptoCobro`)
- Lines 780–825 (`Beca`)
- Lines 826–856 (`ConfiguracionCobros`)
- Lines 857–918 (`ConfiguracionInstitucion`)
- Lines 919–1054 (`Cargo` — **fix #3's partial unique index lives in this
  class's `__table_args__`; verify it is present, unmodified, after the
  paste**: `grep -n "uq_cargos_activo_matricula_concepto_periodo" modelos/cobros.py` must return a match)
- Lines 1055–1098 (`Pago`)
- Lines 1099–1135 (`ContadorFolio`)

```python
"""
Modelos del sistema de cobros: catálogo de conceptos, becas, cargos,
pagos y folios. Cargo.__table_args__ contiene el índice único parcial del
fix #3 (uq_cargos_activo_matricula_concepto_periodo) -- no tocar su
definición al mover esta clase.
"""

import enum
from decimal import Decimal

from sqlalchemy import UniqueConstraint, CheckConstraint, Index, text

from extensiones import db
from modelos.academico import Alumno, PlanEstudio
from utilidades.fechas import ahora_utc, hoy_local

# <-- pegar aquí, en el orden de arriba, cada clase verbatim -->
```

Adjust the exact import list against what each pasted class body
actually references (e.g. only include `UniqueConstraint`,
`CheckConstraint`, `Index`, `text` if the pasted `Cargo`/other class
bodies use them — verify with `grep` on the pasted content, don't
guess).

**Confirmed dependencies** (verified against `app.py`, not guessed):
`Cargo.fecha_generacion` is `db.Column(db.DateTime, default=ahora_utc)`;
`Cargo.esta_vencido()`/`actualizar_recargo_si_vencido()` call
`hoy_local()`; `Pago.fecha_pago` is `db.Column(db.DateTime,
default=ahora_utc, index=True)`. Both `ahora_utc` and `hoy_local` are
covered by the import added above — no circularity here (`modelos/cobros.py`
depending on `utilidades/fechas.py` is the intended direction, same as
`modelos/academico.py`).

- [ ] **Step 4: Remove the moved lines from `app.py`**

Delete `app.py:210-1135` (everything moved in Steps 1–3 — the full block
from the enums/models section header through `ContadorFolio`). Leave
`siguiente_folio()` (lines 1136-1184) in place for now; it moves in Task
3.

- [ ] **Step 5: Create `modelos/__init__.py`**

```python
"""
Re-exporta todos los modelos y enums para que el resto del proyecto
(servicios/, rutas/, tests/, seed.py, crear_admin.py, y app.py mismo)
pueda seguir escribiendo `from modelos import Alumno, RolUsuario, Cargo`
sin que le importe en qué submódulo vive cada uno.
"""

from modelos.usuarios import RolUsuario, Usuario
from modelos.academico import (
    EstatusAlumno, TurnoAlumno, ModalidadEstudio,
    PlanEstudio, Materia, Alumno,
    TipoDocumento, DocumentoAlumno,
    Calificacion, HistorialEstatus, HistorialCalificacion, InscripcionMateria,
)
from modelos.cobros import (
    EstatusCargo, MetodoPago, TipoRecargo, TipoDescuentoBeca,
    ConceptoCobro, Beca, ConfiguracionCobros, ConfiguracionInstitucion,
    Cargo, Pago, ContadorFolio,
)

__all__ = [
    'RolUsuario', 'Usuario',
    'EstatusAlumno', 'TurnoAlumno', 'ModalidadEstudio',
    'PlanEstudio', 'Materia', 'Alumno',
    'TipoDocumento', 'DocumentoAlumno',
    'Calificacion', 'HistorialEstatus', 'HistorialCalificacion', 'InscripcionMateria',
    'EstatusCargo', 'MetodoPago', 'TipoRecargo', 'TipoDescuentoBeca',
    'ConceptoCobro', 'Beca', 'ConfiguracionCobros', 'ConfiguracionInstitucion',
    'Cargo', 'Pago', 'ContadorFolio',
]
```

- [ ] **Step 6: Update `app.py`'s imports**

Add:
```python
from modelos import (
    RolUsuario, Usuario,
    EstatusAlumno, TurnoAlumno, ModalidadEstudio,
    PlanEstudio, Materia, Alumno,
    TipoDocumento, DocumentoAlumno,
    Calificacion, HistorialEstatus, HistorialCalificacion, InscripcionMateria,
    EstatusCargo, MetodoPago, TipoRecargo, TipoDescuentoBeca,
    ConceptoCobro, Beca, ConfiguracionCobros, ConfiguracionInstitucion,
    Cargo, Pago, ContadorFolio,
)
```

- [ ] **Step 7: Critical check — Alembic still sees every model**

Run: `./venv/bin/flask db migrate -m "verificacion-plan-2-no-deberia-crear-nada"`
Expected: output says **no changes detected**. If Alembic proposes
dropping tables, a model failed to import somewhere in the chain — stop
and fix the import before continuing; **do not** let this generated
migration file get committed (delete it from `migrations/versions/` once
it confirms "no changes").

- [ ] **Step 8: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

- [ ] **Step 9: Commit**

```bash
git add modelos/ app.py
git commit -m "refactor: extraer modelos/ (usuarios, academico, cobros)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 3: `utilidades/` (seguridad, archivos, folios, paginación — `fechas.py` already exists from Task 2 Step 0)

**Files:**
- Create: `utilidades/seguridad.py`, `utilidades/archivos.py`, `utilidades/folios.py`, `utilidades/paginacion.py`
- Modify: `utilidades/fechas.py` (append the 4 template filters + `MESES_LARGOS_ES` — the file itself was already created in Task 2 Step 0), `app.py` (remove the moved functions, add imports)

**Interfaces:**
- Consumes: `extensiones.db`, `extensiones.login_manager`, `modelos.Usuario`, `modelos.ContadorFolio` (Task 2), `utilidades.fechas.*` (Task 2 Step 0).
- Produces: the 4 template filters, `rol_requerido`, `es_url_segura`, `load_user`, `extension_permitida`, `contenido_coincide_con_extension`, `FIRMAS_POR_EXTENSION`, `siguiente_folio`, `_paginar_lista`, `ALUMNOS_POR_PAGINA`, `CARGOS_POR_PAGINA` — every one of Task 4's and every `rutas/*` task's imports of these names comes from here (plus `hoy_local`, `a_local`, `ahora_utc`, `rango_utc_del_dia`, `periodo_escolar_actual`, already produced by Task 2 Step 0).

- [ ] **Step 1: Append the 4 template filters to the existing `utilidades/fechas.py`**

`utilidades/fechas.py` already exists (created in Task 2, Step 0, because
`modelos/academico.py` needed `ahora_utc` as a column default before this
task ran). This step only **adds** to that file — it does not recreate
it.

Move verbatim from `app.py`:
- Line 1194 (`MESES_LARGOS_ES = [...]`, ends line 1199)
- Lines 1200–1206 (`_filtro_fechahora`)
- Lines 1206–1218 (`_filtro_fecha`)
- Lines 1218–1224 (`_filtro_hora`)
- Lines 1224–1235 (`_filtro_fecha_larga`)

Append to the end of `utilidades/fechas.py`:

```python
MESES_LARGOS_ES = [
    # <-- pegar aquí el contenido de app.py:1194-1199 -->
]

# <-- pegar aquí los 4 filtros, verbatim (app.py:1200-1235) -->
```

- [ ] **Step 2: Create `utilidades/seguridad.py`**

Move verbatim from `app.py`:
- Lines 1424–1427 (`load_user`)
- Lines 1428–1442 (`es_url_segura`)
- Lines 1443–1464 (`rol_requerido`)

```python
"""
Autenticación y autorización a nivel de vista: quién puede seguir después
del login (rol_requerido), a dónde puede redirigir "next" sin riesgo de
open-redirect (es_url_segura), y el user_loader de Flask-Login.
"""

from functools import wraps
from urllib.parse import urlparse

from flask import flash, redirect, url_for
from flask_login import login_required, current_user

from extensiones import db, login_manager
from modelos import Usuario


@login_manager.user_loader
def load_user(user_id):
    # <-- pegar aquí, verbatim (app.py:1424-1427) -->


def es_url_segura(destino: str) -> bool:
    # <-- pegar aquí, verbatim (app.py:1428-1442) -->


def rol_requerido(*roles_permitidos):
    # <-- pegar aquí, verbatim (app.py:1443-1464) -->
    # OJO: el docstring de esta función tiene un @app.route de EJEMPLO
    # dentro de un bloque de texto (no un decorador real) -- es el que
    # hacía que "grep -c @app.route" contara 52 en vez de 51 rutas reales.
    # Se copia tal cual, es solo texto ilustrativo en el docstring.
```

- [ ] **Step 3: Create `utilidades/archivos.py`**

Move verbatim from `app.py`:
- Lines 2364–2373 (`extension_permitida` — **apply Appendix C substitution #2**: `app.config['EXTENSIONES_PERMITIDAS']` → `current_app.config['EXTENSIONES_PERMITIDAS']`)
- Lines 2383–2390 (`FIRMAS_POR_EXTENSION` dict + `BYTES_DE_FIRMA` constant)
- Lines 2393–2420 (`contenido_coincide_con_extension`)

```python
"""
Validación de archivos subidos (expediente del alumno): extensión
permitida por configuración, y firma real (magic bytes) del contenido
(fix #7 -- la extensión sola no prueba nada, ver docstring movido).
"""

from flask import current_app

FIRMAS_POR_EXTENSION = {
    # <-- pegar aquí, verbatim (app.py:2383-2388) -->
}

BYTES_DE_FIRMA = 8  # Suficiente para la firma más larga (PNG)


def extension_permitida(nombre_archivo: str) -> bool:
    return (
        '.' in nombre_archivo
        and nombre_archivo.rsplit('.', 1)[1].lower() in current_app.config['EXTENSIONES_PERMITIDAS']
    )


def contenido_coincide_con_extension(archivo) -> bool:
    # <-- pegar aquí, verbatim (app.py:2393-2420) -->
```

- [ ] **Step 4: Create `utilidades/folios.py`**

Move verbatim from `app.py`:
- Lines 1136–1184 (`siguiente_folio`)

```python
"""Generación de folios consecutivos y libres de condición de carrera para pagos/documentos."""

from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import ContadorFolio
from utilidades.fechas import hoy_local

def siguiente_folio(tipo: str, prefijo: str, digitos: int = 6, intentos_maximos: int = 5) -> str:
    # <-- pegar aquí, verbatim (app.py:1136-1184) -->
```

**Confirmed:** the body calls `hoy_local().year` and has `except
IntegrityError:` (retry-on-collision, guarding against two requests
racing to create the same `(tipo, año)` counter row) — both imports
above are required, not speculative. Note `utilidades/folios.py`
importing from `modelos` is a deliberate, working exception to the
"utilidades sits below modelos" layering description in this plan's
Architecture section: it does not create a circular import (nothing in
`modelos/` imports `utilidades.folios`), it just means this one file is
slightly higher in the practical dependency chain than a "pure utility"
would be. Not a defect — ruled acceptable, see the ledger.

- [ ] **Step 5: Create `utilidades/paginacion.py`**

Move verbatim from `app.py`:
- Lines 1711–1712 (`ALUMNOS_POR_PAGINA`, `CARGOS_POR_PAGINA`)
- Lines 1715–1745 (`_paginar_lista`)

```python
"""
Paginación en memoria para listas que ya no se pueden ordenar en SQL
(ej. cartera vencida, ordenada por saldo pendiente, un valor calculado).
"""

# PERFORMANCE-NOTE: con ~800 alumnos, el filtro "Activos" del buscador
# generaba una página de MÁS DE 1 MB de HTML. El cuello de botella no era
# SQL, era el tamaño de la respuesta -- de ahí la paginación.
ALUMNOS_POR_PAGINA = 24   # 24 = 8 filas de 3 tarjetas en pantalla grande
CARGOS_POR_PAGINA = 50


def _paginar_lista(items, page, per_page):
    # <-- pegar aquí, verbatim (app.py:1715-1745) -->
```

- [ ] **Step 6: Confirm `utilidades/__init__.py` already exists**

Created in Task 2, Step 0 — nothing to do here except confirm it's
present: `test -f utilidades/__init__.py && echo OK`.

- [ ] **Step 7: Remove the moved code from `app.py`**

At this point in the migration, lines 30 and 33–156 (the fechas.py
content) were **already deleted from `app.py` in Task 2, Step 0** — do
not try to delete them again here. What's left to delete in this task:
lines 1194–1235 (filters section — `MESES_LARGOS_ES` + the 4 filter
functions, moved in Step 1 above); lines 1424–1464 (`load_user`,
`es_url_segura`, `rol_requerido`); lines 2364–2373, 2383–2420
(`extension_permitida`, `FIRMAS_POR_EXTENSION`, `BYTES_DE_FIRMA`,
`contenido_coincide_con_extension`); lines 1711–1712, 1715–1745
(pagination). Also delete `from functools import wraps` and `from
urllib.parse import urlparse` from `app.py`'s top import block **only
after** confirming with `grep -n "wraps(\|urlparse(" app.py` that nothing
else in the remaining file still uses them (the 413 handler at
`app.py:1378` also uses `urlparse` — if so, **keep** the import in
`app.py`).

- [ ] **Step 8: Update `app.py`'s imports**

The `from utilidades.fechas import (...)` statement already exists in
`app.py` (added in Task 2, Step 0). **Extend that same statement** —
don't add a second, duplicate import from the same module — to also
pull in the 4 filter functions:

```python
from utilidades.fechas import (
    ZONA_HORARIA_DEFAULT, ahora_utc, hoy_local, a_local,
    rango_utc_del_dia, periodo_escolar_actual,
    _filtro_fechahora, _filtro_fecha, _filtro_hora, _filtro_fecha_larga,
)
```

Add these as new, separate import statements:

```python
from utilidades.seguridad import load_user, es_url_segura, rol_requerido
from utilidades.archivos import (
    extension_permitida, contenido_coincide_con_extension, FIRMAS_POR_EXTENSION,
)
from utilidades.folios import siguiente_folio
from utilidades.paginacion import _paginar_lista, ALUMNOS_POR_PAGINA, CARGOS_POR_PAGINA
```

`create_app()` still registers the 4 filters
(`app.add_template_filter(_filtro_fechahora, 'fechahora')`, etc.) — that
registration call stays in `app.py`, only the function bodies moved.

- [ ] **Step 9: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

- [ ] **Step 10: Commit**

```bash
git add utilidades/ app.py
git commit -m "refactor: extraer utilidades/ (fechas, seguridad, archivos, folios, paginacion)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 4: `servicios/`

**Files:**
- Create: `servicios/matriculas.py`, `servicios/alumnos.py`, `servicios/cobros.py`, `servicios/academico.py`, `servicios/reportes.py`, `servicios/correo.py`
- Modify: `app.py` (remove the moved functions, add imports)

**Interfaces:**
- Consumes: `modelos.*` (Task 2), `utilidades.*` (Task 3), `extensiones.mail` (Task 1).
- Produces: every `_helper` function that a `rutas/*` blueprint calls — this is the layer every blueprint task (5–13) imports from.

- [ ] **Step 1: Create `servicios/matriculas.py`**

Move verbatim from `app.py`:
- Lines 1852–1898 (`generar_matricula`)
- Lines 1899–1927 (`crear_alumno_generando_matricula`)

```python
"""Generación de matrícula única y alta de un Alumno con reintento ante colisión."""

from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import Alumno
from utilidades.fechas import ahora_utc

# <-- pegar aquí generar_matricula, verbatim (app.py:1852-1898) -->

# <-- pegar aquí crear_alumno_generando_matricula, verbatim (app.py:1899-1927) -->
```

**Confirmed:** `generar_matricula` uses `ahora_utc().year` and the retry
loop has `except IntegrityError:` (same collision-retry pattern as
`siguiente_folio`) — both imports above are required.

- [ ] **Step 2: Create `servicios/alumnos.py`**

Move verbatim from `app.py`:
- Lines 1626–1666 (`_matriculas_con_adeudo`)
- Lines 1667–1681 (`calcular_estadisticas_alumnos`)

```python
"""Estadísticas y agregaciones sobre Alumno para la pantalla de búsqueda."""

from sqlalchemy import func

from extensiones import db
from modelos import Alumno, EstatusAlumno, Cargo, EstatusCargo, Pago

# <-- pegar aquí _matriculas_con_adeudo, verbatim (app.py:1626-1666) -->

# <-- pegar aquí calcular_estadisticas_alumnos, verbatim (app.py:1667-1681) -->
```

- [ ] **Step 3: Create `servicios/cobros.py`**

Move verbatim from `app.py`, in this order:
- Lines 2946–2960 (`_cargo_duplicado`)
- Lines 2961–2972 (`_meses_del_cuatrimestre_actual`)
- Lines 2973–2999 (`_monto_mensualidad_con_beca` — includes the `is
  None`-vs-`not` fix from the precios-por-institución session; move
  as-is, do not re-derive)
- Lines 3000–3075 (`_generar_cargos_de_periodo` — includes the
  avisos_de_configuracion return value from that same session; move
  as-is)
- Lines 3076–3084 (`_generar_cargos_de_inscripcion`)
- Lines 3085–3091 (`_generar_cargos_de_reinscripcion`)
- Line 3852 (`MESES_ES = [...]`)
- Lines 4018–4033 (`_vencimiento_dia_10_sugerido`)

```python
"""
Lógica de generación automática de cargos: inscripción, reinscripción,
mensualidades con beca aplicada, y sugerencia de fecha de vencimiento.
Los precios son configuración de cada institución -- ver
deploy/PENDIENTES_PRODUCCION.md §3: si falta un precio, estas funciones
NO generan el cargo y devuelven un aviso, nunca inventan un monto.
"""

from datetime import datetime, date

from extensiones import db
from modelos import Cargo, EstatusCargo, ConceptoCobro
from utilidades.fechas import hoy_local, periodo_escolar_actual

MESES_ES = ['', 'Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']

# <-- pegar aquí, en el orden de arriba, cada función verbatim -->
```

**Confirmed:** `_meses_del_cuatrimestre_actual`/`_vencimiento_dia_10_sugerido`
call `hoy_local()`; `_generar_cargos_de_periodo` calls
`periodo_escolar_actual()`. Both imports above are required.

- [ ] **Step 4: Create `servicios/academico.py`**

Move verbatim from `app.py`:
- Lines 1694–1703 (`_max_periodos`)
- Lines 3094–3129 (`_generar_carga_academica`)
- Lines 3130–3164 (`_avanzar_cuatrimestre` — calls `_generar_cargos_de_reinscripcion` and `_generar_carga_academica`; import both)
- Lines 4585–4596 (`_registrar_historial_calificacion`)
- Lines 4597–4613 (`_siguiente_numero_acta`)

```python
"""
Avance de periodo (cuatrimestre), carga académica y helpers del módulo de
boletas/calificaciones.
"""

from extensiones import db
from modelos import (
    ConfiguracionInstitucion, EstatusAlumno, InscripcionMateria, Materia,
    HistorialCalificacion,
)
from servicios.cobros import _generar_cargos_de_reinscripcion
from utilidades.fechas import periodo_escolar_actual
from utilidades.folios import siguiente_folio

# <-- pegar aquí, en el orden de arriba, cada función verbatim -->
```

**Confirmed dependencies (verified against `app.py`, not speculative):**
`_siguiente_numero_acta` (`app.py:4611`) calls
`siguiente_folio(tipo='ACTA', prefijo='ACTA', digitos=6)`; one of
`_max_periodos`/`_generar_carga_academica`/`_avanzar_cuatrimestre` calls
`periodo_escolar_actual()`. Both `utilidades.folios.siguiente_folio` and
`utilidades.fechas.periodo_escolar_actual` are imported above to cover
them. `utilidades/folios.py` and `utilidades/fechas.py` both already
exist by the time this step runs (Task 3, earlier steps).

- [ ] **Step 5: Create `servicios/reportes.py`**

Move verbatim from `app.py`:
- Lines 3566–3602 (`_calcular_reporte_cobros_del_dia`)
- Lines 3707–3773 (`_calcular_cartera_vencida`)
- Lines 3855–3876 (`_rango_ultimos_n_meses`)
- Lines 3877–3956 (`_calcular_dashboard_cobros`)

```python
"""Agregaciones para los reportes de cobros: corte del día, cartera vencida, dashboard."""

from sqlalchemy import func

from extensiones import db
from modelos import Cargo, EstatusCargo, Pago, ConfiguracionCobros
from utilidades.fechas import hoy_local, rango_utc_del_dia
from servicios.cobros import MESES_ES

# <-- pegar aquí, en el orden de arriba, cada función verbatim -->
```

**Confirmed:** `_calcular_reporte_cobros_del_dia`/`_calcular_cartera_vencida`
call `ConfiguracionCobros.obtener()`; `_calcular_dashboard_cobros` indexes
`MESES_ES[mes_ref]` — `MESES_ES` is defined in `servicios/cobros.py`
(Task 4, Step 3, already created earlier in this same task), imported
above rather than redefined here to avoid two different lists drifting
apart.

- [ ] **Step 6: Create `servicios/correo.py`**

Move verbatim from `app.py`:
- Lines 3261–3267 (the `MOTIVO_GENERICO_FALLO_CORREO` constant and its
  comment block — **fix from the SMTP-leak session, part of the audit
  fixes, do not alter its wording**)
- Lines 3268–3303 (`enviar_comprobante_pago` — **apply Appendix C
  substitution #6**: `app.logger.warning(...)` → `current_app.logger.warning(...)`)
- Line 3304 (`DIAS_AVISO_VENCIMIENTO = 3`)
- Lines 3307–3336 (`enviar_recordatorio_vencimiento` — **apply Appendix C
  substitution #7**: same change)

```python
"""
Envío de correo transaccional (comprobante de pago, recordatorio de
vencimiento). Nunca revienta el flujo que lo llama: siempre regresa
(ok: bool, motivo: str | None). El detalle técnico de un fallo SMTP se
registra con logger.warning(exc_info=True) y NUNCA se devuelve a la
interfaz -- MOTIVO_GENERICO_FALLO_CORREO es lo único que puede llegar a
pantalla (ver tests/test_errores_smtp_no_expuestos.py).
"""

from flask import current_app
from flask_mail import Message

from extensiones import mail

MOTIVO_GENERICO_FALLO_CORREO = (
    'No se pudo conectar con el servidor de correo. Revisa la conexión a internet '
    'o la configuración de correo del sistema; el detalle técnico quedó en el log '
    'del servidor.'
)


def enviar_comprobante_pago(alumno, cargo, pago):
    if not alumno.correo:
        return False, 'El alumno no tiene correo registrado.'

    try:
        mensaje = Message(
            subject=f'Comprobante de Pago - Folio {pago.folio or ("#" + str(pago.id))}',
            recipients=[alumno.correo],
            body=(
                f'Hola {alumno.nombre_completo},\n\n'
                f'Se registró tu pago con los siguientes datos:\n\n'
                f'Concepto: {cargo.concepto}\n'
                f'Monto pagado: ${pago.monto_pagado}\n'
                f'Fecha: {pago.fecha_pago.strftime("%d/%m/%Y %H:%M")}\n'
                f'Folio: {pago.folio or ("#" + str(pago.id))}\n'
                f'Saldo pendiente del cargo: ${cargo.saldo_pendiente()}\n\n'
                f'Este es un correo automático del Sistema de Gestión Escolar.'
            ),
        )
        mail.send(mensaje)
        return True, None
    except Exception:
        current_app.logger.warning(
            'Falló el envío del comprobante del pago %s (cargo %s, alumno %s).',
            pago.id, cargo.id, alumno.matricula_id, exc_info=True
        )
        return False, MOTIVO_GENERICO_FALLO_CORREO


DIAS_AVISO_VENCIMIENTO = 3  # Manda el recordatorio cuando falten esta cantidad de días (o menos) para vencer


def enviar_recordatorio_vencimiento(alumno, cargo):
    """Igual que enviar_comprobante_pago: nunca truena, solo reporta si pudo o no."""
    if not alumno.correo:
        return False, 'El alumno no tiene correo registrado.'

    try:
        mensaje = Message(
            subject=f'Recordatorio: {cargo.concepto} próximo a vencer',
            recipients=[alumno.correo],
            body=(
                f'Hola {alumno.nombre_completo},\n\n'
                f'Te recordamos que tienes un pago próximo a vencer:\n\n'
                f'Concepto: {cargo.concepto}\n'
                f'Periodo: {cargo.periodo_escolar or "N/A"}\n'
                f'Saldo pendiente: ${cargo.saldo_pendiente()}\n'
                f'Fecha límite: {cargo.fecha_vencimiento.strftime("%d/%m/%Y")}\n\n'
                f'Después de esta fecha se aplica un recargo por atraso.\n\n'
                f'Este es un correo automático del Sistema de Gestión Escolar.'
            ),
        )
        mail.send(mensaje)
        return True, None
    except Exception:
        current_app.logger.warning(
            'Falló el envío del recordatorio de vencimiento del cargo %s (alumno %s).',
            cargo.id, alumno.matricula_id, exc_info=True
        )
        return False, MOTIVO_GENERICO_FALLO_CORREO
```

- [ ] **Step 7: Remove the moved code from `app.py`**

Delete all line ranges listed in Steps 1–6 above. Double-check
`tests/test_correo_timeout.py` and `tests/test_errores_smtp_no_expuestos.py`
still import `mail` from `app` (they do, per Appendix A — `app.py`
re-exports it via the `extensiones` import already in place since
Task 1, so no test file needs touching).

- [ ] **Step 8: Update `app.py`'s imports**

```python
from servicios.matriculas import generar_matricula, crear_alumno_generando_matricula
from servicios.alumnos import _matriculas_con_adeudo, calcular_estadisticas_alumnos
from servicios.cobros import (
    _cargo_duplicado, _meses_del_cuatrimestre_actual, _monto_mensualidad_con_beca,
    _generar_cargos_de_periodo, _generar_cargos_de_inscripcion,
    _generar_cargos_de_reinscripcion, MESES_ES, _vencimiento_dia_10_sugerido,
)
from servicios.academico import (
    _max_periodos, _generar_carga_academica, _avanzar_cuatrimestre,
    _registrar_historial_calificacion, _siguiente_numero_acta,
)
from servicios.reportes import (
    _calcular_reporte_cobros_del_dia, _calcular_cartera_vencida,
    _rango_ultimos_n_meses, _calcular_dashboard_cobros,
)
from servicios.correo import (
    MOTIVO_GENERICO_FALLO_CORREO, enviar_comprobante_pago,
    DIAS_AVISO_VENCIMIENTO, enviar_recordatorio_vencimiento,
)
```

- [ ] **Step 9: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

Run specifically the two tests tied to fixes touched in this task:
`./venv/bin/python -m pytest tests/test_correo_timeout.py tests/test_errores_smtp_no_expuestos.py tests/test_precios_por_institucion.py tests/test_cargos_duplicados.py -v`
Expected: all pass, unmodified.

- [ ] **Step 10: Commit**

```bash
git add servicios/ app.py
git commit -m "refactor: extraer servicios/ (matriculas, alumnos, cobros, academico, reportes, correo)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 5: `rutas/auth.py`

**Endpoint renames in this task:** `login` → `auth.login`, `logout` →
`auth.logout`, `perfil` → `auth.perfil`.

**Files:**
- Create: `rutas/__init__.py` (empty package marker, created once, here), `rutas/auth.py`
- Modify: `app.py` (remove routes, register blueprint, update
  `login_manager.login_view`), `tests/test_inventario_rutas.py` (3 endpoint
  fields only), `templates/*.html` (rename `url_for` calls per Appendix B)

**Interfaces:**
- Consumes: `extensiones.db, limiter`, `modelos.Usuario`, `utilidades.seguridad.es_url_segura`.
- Produces: `auth_bp` (the `Blueprint` object `app.py` registers).

- [ ] **Step 1: Create `rutas/__init__.py`**

```python
"""Paquete de blueprints. Cada módulo aquí dentro registra su propio Blueprint; app.py los importa y los registra en create_app()."""
```

- [ ] **Step 2: Create `rutas/auth.py`**

Move verbatim from `app.py`:
- Lines 1469–1499 (`login`)
- Lines 1500–1507 (`logout`)
- Lines 1508–1540 (`perfil`)

```python
"""Login, logout y edición del perfil propio."""

from flask import Blueprint, render_template, request, flash, redirect, url_for, session
from flask_login import login_user, logout_user, login_required, current_user

from extensiones import db, limiter
from modelos import Usuario
from utilidades.seguridad import es_url_segura
from utilidades.fechas import ahora_utc

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def login():
    # <-- pegar aquí, verbatim (app.py:1471-1499, cuerpo de la función) -->


@auth_bp.route('/logout')
@login_required
def logout():
    # <-- pegar aquí, verbatim (app.py:1502-1507) -->


@auth_bp.route('/perfil', methods=['GET', 'POST'])
@login_required
def perfil():
    # <-- pegar aquí, verbatim (app.py:1510-1540) -->
```

(Verify the exact decorator/import list against the current lines —
e.g. confirm whether `perfil` uses `@login_required` alone or something
else, by reading `app.py:1508-1510` directly before pasting.)

- [ ] **Step 3: Remove the moved routes from `app.py`**

Delete `app.py:1469-1540`.

- [ ] **Step 4: Register the blueprint and fix `login_view`**

In `create_app()`, after `login_manager.init_app(app)`:
```python
login_manager.login_view = 'auth.login'
```
(was `'login'`).

Near the end of `create_app()`, replace the comment block:
```python
    # Aquí se registrarán los Blueprints en pasos posteriores:
    # from routes.registro import registro_bp
    # from routes.admin import admin_bp
    # app.register_blueprint(registro_bp)
    # app.register_blueprint(admin_bp)
```
with:
```python
    from rutas.auth import auth_bp
    app.register_blueprint(auth_bp)
```
(Later tasks add one `register_blueprint` line each to this same block —
this is the only place blueprint registration happens.)

- [ ] **Step 5: Rename `url_for('login'`, `url_for('logout'`, `url_for('perfil'` everywhere**

```bash
grep -rl "url_for('login'" app.py templates/ | xargs sed -i "s/url_for('login'/url_for('auth.login'/g"
grep -rl "url_for('logout'" app.py templates/ | xargs sed -i "s/url_for('logout'/url_for('auth.logout'/g"
grep -rl "url_for('perfil'" app.py templates/ | xargs sed -i "s/url_for('perfil'/url_for('auth.perfil'/g"
```

Verify zero old references remain:
```bash
grep -rn "url_for('login'\|url_for('logout'\|url_for('perfil'" app.py templates/
```
Expected: no output.

- [ ] **Step 6: Update `tests/test_inventario_rutas.py`**

Change exactly these 3 rows' 3rd field (endpoint), nothing else:
```python
('/login', ('GET', 'POST'), 'auth.login', None),
('/logout', ('GET',), 'auth.logout', None),
('/perfil', ('GET', 'POST'), 'auth.perfil', None),
```

- [ ] **Step 7: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`. Also explicitly:
`./venv/bin/python -m pytest tests/test_integridad_urls.py tests/test_inventario_rutas.py tests/test_autenticacion_roles.py -v`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add rutas/ app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover /login, /logout, /perfil a rutas/auth.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 6: `rutas/usuarios.py`

**Endpoint renames in this task:** `usuarios` → `usuarios.usuarios`,
`nuevo_usuario` → `usuarios.nuevo_usuario`, `toggle_usuario` →
`usuarios.toggle_usuario`.

**Files:**
- Create: `rutas/usuarios.py`
- Modify: `app.py`, `tests/test_inventario_rutas.py` (3 rows), `templates/*.html`

**Interfaces:**
- Consumes: `extensiones.db`, `modelos.Usuario, RolUsuario`, `utilidades.seguridad.rol_requerido`.
- Produces: `usuarios_bp`.

- [ ] **Step 1: Create `rutas/usuarios.py`**

Move verbatim from `app.py`:
- Lines 1545–1551 (`usuarios`)
- Lines 1552–1602 (`nuevo_usuario`)
- Lines 1603–1620 (`toggle_usuario`)

```python
"""Alta y administración de cuentas de personal (DIRECTIVO)."""

from flask import Blueprint, render_template, request, flash, redirect, url_for
from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import Usuario, RolUsuario
from utilidades.seguridad import rol_requerido

usuarios_bp = Blueprint('usuarios', __name__)


@usuarios_bp.route('/usuarios')
@rol_requerido('DIRECTIVO')
def usuarios():
    # <-- pegar aquí, verbatim (app.py:1547-1551) -->


@usuarios_bp.route('/usuarios/nuevo', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def nuevo_usuario():
    # <-- pegar aquí, verbatim (app.py:1554-1602) -->


@usuarios_bp.route('/usuarios/<int:user_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO')
def toggle_usuario(user_id):
    # <-- pegar aquí, verbatim (app.py:1605-1620) -->
```

- [ ] **Step 2: Remove the moved routes from `app.py`**

Delete `app.py:1545-1620`.

- [ ] **Step 3: Register the blueprint**

```python
    from rutas.usuarios import usuarios_bp
    app.register_blueprint(usuarios_bp)
```

- [ ] **Step 4: Rename endpoints everywhere**

```bash
for e in usuarios nuevo_usuario toggle_usuario; do
  grep -rl "url_for('$e'" app.py templates/ | xargs -r sed -i "s/url_for('$e'/url_for('usuarios.$e'/g"
done
grep -rn "url_for('usuarios'\|url_for('nuevo_usuario'\|url_for('toggle_usuario'" app.py templates/
```
Expected: no output on the verification grep (all now read
`usuarios.usuarios` etc., which the pattern with the trailing `'`
correctly excludes from matching the old bare form).

- [ ] **Step 5: Update `tests/test_inventario_rutas.py`**

```python
('/usuarios', ('GET',), 'usuarios.usuarios', ('DIRECTIVO',)),
('/usuarios/<int:user_id>/toggle', ('POST',), 'usuarios.toggle_usuario', ('DIRECTIVO',)),
('/usuarios/nuevo', ('GET', 'POST'), 'usuarios.nuevo_usuario', ('DIRECTIVO',)),
```

- [ ] **Step 6: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

- [ ] **Step 7: Commit**

```bash
git add rutas/usuarios.py app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover /usuarios* a rutas/usuarios.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 7: `rutas/registro.py`

**Endpoint renames in this task:** `registro` → `registro.registro`.

**Files:**
- Create: `rutas/registro.py`
- Modify: `app.py` (route removed, blueprint registered, **429 handler's
  endpoint check updated — fix #4/rate-limit territory, be careful**),
  `tests/test_inventario_rutas.py` (1 row), `templates/*.html`

**Interfaces:**
- Consumes: `extensiones.db, limiter`, `servicios.matriculas.crear_alumno_generando_matricula`, `modelos.PlanEstudio`.
- Produces: `registro_bp`.

- [ ] **Step 1: Create `rutas/registro.py`**

Move verbatim from `app.py`:
- Lines 1928–2073 (`registro`)

```python
"""Auto-registro público de aspirantes. Rate-limited (fix #4)."""

from flask import Blueprint, render_template, request, flash, redirect, url_for
from markupsafe import escape

from extensiones import db, limiter
from modelos import Alumno, PlanEstudio, EstatusAlumno, TurnoAlumno, ModalidadEstudio
from servicios.matriculas import crear_alumno_generando_matricula

registro_bp = Blueprint('registro', __name__)


@registro_bp.route('/registro', methods=['GET', 'POST'])
@limiter.limit('20 per hour;5 per minute', methods=['POST'])
def registro():
    # <-- pegar aquí, verbatim (app.py:1930-2073) -->
```

(Confirm the exact decorator on this route by reading `app.py:1928-1930`
directly — the plan's spec §5 documents it as `@limiter.limit('20 per
hour;5 per minute', methods=['POST'])` from fix #4; **do not weaken or
remove this decorator**, it is one of the 9 audit fixes.)

- [ ] **Step 2: Remove the moved route from `app.py`**

Delete `app.py:1928-2073`.

- [ ] **Step 3: Register the blueprint**

```python
    from rutas.registro import registro_bp
    app.register_blueprint(registro_bp)
```

- [ ] **Step 4: Update the 429 handler's endpoint check**

In `limite_intentos_excedido` (stays in `app.py`), change:
```python
    if request.endpoint == 'registro':
```
to:
```python
    if request.endpoint == 'registro.registro':
```
and:
```python
        return redirect(url_for('registro'))
```
to:
```python
        return redirect(url_for('registro.registro'))
```
and the fallback at the bottom of that same function:
```python
    return redirect(url_for('login'))
```
was already renamed to `url_for('auth.login')` in Task 5 — confirm it's
still correct, don't re-touch it.

- [ ] **Step 5: Rename `url_for('registro'` everywhere else**

```bash
grep -rl "url_for('registro'" app.py templates/ | xargs sed -i "s/url_for('registro'/url_for('registro.registro'/g"
grep -rn "url_for('registro'" app.py templates/
```
Expected: no output (the handler's own reference was already fixed by
hand in Step 4, above, to keep that specific edit visible and reviewable
rather than folded into a blanket `sed`).

- [ ] **Step 6: Update `tests/test_inventario_rutas.py`**

```python
('/registro', ('GET', 'POST'), 'registro.registro', None),
```

- [ ] **Step 7: Run the full suite, with special attention to rate-limit tests**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

Run specifically: `./venv/bin/python -m pytest tests/test_rate_limit_registro.py tests/test_xss_mensajes_registro.py tests/test_registro_publico.py -v`
Expected: all pass — these three files directly exercise fixes #4 and
the flash-escaping fix; a broken endpoint rename here would show up as
either a redirect loop or a 404, not a subtle diff.

- [ ] **Step 8: Commit**

```bash
git add rutas/registro.py app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover /registro a rutas/registro.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 8: `rutas/documentos.py`

**Endpoint renames in this task:** `documentos` → `documentos.documentos`,
`ver_documento` → `documentos.ver_documento`, `eliminar_documento` →
`documentos.eliminar_documento`.

**Files:**
- Create: `rutas/documentos.py`
- Modify: `app.py`, `tests/test_inventario_rutas.py` (3 rows), `templates/*.html`

**Interfaces:**
- Consumes: `extensiones.db`, `modelos.Alumno, DocumentoAlumno, TipoDocumento`, `utilidades.seguridad.rol_requerido`, `utilidades.archivos.extension_permitida, contenido_coincide_con_extension`.
- Produces: `documentos_bp`.

- [ ] **Step 1: Create `rutas/documentos.py`**

Move verbatim from `app.py`:
- Lines 2420–2429 (`CAMPOS_DOCUMENTOS` dict)
- Lines 2430–2520 (`documentos` — **apply Appendix C substitution #3**)
- Lines 2521–2552 (`ver_documento` — **apply Appendix C substitution #4**)
- Lines 2553–2584 (`eliminar_documento` — **apply Appendix C substitution #5**)

```python
"""
Documentos del expediente: subida (fix #7 — validación de firma real de
archivo), consulta y borrado. Todas las rutas exigen sesión + rol.
"""

import os

from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, send_file, current_app

from extensiones import db
from modelos import Alumno, DocumentoAlumno, TipoDocumento
from utilidades.seguridad import rol_requerido
from utilidades.archivos import extension_permitida, contenido_coincide_con_extension
from utilidades.fechas import ahora_utc
from servicios.academico import _max_periodos

documentos_bp = Blueprint('documentos', __name__)

CAMPOS_DOCUMENTOS = {
    # <-- pegar aquí, verbatim (app.py:2420-2429) -->
}


@documentos_bp.route('/alumno/<matricula>/documentos', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def documentos(matricula):
    # <-- pegar aquí, verbatim (app.py:2432-2520), con la sustitución de
    #     Apéndice C #3 (app.config['UPLOAD_FOLDER'] -> current_app.config[...]) -->


@documentos_bp.route('/alumno/<matricula>/documento/<int:doc_id>/ver')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def ver_documento(matricula, doc_id):
    # <-- pegar aquí, verbatim (app.py:2523-2552), con la sustitución #4 -->


@documentos_bp.route('/documento/<int:doc_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_documento(doc_id):
    # <-- pegar aquí, verbatim (app.py:2555-2584), con la sustitución #5 -->
```

(Verify the precise `send_file`/`send_from_directory` and `os.path`
imports each function body needs by reading the exact code at those line
ranges — `app.py` already confirmed `os.path.join` usage at lines 2541,
2566, 2451.)

- [ ] **Step 2: Remove the moved routes from `app.py`**

Delete `app.py:2420-2584`.

- [ ] **Step 3: Register the blueprint**

```python
    from rutas.documentos import documentos_bp
    app.register_blueprint(documentos_bp)
```

- [ ] **Step 4: Rename endpoints everywhere**

```bash
for e in documentos ver_documento eliminar_documento; do
  grep -rl "url_for('$e'" app.py templates/ | xargs -r sed -i "s/url_for('$e'/url_for('documentos.$e'/g"
done
grep -rn "url_for('documentos'\|url_for('ver_documento'\|url_for('eliminar_documento'" app.py templates/
```
Expected: no output. **Careful:** the blueprint `cobros` also has a
route named `cobros` (collision noted in Appendix B); `documentos` has
no such collision (no other endpoint is literally named `documentos`),
but double-check with `grep -c "url_for('documentos'" templates/*.html
app.py` before running the loop, since `documentos_bp` variable name
itself must never appear inside a `url_for(...)` string.

- [ ] **Step 5: Update `tests/test_inventario_rutas.py`**

```python
('/alumno/<matricula>/documento/<int:doc_id>/ver', ('GET',), 'documentos.ver_documento', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/alumno/<matricula>/documentos', ('GET', 'POST'), 'documentos.documentos', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/documento/<int:doc_id>/eliminar', ('POST',), 'documentos.eliminar_documento', ('DIRECTIVO',)),
```

- [ ] **Step 6: Run the full suite, with special attention to file validation**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

Run specifically: `./venv/bin/python -m pytest tests/test_validacion_archivos.py -v`
Expected: all pass — this is the fix #7 test file, exercising exactly
the two functions substituted in this task.

- [ ] **Step 7: Commit**

```bash
git add rutas/documentos.py app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover documentos del expediente a rutas/documentos.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 9: `rutas/academico.py`

**Endpoint renames in this task:** `boleta` → `academico.boleta`,
`boletas_importar` → `academico.boletas_importar`, `importar_boletas` →
`academico.importar_boletas`, `plantilla_boletas` →
`academico.plantilla_boletas`, `historial_calificaciones` →
`academico.historial_calificaciones`, `carga_academica` →
`academico.carga_academica`.

**Special case:** `/boletas/importar` is served by **two** different
endpoints depending on HTTP method (`boletas_importar` for GET,
`importar_boletas` for POST). Both move to this blueprint; both get
renamed independently by the same generic rule.

**Files:**
- Create: `rutas/academico.py`
- Modify: `app.py`, `tests/test_inventario_rutas.py` (6 rows), `templates/*.html`

**Interfaces:**
- Consumes: `extensiones.db`, `modelos.Alumno, Materia, PlanEstudio, Calificacion, InscripcionMateria`, `utilidades.seguridad.rol_requerido`, `utilidades.fechas.periodo_escolar_actual`, `servicios.academico._max_periodos, _registrar_historial_calificacion, _siguiente_numero_acta`, `openpyxl`.
- Produces: `academico_bp`.

- [ ] **Step 1: Create `rutas/academico.py`**

Move verbatim from `app.py`:
- Lines 4614–4718 (`boleta`)
- Lines 4719–4736 (`boletas_importar`)
- Lines 4737–4815 (`plantilla_boletas`)
- Lines 4816–4968 (`importar_boletas`)
- Lines 4969–4986 (`historial_calificaciones`)
- Lines 4987–5027 (`carga_academica`)

```python
"""
Módulo académico: boletas de calificaciones (captura individual + carga
masiva desde Excel), historial y carga académica del alumno.
"""

import io

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from flask import Blueprint, render_template, request, flash, redirect, url_for, send_file

from extensiones import db
from modelos import (
    Alumno, Materia, PlanEstudio, Calificacion, InscripcionMateria,
    EstatusAlumno, HistorialCalificacion,
)
from utilidades.seguridad import rol_requerido
from utilidades.fechas import periodo_escolar_actual
from servicios.academico import _max_periodos, _registrar_historial_calificacion, _siguiente_numero_acta

academico_bp = Blueprint('academico', __name__)


@academico_bp.route('/alumno/<matricula>/boleta', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def boleta(matricula):
    # <-- pegar aquí, verbatim (app.py:4616-4718) -->


@academico_bp.route('/boletas/importar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def boletas_importar():
    # <-- pegar aquí, verbatim (app.py:4721-4736) -->


@academico_bp.route('/boletas/importar/plantilla')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def plantilla_boletas():
    # <-- pegar aquí, verbatim (app.py:4739-4815) -->


@academico_bp.route('/boletas/importar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def importar_boletas():
    # <-- pegar aquí, verbatim (app.py:4818-4968) -->


@academico_bp.route('/alumno/<matricula>/historial-calificaciones')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def historial_calificaciones(matricula):
    # <-- pegar aquí, verbatim (app.py:4971-4986) -->


@academico_bp.route('/alumno/<matricula>/carga-academica')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def carga_academica(matricula):
    # <-- pegar aquí, verbatim (app.py:4989-5027) -->
```

(Verify each route's exact decorator by reading `app.py` directly at its
line number before pasting — do not assume from the spec table alone.)

- [ ] **Step 2: Remove the moved routes from `app.py`**

Delete `app.py:4614-5027` (this is now the end of the meaningful content
in `app.py` — only the `if __name__ == '__main__':` block at the very
end, lines 5028+ prior to this deletion, remains after it).

- [ ] **Step 3: Register the blueprint**

```python
    from rutas.academico import academico_bp
    app.register_blueprint(academico_bp)
```

- [ ] **Step 4: Rename endpoints everywhere**

```bash
for e in boleta boletas_importar plantilla_boletas importar_boletas historial_calificaciones carga_academica; do
  grep -rl "url_for('$e'" app.py templates/ | xargs -r sed -i "s/url_for('$e'/url_for('academico.$e'/g"
done
grep -rn "url_for('boleta'\|url_for('boletas_importar'\|url_for('plantilla_boletas'\|url_for('importar_boletas'\|url_for('historial_calificaciones'\|url_for('carga_academica'" app.py templates/
```
Expected: no output. **Careful with `boleta`:** the pattern
`url_for('boleta'` (with the trailing quote) does NOT match
`url_for('boletas_importar'` because the character after `boleta` in
the second string is `s`, not `'` — confirmed safe by construction, but
re-run the verification grep anyway rather than assuming.

- [ ] **Step 5: Update `tests/test_inventario_rutas.py`**

```python
('/alumno/<matricula>/boleta', ('GET', 'POST'), 'academico.boleta', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/alumno/<matricula>/carga-academica', ('GET',), 'academico.carga_academica', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/alumno/<matricula>/historial-calificaciones', ('GET',), 'academico.historial_calificaciones', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/boletas/importar', ('GET',), 'academico.boletas_importar', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/boletas/importar', ('POST',), 'academico.importar_boletas', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/boletas/importar/plantilla', ('GET',), 'academico.plantilla_boletas', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
```

- [ ] **Step 6: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

Run specifically: `./venv/bin/python -m pytest tests/test_escudo_plan_estudios.py -v`
Expected: all pass — this file exercises the "Escudo del Plan de
Estudios" rule inside `boleta`/`importar_boletas`.

- [ ] **Step 7: Commit**

```bash
git add rutas/academico.py app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover boletas/carga académica a rutas/academico.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 10: `rutas/configuracion.py`

**Endpoint renames in this task:** `planes_mensualidades` →
`configuracion.planes_mensualidades`, `gestionar_materias` →
`configuracion.gestionar_materias`, `eliminar_materia` →
`configuracion.eliminar_materia`, `conceptos_cobro` →
`configuracion.conceptos_cobro`, `editar_precio_concepto` →
`configuracion.editar_precio_concepto`, `toggle_concepto_cobro` →
`configuracion.toggle_concepto_cobro`, `configuracion_institucion` →
`configuracion.configuracion_institucion`, `configuracion_cobros` →
`configuracion.configuracion_cobros`.

**Files:**
- Create: `rutas/configuracion.py`
- Modify: `app.py`, `tests/test_inventario_rutas.py` (8 rows), `templates/*.html`

**Interfaces:**
- Consumes: `extensiones.db`, `modelos.PlanEstudio, Materia, ConceptoCobro, ConfiguracionCobros, ConfiguracionInstitucion`, `utilidades.seguridad.rol_requerido`.
- Produces: `configuracion_bp`.

This is where the two price-configuration screens from the "precios por
institución" session live — **do not alter their `NULL`-vs-`0.00`
handling** (`monto_mensualidad = None` on empty input, `is None` checks,
etc.). Move the bodies verbatim.

- [ ] **Step 1: Create `rutas/configuracion.py`**

Move verbatim from `app.py`:
- Lines 4265–4304 (`planes_mensualidades`)
- Lines 4305–4370 (`gestionar_materias`)
- Lines 4371–4395 (`eliminar_materia`)
- Lines 4396–4430 (`conceptos_cobro`)
- Lines 4431–4460 (`editar_precio_concepto`)
- Lines 4461–4472 (`toggle_concepto_cobro`)
- Lines 4473–4527 (`configuracion_institucion`)
- Lines 4528–4578 (`configuracion_cobros`)

```python
"""
Configuración de la institución: mensualidades por carrera, materias por
plan, catálogo de conceptos de cobro (y sus precios), datos generales de
la institución y política de recargos. Ver deploy/PENDIENTES_PRODUCCION.md
§3: NULL en un precio significa "sin configurar", nunca se convierte en
$0 automáticamente -- no alterar esa regla al mover estas rutas.
"""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, flash, redirect, url_for

from extensiones import db
from modelos import (
    PlanEstudio, Materia, ConceptoCobro, ConfiguracionCobros, ConfiguracionInstitucion,
    Calificacion, InscripcionMateria, TipoRecargo,
)
from utilidades.seguridad import rol_requerido
from servicios.academico import _max_periodos

configuracion_bp = Blueprint('configuracion', __name__)


@configuracion_bp.route('/planes/mensualidades', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def planes_mensualidades():
    # <-- pegar aquí, verbatim (app.py:4267-4304) -->


@configuracion_bp.route('/planes/<int:plan_id>/materias', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def gestionar_materias(plan_id):
    # <-- pegar aquí, verbatim (app.py:4307-4370) -->


@configuracion_bp.route('/materias/<int:materia_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_materia(materia_id):
    # <-- pegar aquí, verbatim (app.py:4373-4395) -->


@configuracion_bp.route('/conceptos-cobro', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def conceptos_cobro():
    # <-- pegar aquí, verbatim (app.py:4398-4430) -->


@configuracion_bp.route('/conceptos-cobro/<int:concepto_id>/editar-precio', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def editar_precio_concepto(concepto_id):
    # <-- pegar aquí, verbatim (app.py:4433-4460) -->


@configuracion_bp.route('/conceptos-cobro/<int:concepto_id>/toggle', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def toggle_concepto_cobro(concepto_id):
    # <-- pegar aquí, verbatim (app.py:4463-4472) -->


@configuracion_bp.route('/configuracion/institucion', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def configuracion_institucion():
    # <-- pegar aquí, verbatim (app.py:4475-4527) -->


@configuracion_bp.route('/configuracion/cobros', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def configuracion_cobros():
    # <-- pegar aquí, verbatim (app.py:4530-4578) -->
```

- [ ] **Step 2: Remove the moved routes from `app.py`**

Delete `app.py:4265-4578`.

- [ ] **Step 3: Register the blueprint**

```python
    from rutas.configuracion import configuracion_bp
    app.register_blueprint(configuracion_bp)
```

- [ ] **Step 4: Rename endpoints everywhere**

```bash
for e in planes_mensualidades gestionar_materias eliminar_materia conceptos_cobro editar_precio_concepto toggle_concepto_cobro configuracion_institucion configuracion_cobros; do
  grep -rl "url_for('$e'" app.py templates/ | xargs -r sed -i "s/url_for('$e'/url_for('configuracion.$e'/g"
done
grep -rn "url_for('planes_mensualidades'\|url_for('gestionar_materias'\|url_for('eliminar_materia'\|url_for('conceptos_cobro'\|url_for('editar_precio_concepto'\|url_for('toggle_concepto_cobro'\|url_for('configuracion_institucion'\|url_for('configuracion_cobros'" app.py templates/
```
Expected: no output. **Careful with `conceptos_cobro`:** verify it
doesn't collide with `toggle_concepto_cobro`/`editar_precio_concepto`
(different strings, no substring collision — `conceptos_cobro'` never
appears as a prefix of the other two names).

- [ ] **Step 5: Update `tests/test_inventario_rutas.py`**

```python
('/conceptos-cobro', ('GET', 'POST'), 'configuracion.conceptos_cobro', ('DIRECTIVO', 'CONTADOR')),
('/conceptos-cobro/<int:concepto_id>/editar-precio', ('POST',), 'configuracion.editar_precio_concepto', ('DIRECTIVO', 'CONTADOR')),
('/conceptos-cobro/<int:concepto_id>/toggle', ('POST',), 'configuracion.toggle_concepto_cobro', ('DIRECTIVO', 'CONTADOR')),
('/configuracion/cobros', ('GET', 'POST'), 'configuracion.configuracion_cobros', ('DIRECTIVO', 'CONTADOR')),
('/configuracion/institucion', ('GET', 'POST'), 'configuracion.configuracion_institucion', ('DIRECTIVO',)),
('/materias/<int:materia_id>/eliminar', ('POST',), 'configuracion.eliminar_materia', ('DIRECTIVO',)),
('/planes/<int:plan_id>/materias', ('GET', 'POST'), 'configuracion.gestionar_materias', ('DIRECTIVO',)),
('/planes/mensualidades', ('GET', 'POST'), 'configuracion.planes_mensualidades', ('DIRECTIVO',)),
```

- [ ] **Step 6: Run the full suite, with special attention to the pricing rules**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

Run specifically: `./venv/bin/python -m pytest tests/test_precios_por_institucion.py tests/test_recargos_y_conceptos.py tests/test_configuracion_institucion.py -v`
Expected: all pass, including the 3 role-restriction tests
(`test_un_capturador_no_puede_cambiar_...`, etc.) — a mis-set decorator
here is exactly what those tests exist to catch.

- [ ] **Step 7: Commit**

```bash
git add rutas/configuracion.py app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover configuración (precios, materias, institución) a rutas/configuracion.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 11: `rutas/reportes.py`

**Endpoint renames in this task:** `reporte_cobros_del_dia` →
`reportes.reporte_cobros_del_dia`, `exportar_reporte_cobros_del_dia` →
`reportes.exportar_reporte_cobros_del_dia`, `cartera_vencida` →
`reportes.cartera_vencida`, `exportar_cartera_vencida` →
`reportes.exportar_cartera_vencida`, `dashboard_cobros` →
`reportes.dashboard_cobros`, `exportar_dashboard_cobros` →
`reportes.exportar_dashboard_cobros`.

**Files:**
- Create: `rutas/reportes.py`
- Modify: `app.py`, `tests/test_inventario_rutas.py` (6 rows), `templates/*.html`

**Interfaces:**
- Consumes: `extensiones.db`, `modelos.Cargo, EstatusCargo, Pago`, `utilidades.seguridad.rol_requerido`, `utilidades.fechas.hoy_local`, `utilidades.paginacion._paginar_lista, CARGOS_POR_PAGINA`, `servicios.reportes.*`, `openpyxl`.
- Produces: `reportes_bp`.

- [ ] **Step 1: Create `rutas/reportes.py`**

Move verbatim from `app.py`:
- Lines 3603–3629 (`reporte_cobros_del_dia`)
- Lines 3630–3706 (`exportar_reporte_cobros_del_dia`)
- Lines 3774–3800 (`cartera_vencida`)
- Lines 3801–3854 (`exportar_cartera_vencida`)
- Lines 3957–3963 (`dashboard_cobros`)
- Lines 3964–4017 (`exportar_dashboard_cobros`)

```python
"""Reportes de cobros: corte del día, cartera vencida y dashboard, cada uno con su exportación a Excel."""

import io

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from flask import Blueprint, render_template, request, send_file

from extensiones import db
from modelos import Cargo, EstatusCargo, Pago
from utilidades.seguridad import rol_requerido
from utilidades.fechas import hoy_local, ahora_utc
from utilidades.paginacion import _paginar_lista, CARGOS_POR_PAGINA
from servicios.reportes import (
    _calcular_reporte_cobros_del_dia, _calcular_cartera_vencida,
    _rango_ultimos_n_meses, _calcular_dashboard_cobros,
)

reportes_bp = Blueprint('reportes', __name__)


@reportes_bp.route('/reportes/cobros-del-dia')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def reporte_cobros_del_dia():
    # <-- pegar aquí, verbatim (app.py:3605-3629) -->


@reportes_bp.route('/reportes/cobros-del-dia/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_reporte_cobros_del_dia():
    # <-- pegar aquí, verbatim (app.py:3632-3706) -->


@reportes_bp.route('/reportes/cartera-vencida')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def cartera_vencida():
    # <-- pegar aquí, verbatim (app.py:3776-3800) -->


@reportes_bp.route('/reportes/cartera-vencida/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_cartera_vencida():
    # <-- pegar aquí, verbatim (app.py:3803-3854) -->


@reportes_bp.route('/cobros/dashboard')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def dashboard_cobros():
    # <-- pegar aquí, verbatim (app.py:3959-3963) -->


@reportes_bp.route('/cobros/dashboard/exportar')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def exportar_dashboard_cobros():
    # <-- pegar aquí, verbatim (app.py:3966-4017) -->
```

- [ ] **Step 2: Remove the moved routes from `app.py`**

Delete `app.py:3603-3629`, `app.py:3630-3706`, `app.py:3774-3854`,
`app.py:3957-4017` (four separate ranges — the helper functions between
them, e.g. `_calcular_cartera_vencida` at 3707-3773, already moved to
`servicios/reportes.py` in Task 4 and are gone from `app.py` already;
only delete what's still there).

- [ ] **Step 3: Register the blueprint**

```python
    from rutas.reportes import reportes_bp
    app.register_blueprint(reportes_bp)
```

- [ ] **Step 4: Rename endpoints everywhere**

```bash
for e in reporte_cobros_del_dia exportar_reporte_cobros_del_dia cartera_vencida exportar_cartera_vencida dashboard_cobros exportar_dashboard_cobros; do
  grep -rl "url_for('$e'" app.py templates/ | xargs -r sed -i "s/url_for('$e'/url_for('reportes.$e'/g"
done
grep -rn "url_for('reporte_cobros_del_dia'\|url_for('exportar_reporte_cobros_del_dia'\|url_for('cartera_vencida'\|url_for('exportar_cartera_vencida'\|url_for('dashboard_cobros'\|url_for('exportar_dashboard_cobros'" app.py templates/
```
Expected: no output.

- [ ] **Step 5: Update `tests/test_inventario_rutas.py`**

```python
('/cobros/dashboard', ('GET',), 'reportes.dashboard_cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
('/cobros/dashboard/exportar', ('GET',), 'reportes.exportar_dashboard_cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
('/reportes/cartera-vencida', ('GET',), 'reportes.cartera_vencida', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
('/reportes/cartera-vencida/exportar', ('GET',), 'reportes.exportar_cartera_vencida', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
('/reportes/cobros-del-dia', ('GET',), 'reportes.reporte_cobros_del_dia', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
('/reportes/cobros-del-dia/exportar', ('GET',), 'reportes.exportar_reporte_cobros_del_dia', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
```

- [ ] **Step 6: Run the full suite**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

- [ ] **Step 7: Commit**

```bash
git add rutas/reportes.py app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover reportes de cobros a rutas/reportes.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 12: `rutas/cobros.py`

**This is the most delicate task — `registrar_pago` carries fix #2 (row
lock).** Extra care required, see Step 4.

**Endpoint renames in this task:** `cobros` → `cobros.cobros`,
`nuevo_cargo` → `cobros.nuevo_cargo`, `becas_alumno` →
`cobros.becas_alumno`, `desactivar_beca` → `cobros.desactivar_beca`,
`registrar_pago` → `cobros.registrar_pago`, `cancelar_cargo` →
`cobros.cancelar_cargo`, `condonar_recargo` → `cobros.condonar_recargo`,
`anular_pago` → `cobros.anular_pago`, `recibo_pago` →
`cobros.recibo_pago`, `estado_cuenta` → `cobros.estado_cuenta`,
`generar_mensualidades` → `cobros.generar_mensualidades`,
`recordatorios_vencimiento` → `cobros.recordatorios_vencimiento`.

**Files:**
- Create: `rutas/cobros.py`
- Modify: `app.py`, `tests/test_inventario_rutas.py` (12 rows), `templates/*.html`

**Interfaces:**
- Consumes: `extensiones.db`, `modelos.Cargo, EstatusCargo, Pago, Beca, ConceptoCobro, MetodoPago, Alumno`, `utilidades.seguridad.rol_requerido`, `utilidades.fechas.hoy_local`, `servicios.cobros.*`, `servicios.correo.enviar_comprobante_pago, enviar_recordatorio_vencimiento, DIAS_AVISO_VENCIMIENTO`.
- Produces: `cobros_bp`.

- [ ] **Step 1: Create `rutas/cobros.py`**

Move verbatim from `app.py`:
- Lines 2809–2843 (`cobros`)
- Lines 2844–2934 (`becas_alumno`)
- Lines 2935–2945 (`desactivar_beca`)
- Lines 3165–3260 (`nuevo_cargo`)
- Lines 3337–3412 (`registrar_pago` — **fix #2, see Step 4**)
- Lines 3413–3455 (`anular_pago`)
- Lines 3456–3479 (`cancelar_cargo`)
- Lines 3480–3528 (`condonar_recargo`)
- Lines 3529–3535 (`recibo_pago`)
- Lines 3536–3565 (`estado_cuenta`)
- Lines 4034–4147 (`generar_mensualidades`)
- Lines 4227–4264 (`recordatorios_vencimiento`)

```python
"""
Sistema de cobros: cargos, pagos, becas, estado de cuenta, generación
masiva de mensualidades y recordatorios de vencimiento.

registrar_pago() contiene el fix #2 de la auditoría de producción: un
SELECT ... FOR UPDATE sobre Cargo (con with_for_update(), gateado por
db.engine.dialect.name != 'sqlite' porque SQLite no soporta bloqueo por
fila) que impide que dos cobros concurrentes sobre el mismo cargo se
pasen del saldo disponible. Ver
deploy/PENDIENTES_PRODUCCION.md §1 para el detalle verificado del
mecanismo. Esta función se mueve sin editar una sola línea de su cuerpo.
"""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, flash, redirect, url_for, abort
from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import (
    Cargo, EstatusCargo, Pago, Beca, ConceptoCobro, MetodoPago, Alumno,
    EstatusAlumno, TipoDescuentoBeca,
)
from utilidades.seguridad import rol_requerido
from utilidades.fechas import hoy_local, ahora_utc, periodo_escolar_actual
from servicios.cobros import _cargo_duplicado, _vencimiento_dia_10_sugerido, _monto_mensualidad_con_beca
from servicios.correo import enviar_comprobante_pago, enviar_recordatorio_vencimiento, DIAS_AVISO_VENCIMIENTO

cobros_bp = Blueprint('cobros', __name__)


@cobros_bp.route('/alumno/<matricula>/cobros')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def cobros(matricula):
    # <-- pegar aquí, verbatim (app.py:2811-2843) -->


@cobros_bp.route('/alumno/<matricula>/becas', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def becas_alumno(matricula):
    # <-- pegar aquí, verbatim (app.py:2846-2934) -->


@cobros_bp.route('/becas/<int:beca_id>/desactivar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def desactivar_beca(beca_id):
    # <-- pegar aquí, verbatim (app.py:2937-2945) -->


@cobros_bp.route('/alumno/<matricula>/cobros/nuevo', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def nuevo_cargo(matricula):
    # <-- pegar aquí, verbatim (app.py:3167-3260) -->


@cobros_bp.route('/cobro/<int:cargo_id>/pagar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def registrar_pago(cargo_id):
    # <-- pegar aquí, verbatim EXACTO (app.py:3339-3412) -- NO EDITAR NI
    #     UNA LÍNEA. Confirmar después de pegar, con grep sobre este
    #     archivo, que sigue apareciendo:
    #       with_for_update()
    #       db.engine.dialect.name != 'sqlite'
    #     en el mismo orden relativo que en app.py hoy. -->


@cobros_bp.route('/pago/<int:pago_id>/anular', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def anular_pago(pago_id):
    # <-- pegar aquí, verbatim (app.py:3415-3455) -->


@cobros_bp.route('/cobro/<int:cargo_id>/cancelar', methods=['POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def cancelar_cargo(cargo_id):
    # <-- pegar aquí, verbatim (app.py:3458-3479) -->


@cobros_bp.route('/cobro/<int:cargo_id>/condonar-recargo', methods=['POST'])
@rol_requerido('DIRECTIVO')
def condonar_recargo(cargo_id):
    # <-- pegar aquí, verbatim (app.py:3482-3528) -->


@cobros_bp.route('/pago/<int:pago_id>/recibo')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def recibo_pago(pago_id):
    # <-- pegar aquí, verbatim (app.py:3531-3535) -->


@cobros_bp.route('/alumno/<matricula>/estado-cuenta')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')
def estado_cuenta(matricula):
    # <-- pegar aquí, verbatim (app.py:3538-3565) -->


@cobros_bp.route('/cobros/generar-mensualidades', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def generar_mensualidades():
    # <-- pegar aquí, verbatim (app.py:4036-4147) -->


@cobros_bp.route('/cobros/recordatorios-vencimiento', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'CONTADOR')
def recordatorios_vencimiento():
    # <-- pegar aquí, verbatim (app.py:4229-4264) -->
```

(Verify the exact import list — `generar_mensualidades` needs
`_cargo_duplicado`, already listed; check whether any of these 12
functions reference `IntegrityError` directly and add `from
sqlalchemy.exc import IntegrityError` if so — several do, per the
`except IntegrityError:` pattern documented throughout the audit.)

- [ ] **Step 2: Remove the moved routes from `app.py`**

Delete, in this order (top to bottom, so earlier deletions don't shift
the line numbers of later ones — **always delete from the bottom of the
file upward within a single task** to avoid this exact problem):
`app.py:4227-4264`, `app.py:4034-4147`, `app.py:3529-3565`,
`app.py:3480-3528`, `app.py:3456-3479`, `app.py:3413-3455`,
`app.py:3337-3412`, `app.py:3165-3260`, `app.py:2935-2945`,
`app.py:2844-2934`, `app.py:2809-2843`.

- [ ] **Step 3: Register the blueprint**

```python
    from rutas.cobros import cobros_bp
    app.register_blueprint(cobros_bp)
```

- [ ] **Step 4: Verify fix #2 survived the move byte-for-byte**

```bash
diff <(git show HEAD:app.py | sed -n '3337,3412p') <(sed -n '/^def registrar_pago/,/^def anular_pago\|^@cobros_bp.route(.\/pago/p' rutas/cobros.py | head -n -2)
```
This diff should show **only** whitespace/indentation-neutral
differences from the `@app.route`/`@cobros_bp.route` decorator line
itself and nothing else inside the function body. If the diff shows any
change inside the function body (the actual SQL-producing lines), stop
and re-paste from the git-tracked original rather than trying to patch
the discrepancy by hand.

Then explicitly:
```bash
grep -n "with_for_update()\|dialect.name != 'sqlite'" rutas/cobros.py
```
Expected: both lines present, inside `registrar_pago`.

- [ ] **Step 5: Rename endpoints everywhere**

```bash
for e in cobros nuevo_cargo becas_alumno desactivar_beca registrar_pago cancelar_cargo condonar_recargo anular_pago recibo_pago estado_cuenta generar_mensualidades recordatorios_vencimiento; do
  grep -rl "url_for('$e'" app.py templates/ | xargs -r sed -i "s/url_for('$e'/url_for('cobros.$e'/g"
done
grep -rn "url_for('cobros'\|url_for('nuevo_cargo'\|url_for('becas_alumno'\|url_for('desactivar_beca'\|url_for('registrar_pago'\|url_for('cancelar_cargo'\|url_for('condonar_recargo'\|url_for('anular_pago'\|url_for('recibo_pago'\|url_for('estado_cuenta'\|url_for('generar_mensualidades'\|url_for('recordatorios_vencimiento'" app.py templates/
```
Expected: no output. **This is the collision case flagged in Appendix
B**: the blueprint is named `cobros` AND one of its own routes' function
is also named `cobros` (`/alumno/<matricula>/cobros`). The rename turns
`url_for('cobros', matricula=...)` into `url_for('cobros.cobros',
matricula=...)` — correct and expected. Confirm by hand with `grep -n
"cobros\.cobros" templates/*.html app.py | head` that this specific
double-`cobros` form appears and looks right, since it's the one
rename in this whole plan that reads unusually and is worth eyeballing.

- [ ] **Step 6: Update `tests/test_inventario_rutas.py`**

```python
('/alumno/<matricula>/becas', ('GET', 'POST'), 'cobros.becas_alumno', ('DIRECTIVO', 'CONTADOR')),
('/alumno/<matricula>/cobros', ('GET',), 'cobros.cobros', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
('/alumno/<matricula>/cobros/nuevo', ('POST',), 'cobros.nuevo_cargo', ('DIRECTIVO', 'CONTADOR')),
('/alumno/<matricula>/estado-cuenta', ('GET',), 'cobros.estado_cuenta', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
('/becas/<int:beca_id>/desactivar', ('POST',), 'cobros.desactivar_beca', ('DIRECTIVO', 'CONTADOR')),
('/cobro/<int:cargo_id>/cancelar', ('POST',), 'cobros.cancelar_cargo', ('DIRECTIVO', 'CONTADOR')),
('/cobro/<int:cargo_id>/condonar-recargo', ('POST',), 'cobros.condonar_recargo', ('DIRECTIVO',)),
('/cobro/<int:cargo_id>/pagar', ('POST',), 'cobros.registrar_pago', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
('/cobros/generar-mensualidades', ('GET', 'POST'), 'cobros.generar_mensualidades', ('DIRECTIVO', 'CONTADOR')),
('/cobros/recordatorios-vencimiento', ('GET', 'POST'), 'cobros.recordatorios_vencimiento', ('DIRECTIVO', 'CONTADOR')),
('/pago/<int:pago_id>/anular', ('POST',), 'cobros.anular_pago', ('DIRECTIVO', 'CONTADOR')),
('/pago/<int:pago_id>/recibo', ('GET',), 'cobros.recibo_pago', ('DIRECTIVO', 'ADMINISTRATIVO', 'CONTADOR')),
```

- [ ] **Step 7: Run the full suite, with mandatory focus on the concurrency test**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

Run specifically:
`./venv/bin/python -m pytest tests/test_concurrencia_pagos.py tests/test_cargos_duplicados.py tests/test_cargos_saldo_cero.py tests/test_correo_timeout.py tests/test_errores_smtp_no_expuestos.py -v`
Expected: all pass unmodified — this is the full set of tests tied to
fixes #2, #3, #6 and the SMTP-leak fix, all of which route through this
one blueprint.

- [ ] **Step 8: Commit**

```bash
git add rutas/cobros.py app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover el sistema de cobros a rutas/cobros.py (fix #2 verificado intacto)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 13: `rutas/alumnos.py`

**Endpoint renames in this task:** `index` → `alumnos.index`, `buscar` →
`alumnos.buscar`, `importar_alumnos` → `alumnos.importar_alumnos`,
`plantilla_importacion` → `alumnos.plantilla_importacion`,
`ver_expediente` → `alumnos.ver_expediente`, `ficha_inscripcion` →
`alumnos.ficha_inscripcion`, `cambiar_estatus` → `alumnos.cambiar_estatus`,
`avanzar_cuatrimestre` → `alumnos.avanzar_cuatrimestre`,
`avanzar_cuatrimestre_lote` → `alumnos.avanzar_cuatrimestre_lote`.

**`ver_expediente` and `ficha_inscripcion` carry fix #1 (the role guard
added in the earliest step of the audit) — verify their decorator
survives unchanged, same discipline as Task 12's fix #2 check.**

**Files:**
- Create: `rutas/alumnos.py`
- Modify: `app.py`, `tests/test_inventario_rutas.py` (9 rows), `templates/*.html`

**Interfaces:**
- Consumes: `extensiones.db`, `modelos.Alumno, EstatusAlumno, PlanEstudio, HistorialEstatus`, `utilidades.seguridad.rol_requerido`, `utilidades.fechas.periodo_escolar_actual`, `utilidades.paginacion._paginar_lista, ALUMNOS_POR_PAGINA`, `servicios.alumnos.calcular_estadisticas_alumnos, _matriculas_con_adeudo`, `servicios.academico._avanzar_cuatrimestre, _max_periodos`, `openpyxl`.
- Produces: `alumnos_bp`.

- [ ] **Step 1: Create `rutas/alumnos.py`**

Move verbatim from `app.py`:
- Lines 1746–1823 (`index`)
- Lines 1824–1847 (`buscar`)
- Lines 2084–2112 (`COLUMNAS_IMPORTACION_ALUMNOS` constant)
- Lines 2113–2114 (`plantilla_importacion` decorator line — actually
  starts at 2113 per earlier scan; verify exact boundary by reading
  `app.py` at that point before pasting)
- Lines 2115–2164 (`plantilla_importacion` body)
- Lines 2165–2356 (`importar_alumnos`)
- Lines 2589–2633 (`ver_expediente` — **fix #1, verify decorator**)
- Lines 2634–2657 (`ficha_inscripcion` — **fix #1, verify decorator**)
- Lines 2658–2764 (`cambiar_estatus`)
- Lines 2765–2801 (`avanzar_cuatrimestre`)
- Lines 4148–4226 (`avanzar_cuatrimestre_lote`)

```python
"""
Búsqueda de alumnos, alta individual/masiva, expediente, ficha de
inscripción, cambio de estatus y avance de cuatrimestre.

ver_expediente y ficha_inscripcion llevan @rol_requerido('DIRECTIVO',
'ADMINISTRATIVO', 'CAPTURADOR') -- fix #1 de la auditoría de producción
(antes solo tenían @login_required, sin restricción de rol). No
debilitar este decorador al mover estas dos rutas.
"""

import io

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from flask import Blueprint, render_template, request, flash, redirect, url_for, send_file
from sqlalchemy.exc import IntegrityError

from extensiones import db
from modelos import (
    Alumno, EstatusAlumno, PlanEstudio, HistorialEstatus,
    Materia, DocumentoAlumno, TipoDocumento, TurnoAlumno, ModalidadEstudio,
)
from utilidades.seguridad import rol_requerido
from utilidades.fechas import periodo_escolar_actual, ahora_utc
from utilidades.paginacion import _paginar_lista, ALUMNOS_POR_PAGINA
from servicios.alumnos import calcular_estadisticas_alumnos, _matriculas_con_adeudo
from servicios.academico import _avanzar_cuatrimestre, _max_periodos, _generar_carga_academica
from servicios.matriculas import crear_alumno_generando_matricula
from servicios.cobros import _generar_cargos_de_inscripcion

alumnos_bp = Blueprint('alumnos', __name__)


@alumnos_bp.route('/')
@rol_requerido(...)  # <-- copiar el decorador exacto de app.py:1746-1747, NO adivinarlo
def index():
    # <-- pegar aquí, verbatim (app.py:1748-1823) -->


@alumnos_bp.route('/buscar', methods=['POST'])
@rol_requerido(...)  # <-- copiar el decorador exacto de app.py:1824-1825
def buscar():
    # <-- pegar aquí, verbatim (app.py:1826-1847) -->


# (columna, es_obligatoria, descripción para la hoja de ayuda de la plantilla)
COLUMNAS_IMPORTACION_ALUMNOS = [
    # <-- pegar aquí, verbatim (app.py:2084-2111) -->
]


@alumnos_bp.route('/alumnos/importar/plantilla')
@rol_requerido('DIRECTIVO')
def plantilla_importacion():
    # <-- pegar aquí, verbatim (app.py:2115-2164) -->


@alumnos_bp.route('/alumnos/importar', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO')
def importar_alumnos():
    # <-- pegar aquí, verbatim (app.py:2167-2356) -->


@alumnos_bp.route('/alumno/<matricula>/expediente')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def ver_expediente(matricula):
    # <-- pegar aquí, verbatim (app.py:2591-2633) -- VERIFICAR fix #1 -->


@alumnos_bp.route('/alumno/<matricula>/ficha')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def ficha_inscripcion(matricula):
    # <-- pegar aquí, verbatim (app.py:2636-2657) -- VERIFICAR fix #1 -->


@alumnos_bp.route('/alumno/<matricula>/cambiar-estatus', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def cambiar_estatus(matricula):
    # <-- pegar aquí, verbatim (app.py:2660-2764) -->


@alumnos_bp.route('/alumno/<matricula>/avanzar-cuatrimestre', methods=['POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def avanzar_cuatrimestre(matricula):
    # <-- pegar aquí, verbatim (app.py:2767-2801) -->


@alumnos_bp.route('/alumnos/avanzar-cuatrimestre-lote', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO')
def avanzar_cuatrimestre_lote():
    # <-- pegar aquí, verbatim (app.py:4150-4226) -->
```

`index` and `buscar` show `None` for roles in the Task 0 inventory
(they use plain `@login_required`, not `rol_requerido`) — copy whatever
decorator `app.py:1746-1747` and `app.py:1824-1825` actually show, not
`@rol_requerido(...)`; the placeholder above is a reminder to check, not
literal code to paste.

- [ ] **Step 2: Remove the moved routes from `app.py`**

Delete, bottom-to-top within this task: `app.py:4148-4226`,
`app.py:2589-2801` (covers `ver_expediente`, `ficha_inscripcion`,
`cambiar_estatus`, `avanzar_cuatrimestre` in one contiguous block —
confirm no other code sits between them before deleting as one range),
`app.py:2074-2356` (covers the module comment header,
`COLUMNAS_IMPORTACION_ALUMNOS`, `plantilla_importacion`,
`importar_alumnos`), `app.py:1746-1847` (`index`, `buscar`).

- [ ] **Step 3: Register the blueprint**

```python
    from rutas.alumnos import alumnos_bp
    app.register_blueprint(alumnos_bp)
```

- [ ] **Step 4: Verify fix #1 survived the move**

```bash
grep -n "@rol_requerido" rutas/alumnos.py | grep -B1 "def ver_expediente\|def ficha_inscripcion" 2>/dev/null
grep -n -A1 "^def ver_expediente\|^def ficha_inscripcion" rutas/alumnos.py
```
Confirm both show `@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO',
'CAPTURADOR')` immediately above the `def` line — not merely
`@login_required` alone, which was the pre-fix #1 state this whole audit
started from.

- [ ] **Step 5: Rename endpoints everywhere**

```bash
for e in index buscar importar_alumnos plantilla_importacion ver_expediente ficha_inscripcion cambiar_estatus avanzar_cuatrimestre avanzar_cuatrimestre_lote; do
  grep -rl "url_for('$e'" app.py templates/ | xargs -r sed -i "s/url_for('$e'/url_for('alumnos.$e'/g"
done
grep -rn "url_for('index'\|url_for('buscar'\|url_for('importar_alumnos'\|url_for('plantilla_importacion'\|url_for('ver_expediente'\|url_for('ficha_inscripcion'\|url_for('cambiar_estatus'\|url_for('avanzar_cuatrimestre'\|url_for('avanzar_cuatrimestre_lote'" app.py templates/
```
Expected: no output. **Careful with `avanzar_cuatrimestre` vs
`avanzar_cuatrimestre_lote`:** run the loop with `avanzar_cuatrimestre`
listed **before** `avanzar_cuatrimestre_lote` doesn't matter for
correctness (the trailing-quote match makes them non-overlapping
regardless of order — `url_for('avanzar_cuatrimestre'` never matches
inside `url_for('avanzar_cuatrimestre_lote'` because character 24 differs,
`'` vs `_`), but verify with the grep anyway.

- [ ] **Step 6: Update `tests/test_inventario_rutas.py`**

```python
('/', ('GET',), 'alumnos.index', None),
('/alumno/<matricula>/avanzar-cuatrimestre', ('POST',), 'alumnos.avanzar_cuatrimestre', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/alumno/<matricula>/cambiar-estatus', ('POST',), 'alumnos.cambiar_estatus', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/alumno/<matricula>/expediente', ('GET',), 'alumnos.ver_expediente', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/alumno/<matricula>/ficha', ('GET',), 'alumnos.ficha_inscripcion', ('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')),
('/alumnos/avanzar-cuatrimestre-lote', ('GET', 'POST'), 'alumnos.avanzar_cuatrimestre_lote', ('DIRECTIVO', 'ADMINISTRATIVO')),
('/alumnos/importar', ('GET', 'POST'), 'alumnos.importar_alumnos', ('DIRECTIVO',)),
('/alumnos/importar/plantilla', ('GET',), 'alumnos.plantilla_importacion', ('DIRECTIVO',)),
('/buscar', ('POST',), 'alumnos.buscar', None),
```

- [ ] **Step 7: Run the full suite, with mandatory focus on fix #1's test**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`.

Run specifically:
`./venv/bin/python -m pytest tests/test_autenticacion_roles.py tests/test_matricula.py tests/test_registro_publico.py -v`
Expected: all pass — `test_autenticacion_roles.py` is where fix #1's
parametrized role tests live (DIRECTIVO/ADMINISTRATIVO/CAPTURADOR allowed,
CONTADOR denied, no session redirected).

- [ ] **Step 8: Commit**

```bash
git add rutas/alumnos.py app.py tests/test_inventario_rutas.py templates/
git commit -m "refactor: mover búsqueda, alta y expediente de alumnos a rutas/alumnos.py (fix #1 verificado intacto)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Task 14: Clean up `app.py` + final verification

**Files:**
- Modify: `app.py`

**Interfaces:**
- Consumes: everything produced by Tasks 1–13.
- Produces: the final `app.py` — a thin assembler under ~400 lines, per
  the spec's acceptance criterion #7.

- [ ] **Step 1: Read the current `app.py` top to bottom**

At this point `app.py` should contain, in order: module docstring,
imports (from `extensiones`, `modelos`, `utilidades`, `servicios`, and
the handful of `flask`/`flask_login`/stdlib imports still needed
directly — `render_template`, `request`, `flash`, `redirect`, `url_for`
for the error handlers; `urlparse` for the 413 handler; `os`, `logging`,
`RotatingFileHandler` for `create_app()`), `create_app()` (with all 9
`register_blueprint` calls), the 5 `errorhandler` functions, the
`inyectar_configuracion_institucion` context processor, `app =
create_app(...)`, and the `if __name__ == '__main__':` block. Confirm no
leftover dead code (an orphaned helper nobody imports, a duplicate
import) remains — `grep -c "^def \|^class "  app.py` should show a very
small number (only whatever helper functions, if any, were deliberately
left because `create_app()` itself needs them and nothing else does).

- [ ] **Step 2: Add the compatibility re-export block**

At the very end of `app.py`, immediately before `if __name__ ==
'__main__':`, add:

```python
# ---------------------------------------------------------------------------
# COMPATIBILIDAD: re-exports para tests/, seed.py y crear_admin.py
# ---------------------------------------------------------------------------
# Estos 34 nombres se importaban antes directamente de app.py (cuando
# TODO vivía aquí). Tras Plan 2 (organizar rutas en blueprints), viven en
# extensiones/modelos/utilidades/servicios -- este bloque evita tener que
# tocar ninguna de las 166 pruebas existentes, seed.py ni crear_admin.py.
__all__ = [
    'app', 'db', 'limiter', 'mail',
    'Alumno', 'Calificacion', 'Cargo', 'ConceptoCobro', 'ConfiguracionCobros',
    'ConfiguracionInstitucion', 'ContadorFolio', 'DocumentoAlumno',
    'HistorialCalificacion', 'InscripcionMateria', 'Materia', 'Pago', 'PlanEstudio',
    'Usuario',
    'EstatusAlumno', 'EstatusCargo', 'MetodoPago', 'RolUsuario', 'TipoRecargo',
    'DIAS_AVISO_VENCIMIENTO',
    'a_local', 'ahora_utc', 'hoy_local', 'periodo_escolar_actual',
    'rango_utc_del_dia', 'siguiente_folio', 'generar_matricula',
    'crear_alumno_generando_matricula', '_vencimiento_dia_10_sugerido',
    '_calcular_reporte_cobros_del_dia',
]
```

Every name in that list must already be in scope in `app.py` via the
imports added across Tasks 1–4 (nothing new to import here — this step
only adds the `__all__` declaration documenting the compatibility
contract explicitly). Run:

```bash
./venv/bin/python -c "
import ast
tree = ast.parse(open('app.py').read())
declarados = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            declarados.add(alias.asname or alias.name)
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) for t in node.targets):
        for t in node.targets:
            if isinstance(t, ast.Name):
                declarados.add(t.id)
    if isinstance(node, (ast.FunctionDef,)):
        declarados.add(node.name)
esperados = ['app','db','limiter','mail','Alumno','Calificacion','Cargo','ConceptoCobro','ConfiguracionCobros','ConfiguracionInstitucion','ContadorFolio','DocumentoAlumno','HistorialCalificacion','InscripcionMateria','Materia','Pago','PlanEstudio','Usuario','EstatusAlumno','EstatusCargo','MetodoPago','RolUsuario','TipoRecargo','DIAS_AVISO_VENCIMIENTO','a_local','ahora_utc','hoy_local','periodo_escolar_actual','rango_utc_del_dia','siguiente_folio','generar_matricula','crear_alumno_generando_matricula','_vencimiento_dia_10_sugerido','_calcular_reporte_cobros_del_dia']
faltan = [n for n in esperados if n not in declarados]
print('Faltan:', faltan if faltan else 'ninguno')
"
```
Expected: `Faltan: ninguno`.

- [ ] **Step 3: Update the stale comment about blueprint registration**

Confirm the comment block that originally said "Aquí se registrarán los
Blueprints en pasos posteriores" was fully replaced (across Tasks 5–13)
by the 9 real `register_blueprint` calls, in this order: `auth_bp`,
`usuarios_bp`, `registro_bp`, `documentos_bp`, `academico_bp`,
`configuracion_bp`, `reportes_bp`, `cobros_bp`, `alumnos_bp`. Order
doesn't affect behavior (Flask blueprints don't have registration-order
dependencies here — no two blueprints share a URL prefix), but keep it
consistent with the order tasks executed them, for readability.

- [ ] **Step 4: `flask db migrate` sanity check, one more time**

Run: `./venv/bin/flask db migrate -m "verificacion-final-plan-2"`
Expected: **no changes detected**. Delete the generated migration file
if one was created before confirming "no changes" (same discipline as
Task 2 Step 7).

- [ ] **Step 5: `seed.py` and `crear_admin.py` still import cleanly**

Run: `./venv/bin/python -c "import ast; ast.parse(open('seed.py').read()); print('seed.py: sintaxis OK')"`
Run: `./venv/bin/python -c "import ast; ast.parse(open('crear_admin.py').read()); print('crear_admin.py: sintaxis OK')"`
Then, in an app context but **without touching any database** (do not
run `sembrar()` or `crear_admin()` themselves — only import the module
top-level, which triggers `from app import app, db, ...` and nothing
else):
```bash
./venv/bin/python -c "
import os
os.environ['FLASK_ENV'] = 'testing'
import importlib
importlib.import_module('seed')
importlib.import_module('crear_admin')
print('Ambos módulos importan sin error.')
"
```
Expected: `Ambos módulos importan sin error.` — if either raises
`ImportError`, a name from Appendix A is missing from `app.py`; go back
and find where it should have been re-exported.

- [ ] **Step 6: Confirm the file-size acceptance criterion**

Run: `wc -l app.py rutas/*.py servicios/*.py utilidades/*.py modelos/*.py extensiones.py`
Expected: `app.py` under ~400 lines; no single file over ~600.

- [ ] **Step 7: Confirm no data file changed, and remove the line-number baseline**

Run: `git status --short instance/`
Expected: no output (nothing tracked or modified under `instance/`).

Remove the frozen baseline created in Task 1, Step 0 — it was never
committed and has no purpose past this point:

```bash
rm app.py.baseline-plan2
git status --short | grep baseline-plan2
```
Expected: the `rm` succeeds; the `grep` afterward returns nothing (confirms it wasn't accidentally tracked).

- [ ] **Step 8: Full suite, one final time**

Run: `./venv/bin/python -m pytest -q`
Expected: `169 passed`, zero warnings, zero skips beyond what already
existed before this plan (there were none).

- [ ] **Step 9: Manual smoke test — app actually starts**

Run: `./venv/bin/flask run &` (background), wait ~2s, then:
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/login
```
Expected: `200`. Then stop the dev server (`kill %1` or equivalent).

- [ ] **Step 10: Commit**

```bash
git add app.py
git commit -m "refactor: limpiar app.py final (assembler + re-exports de compatibilidad)

app.py pasó de 5,028 a <N> líneas. Las 51 rutas ahora viven en 9
blueprints (rutas/), 17 modelos en modelos/, la lógica de negocio en
servicios/, y las utilidades sin estado en utilidades/. Ninguna URL
pública cambió; ninguna de las 166 pruebas originales se modificó;
los fixes #1-#9 de la auditoría de producción se verificaron intactos
en los pasos donde se movió su código (fix #1 en Task 13, fix #2 en
Task 12, el resto en sus tareas correspondientes).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UHqUrM1aHJmV2cGCYaDbx4"
```

---

## Self-review notes (already applied above)

- **Spec coverage:** every spec §5 blueprint has its own task (5–13);
  §3's `modelos/`/`utilidades/` prerequisite is Tasks 2–3; §7's 34
  re-exports are Appendix A + Task 14; §8's two safety-net tests are
  Task 0; §6 (what stays in `app.py`) is verified in Task 14 Step 1;
  §9's step order matches Tasks 0–14 one-to-one; §10's acceptance
  criteria map to Task 14 Steps 2, 4–9.
- **Forward-reference bug found and fixed (not just flagged):** verified
  directly against `app.py` that `Alumno.fecha_registro` (line 465) uses
  `ahora_utc` as a column default. My first draft only left a conditional
  hedge here ("check and reorder if needed"); confirming the check found
  the dependency IS real, so I restructured Task 2 to pull
  `utilidades/fechas.py`'s core functions forward into its own Step 0,
  before `modelos/academico.py` is created, and adjusted Task 3 to append
  to that file rather than recreate it.
- **Second bug found in the same area, by tracing the consequence
  through:** `app.py:1487` (`login()`, which doesn't move until Task 5)
  calls `ahora_utc()` directly. Deleting `ahora_utc`'s definition in
  Task 2 Step 0 without also re-adding the import to `app.py` in that
  same step would have made Task 2's own "run full suite" step fail with
  `NameError` on any test that hits `/login` — a bug that would have
  surfaced immediately if this plan were executed, but is better caught
  here. Fixed by adding the `utilidades.fechas` import to `app.py`
  directly in Task 2 Step 0, and having Task 3 Step 8 extend that same
  import statement (not duplicate it) once the filter functions exist.
- **Structural bug found: line numbers go stale mid-plan.** Nearly every
  task cites `app.py:X-Y` ranges computed once, against the file as it
  stood before Task 1. But each task's own deletions shift every line
  number after the deleted range — meaning Task 2's internal steps
  (Step 0 deletes lines 30, 33-156; Steps 1-3 then cite lines 239-1135)
  would already be reading the wrong lines by the time Step 1 runs. Fixed
  by adding a frozen-snapshot baseline (`app.py.baseline-plan2`, created
  in Task 1 Step 0, removed in Task 14 Step 7): every line citation in
  this plan resolves against that frozen copy, never against the live,
  currently-mutating `app.py`. Deletions still target the live file, but
  by matching the actual code (function/class signature), not by
  trusting a line number.
- **Deletion-order fix applied:** Task 12 and Task 13 both delete
  multiple non-contiguous line ranges from `app.py` in one task; added
  an explicit instruction to delete bottom-to-top so earlier deletions
  don't shift the line numbers the later deletions rely on.
- **Type/name consistency check:** `MOTIVO_GENERICO_FALLO_CORREO`,
  `DIAS_AVISO_VENCIMIENTO`, `_vencimiento_dia_10_sugerido`,
  `_calcular_reporte_cobros_del_dia` are spelled identically everywhere
  they appear (Appendix A, Task 4, Task 14) — verified by grep across
  this document while writing it.
- **Placeholder scan:** the only bracketed markers in this plan are the
  `<-- pegar aquí ... -->` move-instructions, which point at verified,
  specific `app.py` line ranges (never "similar to Task N" or "add
  appropriate error handling") — these are mechanical copy instructions
  for existing, unmodified code, not unwritten logic.
