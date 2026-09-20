# Pendientes antes de operar con datos reales

Dos cosas quedaron documentadas y no se pueden cerrar desde la máquina de
desarrollo: la validación de concurrencia contra PostgreSQL y la aplicación
de la migración del índice único en el VPS. Este archivo es el
procedimiento exacto para ambas.

La sección 3 es distinta: **no es un defecto técnico**, es configuración
que cada institución captura por su cuenta. Está aquí para que no se
vuelva a confundir con un bloqueo del sistema.

---

## 1. Validar la concurrencia de pagos contra PostgreSQL

> **HECHO el 2026-09-20** (PostgreSQL 16.2 real): la suite completa pasa contra
> PostgreSQL (409/409) y 12 rondas de 2 cobros simultáneos de $800 sobre un cargo de
> $1,000 dieron **0 sobrepagos** y ninguna ronda con 2 pagos aceptados (el bloqueo
> `FOR UPDATE` funciona). Además apareció y se corrigió un bloqueo de despliegue: la
> migración `f1a2b3c4d5e6` solo funcionaba en SQLite y `flask db upgrade` fallaba en
> PostgreSQL. **Cómo repetirlo** en cualquier máquina con PostgreSQL:
> `createdb sge_test && TEST_DATABASE_URL=postgresql://USUARIO:CLAVE@localhost/sge_test pytest`
> (la suite exige que el nombre de la base contenga "test" para no tocar una real).
> Lo que sigue describe el análisis original; repite la prueba en el servidor de
> producción antes de abrir el sistema si cambian versiones de PostgreSQL.

### Qué hace hoy el código (verificado, no supuesto)

`registrar_pago()` (`app.py`) ejecuta, dentro de UNA sola transacción:

| Paso | Sentencia | Qué ocurre |
|---|---|---|
| 1 | `SELECT ... FROM cargos WHERE id = ? FOR UPDATE` | **se toma el bloqueo de fila** |
| 2 | `SELECT ... FROM pagos WHERE cargo_fk = ?` | se calcula el saldo, ya con el bloqueo tomado |
| 3 | `INSERT INTO pagos ...` | se registra el pago |
| 4 | `UPDATE cargos SET estatus = ?` | se recalcula el estatus |
| 5 | `COMMIT` | **se libera el bloqueo** |

El `FOR UPDATE` se verificó compilando la consulta con el dialecto
`postgresql`; el orden de las 5 sentencias se verificó capturando el SQL
real de un cobro con un listener de SQLAlchemy.

En SQLite la cláusula se omite a propósito (`db.engine.dialect.name !=
'sqlite'`), porque ese motor no tiene bloqueo por fila. **Por eso la suite
actual NO puede demostrar la protección: hay que probarla en PostgreSQL.**

### Por qué esto debería bastar en PostgreSQL

Con el nivel de aislamiento por defecto (`READ COMMITTED`), la transacción
B que pide `FOR UPDATE` sobre la misma fila **se queda esperando** hasta
que A haga COMMIT. Al desbloquearse, PostgreSQL re-evalúa la fila con la
versión ya commiteada, y la lectura de `pagos` del paso 2 (sentencia
nueva) ya ve el pago de A. B calcula su saldo sobre el estado real y su
validación `monto_pagado > saldo` rechaza el sobrecobro.

### Qué falta exactamente para poder validarlo

La máquina de desarrollo tiene PostgreSQL 16 corriendo, pero **el proyecto
no está cableado para probar contra él**. Faltan cuatro cosas:

1. **Driver:** `psycopg2-binary` no está en el entorno de desarrollo (vive
   en `requirements-prod.txt`, sólo para el VPS). Habría que agregarlo a
   `requirements-dev.txt` o instalarlo aparte.
2. **Base y rol de pruebas:** no existe una base dedicada; hay que crearla
   (ej. `sge_test`) con un rol que pueda crear y borrar tablas.
3. **`tests/conftest.py` lo impide a propósito:** la línea 30 fuerza
   `FLASK_ENV=testing` (SQLite en memoria) y la 62 tiene
   `assert 'memory' in uri_actual`, una salvaguarda para que la suite
   nunca toque una base real por accidente. Habría que permitir apuntar a
   PostgreSQL mediante una variable de entorno **sin quitar** esa
   salvaguarda para el caso normal.
4. **Pool de conexiones apto para hilos:** el test tiene que abrir dos
   conexiones simultáneas de verdad; con `StaticPool` (lo que usa hoy
   SQLite en memoria) no hay concurrencia real posible.

