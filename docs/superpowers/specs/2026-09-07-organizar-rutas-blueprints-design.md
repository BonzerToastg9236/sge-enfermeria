# Plan 2 — Organizar las rutas de `app.py` en blueprints

**Fecha:** 2026-09-07
**Proyecto:** `~/Documentos/sge-enfermeria`
**Estado:** spec para revisión — **no implementado**

---

## 1. Objetivo

Partir `app.py` (5,028 líneas) en módulos con una responsabilidad clara
cada uno, moviendo sus 51 rutas a 9 blueprints registrados dentro de
`create_app()`.

Es una reorganización **estructural**: nada de lo que el sistema hace
cambia. No se toca ni una regla de negocio, ni una URL, ni un dato.

### Por qué ahora

1. `app.py` mezcla hoy configuración, 17 modelos, ~30 funciones auxiliares
   y 51 rutas. Cualquier cambio obliga a navegar un archivo de 5,000
   líneas, y es lo que hace lenta y riesgosa cada corrección.
2. El propio código ya lo anticipaba: `create_app()` tiene el hueco
   reservado con el comentario *"Aquí se registrarán los Blueprints en
   pasos posteriores"*, incluyendo los nombres `registro_bp` y `admin_bp`.
3. **Desbloquea un pendiente real.** Hoy las rutas cuelgan del objeto
   `app` global creado al importar el módulo, y por eso
   `tests/conftest.py` no puede usar `create_app('testing')` (lo
   documenta en su propio encabezado: esa instancia "nunca tendría
   ninguna ruta registrada encima"). Esa limitación es justamente lo que
   impide montar la infraestructura de pruebas contra PostgreSQL descrita
   en `deploy/PENDIENTES_PRODUCCION.md` §1. Con los blueprints
   registrados dentro de `create_app()`, la limitación desaparece.

---

## 2. Restricciones (acordadas, no negociables)

1. **No cambiar las URLs públicas existentes.** Cambian los nombres de
   endpoint, nunca las rutas. Ningún enlace ni marcador se rompe.
2. **No modificar la lógica de negocio.** El cuerpo de cada función se
   mueve tal cual; sólo cambian los `import` y los `url_for` internos.
3. **No modificar datos existentes.** Ningún script toca
   `instance/sge_dev.db` ni ninguna base real.
4. **No deshacer los fixes #1–#9** de la auditoría de producción.
5. **Compatibilidad total con las 166 pruebas actuales**: ninguna se
   modifica.
6. **Mantener los re-exports desde `app.py`** (§7).
7. **Ejecutar la suite completa después de cada paso lógico**; un paso no
   se da por bueno si deja una sola prueba en rojo.
8. Nada de refactorización oportunista: no se "mejora de paso" ningún
   cuerpo de función.

---

## 3. Estado actual (medido, no estimado)

| Métrica | Valor | Cómo se obtuvo |
|---|---|---|
| Líneas de `app.py` | 5,028 | `wc -l` |
| Rutas registradas | **51** | `app.url_map`, excluyendo `static` |
| Ocurrencias de `@app.route` | 52 | `grep` — una es un **ejemplo dentro del docstring** de `rol_requerido` (`app.py:1448`), no un decorador |
| URLs distintas | 50 | `/boletas/importar` la comparten dos endpoints (GET y POST) |
| Modelos | 17 | + 8 enums |
| Plantillas | 31 | `ls templates` |
| `url_for` en plantillas | 98 (~51 endpoints) | `grep` |
| `url_for` en `app.py` | 65 (17 endpoints) | `grep` |
| `url_for` en pruebas | **0** | las pruebas usan URLs literales |
| Nombres importados desde `app` | **34** | análisis AST de `tests/`, `seed.py`, `crear_admin.py` |
| Pruebas | 166 en verde | `pytest` |

Dos consecuencias importantes de esa tabla:

- Como las pruebas no usan `url_for`, **renombrar endpoints no puede
  romperlas** — pero tampoco lo detectan. Ese hueco es lo que cubre la
  red de seguridad del §8.
- Como importan 34 nombres desde `app`, `app.py` debe seguir
  exportándolos todos.

---

## 4. Arquitectura destino

```
app.py                  Ensamblador: create_app(), error handlers,
                        context processor, registro de filtros y
                        blueprints, y re-exports de compatibilidad
config.py               (sin cambios)
extensiones.py          db, migrate, login_manager, csrf, limiter, mail
                        + _ConexionSMTPConTimeout y _CorreoConTimeout
                        (viven aquí, no en utilidades/, porque `mail` es
                        una instancia de _CorreoConTimeout y extensiones
                        no puede importar de utilidades sin invertir la
                        regla de dependencias)
modelos/
    __init__.py         re-exporta todos los modelos y enums
    usuarios.py         RolUsuario, Usuario
    academico.py        PlanEstudio, Materia, Alumno, DocumentoAlumno,
                        Calificacion, HistorialEstatus,
                        HistorialCalificacion, InscripcionMateria,
                        TipoDocumento, EstatusAlumno, TurnoAlumno,
                        ModalidadEstudio
    cobros.py           ConceptoCobro, Beca, Cargo, Pago, ContadorFolio,
                        ConfiguracionCobros, ConfiguracionInstitucion,
                        EstatusCargo, MetodoPago, TipoRecargo,
                        TipoDescuentoBeca
utilidades/
    fechas.py           ahora_utc, _zona_horaria, hoy_local, a_local,
                        rango_utc_del_dia, periodo_escolar_actual + los
                        4 filtros de plantilla
    seguridad.py        rol_requerido, es_url_segura, load_user
    archivos.py         extension_permitida,
                        contenido_coincide_con_extension,
                        FIRMAS_POR_EXTENSION
    folios.py           siguiente_folio
    paginacion.py       _paginar_lista
servicios/
    matriculas.py       generar_matricula,
                        crear_alumno_generando_matricula
    alumnos.py          _matriculas_con_adeudo,
                        calcular_estadisticas_alumnos
    cobros.py           _cargo_duplicado, _meses_del_cuatrimestre_actual,
                        _monto_mensualidad_con_beca,
                        _generar_cargos_de_periodo,
                        _generar_cargos_de_inscripcion,
                        _generar_cargos_de_reinscripcion,
                        _vencimiento_dia_10_sugerido
    academico.py        _max_periodos, _generar_carga_academica,
                        _avanzar_cuatrimestre,
                        _registrar_historial_calificacion,
                        _siguiente_numero_acta
    reportes.py         _calcular_reporte_cobros_del_dia,
                        _calcular_cartera_vencida, _rango_ultimos_n_meses,
                        _calcular_dashboard_cobros
    correo.py           enviar_comprobante_pago,
                        enviar_recordatorio_vencimiento,
                        MOTIVO_GENERICO_FALLO_CORREO,
                        DIAS_AVISO_VENCIMIENTO
rutas/
    auth.py  usuarios.py  registro.py  alumnos.py  documentos.py
    cobros.py  reportes.py  academico.py  configuracion.py
```

### Regla de dependencias

```
config  →  extensiones  →  modelos  →  utilidades  →  servicios  →  rutas  →  app.py
```

Las flechas van en un solo sentido. **Ningún módulo importa de `app.py`**;
`app.py` importa de todos. Ésa es la razón por la que `modelos/` y
`utilidades/` entran en el alcance de un plan que se llama "organizar las
rutas": un blueprint no puede importar de `app.py` sin crear un ciclo,
porque `app.py` lo importa a él.

---

## 5. Los 9 blueprints y sus 51 rutas

Ningún `url_prefix`: las URLs se conservan exactamente como están hoy.
El endpoint pasa de `x` a `blueprint.x`.

### `auth` — 3 rutas

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/login` | GET, POST | `auth.login` | público |
| `/logout` | GET | `auth.logout` | `@login_required` |
| `/perfil` | GET, POST | `auth.perfil` | `@login_required` |

### `usuarios` — 3 rutas

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/usuarios` | GET | `usuarios.usuarios` | DIRECTIVO |
| `/usuarios/nuevo` | GET, POST | `usuarios.nuevo_usuario` | DIRECTIVO |
| `/usuarios/<int:user_id>/toggle` | POST | `usuarios.toggle_usuario` | DIRECTIVO |

### `registro` — 1 ruta

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/registro` | GET, POST | `registro.registro` | público, con rate limit |

Conserva `@limiter.limit('20 per hour;5 per minute', methods=['POST'])`
(fix #4). El `errorhandler(429)` que distingue este endpoint se queda a
nivel de app y pasa a comparar contra `'registro.registro'` (§6).

### `alumnos` — 9 rutas

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/` | GET | `alumnos.index` | `@login_required` |
| `/buscar` | POST | `alumnos.buscar` | `@login_required` |
| `/alumnos/importar` | GET, POST | `alumnos.importar_alumnos` | DIRECTIVO |
| `/alumnos/importar/plantilla` | GET | `alumnos.plantilla_importacion` | DIRECTIVO |
| `/alumno/<matricula>/expediente` | GET | `alumnos.ver_expediente` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/alumno/<matricula>/ficha` | GET | `alumnos.ficha_inscripcion` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/alumno/<matricula>/cambiar-estatus` | POST | `alumnos.cambiar_estatus` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/alumno/<matricula>/avanzar-cuatrimestre` | POST | `alumnos.avanzar_cuatrimestre` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/alumnos/avanzar-cuatrimestre-lote` | GET, POST | `alumnos.avanzar_cuatrimestre_lote` | DIRECTIVO, ADMINISTRATIVO |

`ver_expediente` y `ficha_inscripcion` conservan el `rol_requerido` del
**fix #1**.

### `documentos` — 3 rutas

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/alumno/<matricula>/documentos` | GET, POST | `documentos.documentos` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/alumno/<matricula>/documento/<int:doc_id>/ver` | GET | `documentos.ver_documento` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/documento/<int:doc_id>/eliminar` | POST | `documentos.eliminar_documento` | DIRECTIVO |

Conserva la validación por firma de archivo del **fix #7**.

### `cobros` — 12 rutas

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/alumno/<matricula>/cobros` | GET | `cobros.cobros` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/alumno/<matricula>/cobros/nuevo` | POST | `cobros.nuevo_cargo` | DIRECTIVO, CONTADOR |
| `/alumno/<matricula>/becas` | GET, POST | `cobros.becas_alumno` | DIRECTIVO, CONTADOR |
| `/becas/<int:beca_id>/desactivar` | POST | `cobros.desactivar_beca` | DIRECTIVO, CONTADOR |
| `/cobro/<int:cargo_id>/pagar` | POST | `cobros.registrar_pago` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/cobro/<int:cargo_id>/cancelar` | POST | `cobros.cancelar_cargo` | DIRECTIVO, CONTADOR |
| `/cobro/<int:cargo_id>/condonar-recargo` | POST | `cobros.condonar_recargo` | DIRECTIVO |
| `/pago/<int:pago_id>/anular` | POST | `cobros.anular_pago` | DIRECTIVO, CONTADOR |
| `/pago/<int:pago_id>/recibo` | GET | `cobros.recibo_pago` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/alumno/<matricula>/estado-cuenta` | GET | `cobros.estado_cuenta` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/cobros/generar-mensualidades` | GET, POST | `cobros.generar_mensualidades` | DIRECTIVO, CONTADOR |
| `/cobros/recordatorios-vencimiento` | GET, POST | `cobros.recordatorios_vencimiento` | DIRECTIVO, CONTADOR |

`registrar_pago` conserva **intacto** el bloqueo de fila del **fix #2**
(`with_for_update()` sobre `Cargo`, con la guarda de dialecto) y el orden
exacto de sus 5 sentencias SQL. Es la función más delicada del proyecto:
se mueve sin tocar una sola línea de su cuerpo.

### `reportes` — 6 rutas

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/reportes/cobros-del-dia` | GET | `reportes.reporte_cobros_del_dia` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/reportes/cobros-del-dia/exportar` | GET | `reportes.exportar_reporte_cobros_del_dia` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/reportes/cartera-vencida` | GET | `reportes.cartera_vencida` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/reportes/cartera-vencida/exportar` | GET | `reportes.exportar_cartera_vencida` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/cobros/dashboard` | GET | `reportes.dashboard_cobros` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |
| `/cobros/dashboard/exportar` | GET | `reportes.exportar_dashboard_cobros` | DIRECTIVO, ADMINISTRATIVO, CONTADOR |

Las URLs del dashboard viven bajo `/cobros/` y aun así el endpoint queda
en `reportes`: la URL manda, el agrupamiento es por responsabilidad.

### `academico` — 6 rutas

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/alumno/<matricula>/boleta` | GET, POST | `academico.boleta` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/boletas/importar` | GET | `academico.boletas_importar` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/boletas/importar` | POST | `academico.importar_boletas` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/boletas/importar/plantilla` | GET | `academico.plantilla_boletas` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/alumno/<matricula>/historial-calificaciones` | GET | `academico.historial_calificaciones` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |
| `/alumno/<matricula>/carga-academica` | GET | `academico.carga_academica` | DIRECTIVO, ADMINISTRATIVO, CAPTURADOR |

**Caso especial:** `/boletas/importar` está servida por dos funciones
distintas según el método (GET → `boletas_importar`, POST →
`importar_boletas`). Ambas deben quedar en el mismo blueprint y conservar
su separación por método; es el único punto del inventario donde dos
endpoints comparten URL.

### `configuracion` — 8 rutas

| URL | Métodos | Endpoint nuevo | Acceso |
|---|---|---|---|
| `/planes/mensualidades` | GET, POST | `configuracion.planes_mensualidades` | DIRECTIVO |
| `/planes/<int:plan_id>/materias` | GET, POST | `configuracion.gestionar_materias` | DIRECTIVO |
| `/materias/<int:materia_id>/eliminar` | POST | `configuracion.eliminar_materia` | DIRECTIVO |
| `/conceptos-cobro` | GET, POST | `configuracion.conceptos_cobro` | DIRECTIVO, CONTADOR |
| `/conceptos-cobro/<int:concepto_id>/editar-precio` | POST | `configuracion.editar_precio_concepto` | DIRECTIVO, CONTADOR |
| `/conceptos-cobro/<int:concepto_id>/toggle` | POST | `configuracion.toggle_concepto_cobro` | DIRECTIVO, CONTADOR |
| `/configuracion/institucion` | GET, POST | `configuracion.configuracion_institucion` | DIRECTIVO |
| `/configuracion/cobros` | GET, POST | `configuracion.configuracion_cobros` | DIRECTIVO, CONTADOR |

Aquí viven las dos únicas pantallas que escriben precios; conservan sus
roles exactos (Plan 1 de precios).

**Total: 3 + 3 + 1 + 9 + 3 + 12 + 6 + 6 + 8 = 51 rutas.**

---

## 6. Qué se queda en `app.py`

- `create_app()`: configuración, `ProxyFix`, `init_app` de las 6
  extensiones, registro de los 4 filtros de plantilla, logging, creación
  de `UPLOAD_FOLDER` y **registro de los 9 blueprints** (en el hueco que
  ya estaba reservado).
- Los 5 `errorhandler`: 403, 404, 413, 429, 500. Son de alcance de
  aplicación y no pertenecen a ningún blueprint.
- El `context_processor` `inyectar_configuracion_institucion`.
- El objeto `app = create_app(...)` a nivel de módulo (lo necesitan
  `conftest.py`, Gunicorn y `flask db`).
- Los re-exports del §7.

Tres referencias a endpoints que cambian dentro de `app.py` y deben
actualizarse:

| Dónde | Antes | Después |
|---|---|---|
| `create_app()` | `login_manager.login_view = 'login'` | `'auth.login'` |
| `errorhandler(429)` | `if request.endpoint == 'registro'` | `== 'registro.registro'` |
| `errorhandler(413)` | `urlparse(request.referrer).path` | sin cambio (usa la URL, no el endpoint) |

---

## 7. Compatibilidad: los 34 re-exports

`app.py` termina con un bloque explícito que re-exporta, con un comentario
que explica por qué existe. Son los 34 nombres que hoy importan
`tests/`, `seed.py` y `crear_admin.py`:

```
app, db, limiter, mail
Alumno, Calificacion, Cargo, ConceptoCobro, ConfiguracionCobros,
ConfiguracionInstitucion, ContadorFolio, DocumentoAlumno,
HistorialCalificacion, InscripcionMateria, Materia, Pago, PlanEstudio,
Usuario
EstatusAlumno, EstatusCargo, MetodoPago, RolUsuario, TipoRecargo
DIAS_AVISO_VENCIMIENTO
a_local, ahora_utc, hoy_local, periodo_escolar_actual,
rango_utc_del_dia, siguiente_folio, generar_matricula,
crear_alumno_generando_matricula, _vencimiento_dia_10_sugerido,
_calcular_reporte_cobros_del_dia
```

Se declararán también en `__all__`. **Ninguna de las 166 pruebas se
modifica**; si alguna necesitara cambiar, es señal de que el paso rompió
compatibilidad y hay que corregir el paso, no la prueba.

---

## 8. Red de seguridad (paso 0, antes de mover nada)

El riesgo dominante de este trabajo es un `url_for` que se quede con el
nombre viejo: no falla al arrancar, falla con `BuildError` cuando alguien
abre esa pantalla — y puede ser una pantalla que ninguna prueba visita.
Dos pruebas nuevas lo convierten en un fallo inmediato y, de paso, cubren
huecos que ya existen hoy.

### 8.1 `tests/test_integridad_urls.py`

Escanea las 31 plantillas, extrae cada `url_for('X')` con una expresión
regular y afirma que `X` existe en `app.url_map`. Falla nombrando la
plantilla, la línea y el endpoint inexistente.

Limitación conocida, que se documenta en el propio archivo: sólo detecta
endpoints escritos como literal. Un `url_for(variable)` no se puede
verificar estáticamente. Se hará un inventario de esos casos; hoy no
parece haber ninguno, y si aparece se listará como excepción explícita.

### 8.2 `tests/test_inventario_rutas.py`

Fija el inventario completo: las 51 rutas con su URL, sus métodos HTTP y
su control de acceso, comparado contra una tabla escrita a mano en el
propio test. Falla si una ruta cambia de URL, gana o pierde métodos,
cambia de roles, o si aparece una ruta nueva sin declararla.

Esto cierra además el pendiente reportado en la auditoría: *"no hay
prueba que falle si una ruta nueva nace sin control de acceso"*.

Para leer los roles hay que recorrer los closures anidados: `rol_requerido`
envuelve internamente a `@login_required`, así que la tupla de roles no
está en el primer nivel. Ya está resuelto (se hizo durante la auditoría)
y se reutiliza esa función.

**Ambas pruebas se escriben y se ven pasar contra el `app.py` actual**,
antes de mover una sola línea. Son el detector, no el resultado.

---

## 9. Plan de ejecución

Cada paso termina con la suite completa en verde. Si un paso rompe algo,
se revierte **ese** paso y se investiga antes de seguir.

| Paso | Qué | Verificación adicional |
|---|---|---|
| 0 | Red de seguridad (§8) | 166 + 2 pruebas nuevas en verde |
| 1 | `extensiones.py` | `flask db current` responde |
| 2 | `modelos/` | **`flask db migrate` no detecta ninguna diferencia** |
| 3 | `utilidades/` | — |
| 4 | `servicios/` | — |
| 5 | `rutas/auth.py` | + `login_manager.login_view` |
| 6 | `rutas/usuarios.py` | — |
| 7 | `rutas/registro.py` | + `errorhandler(429)` |
| 8 | `rutas/documentos.py` | — |
| 9 | `rutas/academico.py` | ojo con la URL compartida |
| 10 | `rutas/configuracion.py` | — |
| 11 | `rutas/reportes.py` | — |
| 12 | `rutas/cobros.py` | el paso más delicado (fix #2) |
| 13 | `rutas/alumnos.py` | — |
| 14 | Limpieza de `app.py` + verificación final | §10 |

El orden de los blueprints va de menor a mayor riesgo: `auth` (3 rutas
simples) primero, `cobros` y `alumnos` (los más grandes y los que
concentran los fixes) al final, cuando el patrón ya esté rodado.

En cada paso de blueprint se mueven **también** los `url_for` de las
plantillas que apuntan a sus endpoints. La prueba 8.1 verifica que no
quedó ninguno a medias.

---

## 10. Criterios de aceptación

1. `pytest` → **168 pruebas en verde** (166 actuales + 2 nuevas), sin
   modificar ninguna de las 166.
2. `app.url_map` contiene exactamente las mismas 51 URLs con los mismos
   métodos que hoy (lo afirma la prueba 8.2).
3. Cada ruta conserva exactamente el mismo control de acceso.
4. `flask db migrate` no detecta ninguna diferencia de esquema.
5. `seed.py` y `crear_admin.py` importan sin error (**sólo import; no se
   ejecutan contra ninguna base**).
6. La app arranca en desarrollo y responde en `/login`.
7. `app.py` queda por debajo de ~400 líneas y ningún módulo nuevo pasa de
   ~600.
8. `git diff` no muestra ni una línea de lógica de negocio alterada: los
   cuerpos de las 51 vistas se mueven íntegros.
9. Ningún archivo bajo `instance/` cambia.

---

## 11. Riesgos

| Riesgo | Probabilidad | Mitigación |
|---|---|---|
| `url_for` con el nombre viejo en una plantilla poco visitada | Alta | Prueba 8.1, ejecutada en cada paso |
| Import circular al mover modelos/utilidades | Media | Regla de dependencias unidireccional (§4); los pasos 1–4 van antes que cualquier ruta |
| Alembic deja de ver los modelos y propone borrar tablas | Media | `modelos/__init__.py` importa todos los submódulos; el paso 2 se valida con `flask db migrate` |
| Perder un decorador de rol al mover una vista | Media | Prueba 8.2 compara los roles ruta por ruta |
| Romper el bloqueo de fila del fix #2 | Baja | Se mueve sin editar; verificación explícita de `with_for_update()` y del orden de sentencias al cerrar el paso 12 |
| `conftest.py` deja de encontrar un nombre | Media | Los 34 re-exports del §7 se declaran en el paso 1 y se verifican en cada paso |

---

## 12. Fuera de alcance

- Cambiar URLs, añadir `url_prefix` o versionar rutas.
- Tocar lógica de negocio, modelos, esquema o migraciones existentes.
- Migrar `conftest.py` a `create_app('testing')`. Este trabajo lo
  **habilita**, pero hacerlo es un plan aparte (junto con las pruebas
  contra PostgreSQL de `PENDIENTES_PRODUCCION.md` §1).
- El rediseño visual (Plan 3).
- Multi-tenancy: sigue sin existir y este plan no la introduce.
- Cualquier modificación de datos.