### Forma del test cuando se habilite

```python
# Se salta solo si no hay PostgreSQL configurado, para no romper la suite normal.
@pytest.mark.skipif(not os.environ.get('DATABASE_URL_TEST', '').startswith('postgresql'),
                    reason='Requiere PostgreSQL real: with_for_update() no bloquea en SQLite')
def test_dos_pagos_concurrentes_no_sobrepasan_el_saldo():
    # Cargo de $1000. Dos hilos intentan cobrar $800 cada uno,
    # sincronizados con threading.Barrier para que lleguen juntos.
    # Esperado: uno tiene éxito y el otro es rechazado por saldo
    # insuficiente. Invariante duro: total_pagado() <= monto + recargo.
```

Lo que debe afirmar el test es el **invariante**: la suma de los pagos
commiteados nunca puede exceder `monto + recargo_aplicado`.

### Alternativa recomendada (sin tocar el proyecto)

Hacerlo contra el VPS de **staging**, no en la máquina de desarrollo:
levantar la app con su PostgreSQL real y lanzar dos cobros simultáneos del
mismo cargo (por ejemplo con `curl` en paralelo, o dos pestañas dando clic
a la vez), y comprobar en la base que sólo quedó un pago:

```sql
SELECT c.id, c.monto + c.recargo_aplicado AS total_cobrable,
       COALESCE(SUM(p.monto_pagado) FILTER (WHERE NOT p.anulado), 0) AS pagado
FROM cargos c
LEFT JOIN pagos p ON p.cargo_fk = c.id
GROUP BY c.id
HAVING COALESCE(SUM(p.monto_pagado) FILTER (WHERE NOT p.anulado), 0)
       > c.monto + c.recargo_aplicado;
```

**Cero filas = ningún cargo quedó sobrepagado.** Esta consulta también
sirve como chequeo periódico en producción.

### Fragilidad conocida del mecanismo

La garantía depende de que **nada cargue el `Cargo` antes** de la consulta
bloqueante. Hoy no ocurre. Pero si en el futuro alguien agrega una consulta
previa que traiga ese `Cargo` a la sesión, SQLAlchemy devolvería la
instancia ya presente en el *identity map* sin refrescar sus atributos (no
se usa `populate_existing()`), y el saldo podría leerse de datos anteriores
al bloqueo, **sin ningún síntoma visible**. Si se toca esa función, hay que
revisar esto.

---

## 2. Aplicar la migración del índice único en producción

Migración: `b7e2c9a41f3d_uniq_cargos_no_cancelado.py`
Índice: `uq_cargos_activo_matricula_concepto_periodo`
Llave: `(matricula_fk, concepto_cobro_fk, COALESCE(periodo_escolar, ''))`
sobre los cargos **no cancelados**.

> La migración **falla** si en producción ya existen duplicados que violen
> esa llave. Por eso el paso 1 es obligatorio ANTES del upgrade.

### Paso 1 — Detectar duplicados (sólo lectura)

```bash
psql -U sge_user -h localhost -d sge_produccion
```

```sql
-- Grupos que impedirían crear el índice. Debe devolver 0 filas.
SELECT matricula_fk,
       concepto_cobro_fk,
       COALESCE(periodo_escolar, '') AS periodo,
       COUNT(*) AS repeticiones
FROM cargos
WHERE estatus <> 'CANCELADO'
GROUP BY matricula_fk, concepto_cobro_fk, COALESCE(periodo_escolar, '')
HAVING COUNT(*) > 1
ORDER BY repeticiones DESC;
```

**0 filas → pasa directo al paso 3.**

### Paso 2 — Inspeccionar los duplicados (sólo lectura)

Sólo si el paso 1 devolvió algo. Esto muestra cada cargo involucrado con
lo que hace falta para decidir a mano qué hacer con él:

```sql
SELECT c.id,
       c.matricula_fk,
       a.nombre_completo,
       c.concepto,
       c.concepto_cobro_fk,
       c.periodo_escolar,
       c.monto,
       c.recargo_aplicado,
       c.estatus,
       c.fecha_generacion,
       COALESCE(SUM(p.monto_pagado) FILTER (WHERE NOT p.anulado), 0) AS pagado,
       COUNT(p.id) FILTER (WHERE NOT p.anulado) AS num_pagos
FROM cargos c
JOIN alumnos a ON a.matricula_id = c.matricula_fk
LEFT JOIN pagos p ON p.cargo_fk = c.id
WHERE c.estatus <> 'CANCELADO'
  AND (c.matricula_fk, c.concepto_cobro_fk, COALESCE(c.periodo_escolar, '')) IN (
      SELECT matricula_fk, concepto_cobro_fk, COALESCE(periodo_escolar, '')
      FROM cargos
      WHERE estatus <> 'CANCELADO'
      GROUP BY matricula_fk, concepto_cobro_fk, COALESCE(periodo_escolar, '')
      HAVING COUNT(*) > 1
  )
GROUP BY c.id, a.nombre_completo
ORDER BY c.matricula_fk, c.concepto_cobro_fk, c.periodo_escolar, c.fecha_generacion;
```

Lo importante de cada fila es **`pagado` / `num_pagos`**:

- Un duplicado **sin pagos** normalmente es el cargo repetido por error.
- Un duplicado **con pagos** NO se debe tocar sin decidir antes qué pasa
  con ese dinero.

**No borres ni edites nada desde SQL.** La forma correcta de resolverlo es
desde la interfaz, con Dirección: cancelar el cargo sobrante (queda
registrado quién y por qué). Los cargos `CANCELADO` quedan fuera del
índice, así que cancelar el sobrante basta para desbloquear la migración.
Guarda antes un respaldo (`deploy/backup.sh`).

### Paso 3 — Aplicar la migración

```bash
ssh sge@IP_DEL_VPS
cd ~/sge_enfermeria
source venv/bin/activate

./deploy/backup.sh                 # respaldo antes de tocar el esquema

export FLASK_APP=app.py
export FLASK_ENV=production
flask db upgrade
```

Salida esperada:

```
INFO  [alembic.runtime.migration] Running upgrade f1a2b3c4d5e6 -> b7e2c9a41f3d, Índice único parcial contra cargos duplicados
```

### Paso 4 — Comprobar que el índice quedó

```bash
flask db current        # debe imprimir: b7e2c9a41f3d (head)
```

```sql
-- Definición real del índice en PostgreSQL
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'cargos'
  AND indexname = 'uq_cargos_activo_matricula_concepto_periodo';
```

Debe aparecer algo equivalente a:

```
CREATE UNIQUE INDEX uq_cargos_activo_matricula_concepto_periodo
  ON public.cargos USING btree (matricula_fk, concepto_cobro_fk, COALESCE(periodo_escolar, ''::character varying))
  WHERE ((estatus)::text <> 'CANCELADO'::text)
```

Prueba funcional (en staging, **no** en producción): intenta crear dos
veces el mismo cargo desde la interfaz. El segundo debe rechazarse con el
mensaje de cargo duplicado, no con un error 500.

### Si algo sale mal

```bash
flask db downgrade      # revierte: elimina el índice, no toca ningún dato
```

La migración sólo crea un índice: `downgrade` lo borra y deja la base
exactamente como estaba. No transforma ni elimina registros.

---

## 3. Precios: configuración de CADA institución, no un dato faltante

`ConceptoCobro.monto_sugerido` y `PlanEstudio.monto_mensualidad` son
`nullable=True` **a propósito**. Que estén vacíos en una instalación
recién sembrada (`seed.py` crea el catálogo de conceptos sin precios) no
es un error del sistema ni un pendiente de programación: es que esa
institución todavía no capturó sus cuotas.

### NULL y $0.00 NO son lo mismo

| Valor | Qué significa | Qué hace el sistema |
|---|---|---|
| `NULL` | La institución todavía no capturó ese precio | **No genera el cargo** y avisa qué falta configurar |
| `0.00` | La institución decidió que ese concepto es gratuito | Es un precio válido: **sí** genera el cargo, en $0.00 |

Lo que el sistema **nunca** debe hacer, y ya no hace:

- inventar un precio o poner uno "por defecto";
- convertir un `NULL` en `$0.00` para poder seguir adelante;
- copiar el precio de otra institución, otra carrera u otro concepto;
- generar en silencio un cargo económico que nadie configuró.

> Hasta el 7 de septiembre de 2026, `_generar_cargos_de_periodo()` creaba
> el cargo con `monto=concepto.monto_sugerido or Decimal('0.00')`: convertía
> "sin configurar" en "$0" sin decirlo, y dejaba en el expediente del alumno
> un cargo falso que además nadie podía cobrar. Corregido; queda fijado en
> `tests/test_precios_por_institucion.py`.

### Qué pasa hoy si falta un precio

Ninguna operación revienta ni asume un monto. Se detiene esa parte y se
dice exactamente qué capturar y dónde:

| Operación | Si falta el precio |
|---|---|
| Activar a un alumno (genera Inscripción + mensualidades) | No genera ese cargo; avisa: *"…ese concepto todavía no tiene precio configurado en el catálogo de la institución"* |
| Avanzar de cuatrimestre (individual o en lote) | Igual; en el lote el aviso sale **una sola vez**, no uno por alumno |
| Generar mensualidades en lote | Omite al alumno y lo lista con el motivo: *"Su plan de estudios no tiene mensualidad configurada"* |
| Crear un cargo a mano | El monto es obligatorio y debe ser mayor a 0 (nunca se autocompleta con un precio inexistente) |

### Dónde captura sus precios cada institución

Ambas pantallas ya existen, y son las **únicas** que escriben precios:

| Precio | Pantalla | Quién puede |
|---|---|---|
| `ConceptoCobro.monto_sugerido` | `/conceptos-cobro` | Directivo y Contador |
| `PlanEstudio.monto_mensualidad` | `/planes/mensualidades` | Solo Directivo |

Dejar el campo vacío en cualquiera de las dos guarda `NULL` ("sin
definir"), no `0`. Es una operación válida y reversible.

### Antes de que esa institución empiece a cobrar

Esto **no** bloquea el despliegue ni la migración; bloquea únicamente las
funciones de cobro que dependan de ese precio. Checklist por institución:

1. Capturar el precio de los conceptos que se cobren de forma automática
   (típicamente Inscripción y Reinscripción) en `/conceptos-cobro`.
2. Marcar cuál concepto es la mensualidad (`es_mensualidad`) y capturar
   `monto_mensualidad` de cada carrera en `/planes/mensualidades`.
3. Los conceptos que se cobran a mano y cambian de caso a caso (Uniformes,
   Material Didáctico, constancias…) pueden quedarse **sin precio**: se
   captura el monto al crear cada cargo.

### Aislamiento entre instituciones (estado real, hoy)

**Hoy el sistema es de UNA institución por instalación.** No hay
multi-tenancy dentro de la aplicación: no existen `tenant_id`,
`schema_translate_map` ni filtros por institución en las consultas —
`ConfiguracionInstitucion.obtener()` es un singleton y el catálogo de
precios es global dentro de esa base de datos.

El aislamiento entre instituciones es, por lo tanto, **de despliegue**:
cada institución tiene su propio servidor y su propia base de datos, y
`DATABASE_URL` apunta a una sola. La Universidad A no puede leer los
precios de la Universidad B porque no comparten base de datos, no porque
haya un filtro en el código. Es un aislamiento fuerte, pero hay que
entender de dónde viene: **si algún día dos instituciones comparten base
de datos, ese aislamiento desaparece por completo** — no hay nada en el
código que lo sostenga.

Dentro de una institución, lo que sí existe y está probado es el control
por rol de la tabla de arriba
(`tests/test_precios_por_institucion.py`): un Capturador no puede tocar
ningún precio, un Contador no puede cambiar la mensualidad de una carrera,
y sin sesión no se puede ni consultar la pantalla.

### Futuro: módulo de configuración institucional

Cuando se construya el módulo
(Administración → Configuración institucional → Conceptos de cobro →
Precios → Guardar), estas son las reglas que debe respetar:

1. **No inventar nada.** Sin precio capturado, el campo se queda vacío
   (`NULL`) y las operaciones que lo necesiten avisan, como ya hacen.
2. **Distinguir vacío de cero** en la interfaz. "Sin definir" y "$0.00
   (gratuito)" tienen que verse distinto, porque significan cosas
   distintas.
3. **Nunca precargar** un precio tomándolo de otra institución, otra
   carrera u otro concepto, ni siquiera como sugerencia.
4. **Todo cambio de precio se audita**: quién, cuándo y de qué valor a
   qué valor (hoy no queda registro de eso; es lo único que habría que
   agregar, con el mismo patrón de `HistorialEstatus`).
5. **Los cargos ya generados no se tocan** al cambiar un precio: un cargo
   guarda su propio `monto`, y por diseño los periodos futuros se generan
   uno a la vez justo para que un cambio de precio no arrastre cargos
   viejos.

Si además se decide alojar varias instituciones en una sola instalación,
eso es un cambio de arquitectura aparte (columna de institución en cada
tabla + filtro obligatorio en cada consulta, o una base por institución) y
**tiene que decidirse antes** de construir la pantalla, no después.
