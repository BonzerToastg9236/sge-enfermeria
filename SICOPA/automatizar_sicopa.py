"""
Automatización de captura en SICOPA (Gobierno del Estado de México)
--------------------------------------------------------------------
Lee los datos fijos y la lista de empleados desde "Plantilla_SICOPA.xlsx"
y llena el formulario de alta de empleados (index.faces) fila por fila.

REQUISITOS PREVIOS
-------------------
1. pip install selenium openpyxl pandas
2. Descargar el chromedriver que corresponda a tu versión de Chrome:
   https://googlechromelabs.github.io/chrome-for-testing/
3. Haber iniciado sesión manualmente en SICOPA con el perfil de Chrome
   que uses (ver PERFIL_CHROME abajo), ya que el script no automatiza el login.
4. Revisar los IDs de campo abajo contra el HTML real antes de correr en
   producción: los formularios PrimeFaces regeneran sufijos (":j_idtNN")
   que pueden cambiar entre despliegues de la plataforma.

CÓMO FUNCIONA
-------------
- Abre Chrome con tu perfil de usuario (para reutilizar la sesión ya
  iniciada) y navega al formulario.
- Por cada fila del Excel, llena los campos de texto directamente,
  maneja los combos (PrimeFaces selectonemenu) y los autocomplete
  (adscripción / dependencia) con clic + selección de la primera
  coincidencia.
- Da clic en "Guardar", confirma el diálogo modal de verificación de
  nombre, y espera a que la página regrese al formulario en blanco
  antes de seguir con la siguiente persona.
- Se detiene y avisa en consola si algún campo no se pudo llenar, para
  que una persona revise ese registro a mano.
"""

import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    InvalidElementStateException,
    StaleElementReferenceException,
)

# ----------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------
URL_FORMULARIO = "https://nsicopa.edomex.gob.mx/Sicopa/sistema/empleados/index.faces"
ARCHIVO_EXCEL = "Plantilla_SICOPA_HG_Tultitlan.xlsx"

# Si quieres reutilizar una sesión de Chrome ya logueada, apunta esto a
# tu carpeta de perfil (chrome://version -> "Ruta de perfil"). Si lo
# dejas en None, Chrome abrirá un perfil limpio y tendrás que iniciar
# sesión manualmente cuando se abra el navegador.
PERFIL_CHROME = None  # ej: r"C:\Users\TuUsuario\AppData\Local\Google\Chrome\User Data"

ESPERA_SEGUNDOS = 15
PAUSA_ENTRE_REGISTROS = 2  # segundos de cortesía entre alta y alta

# MODO DE PRUEBA: si es True, llena todos los campos pero NO da clic en
# "Guardar" ni confirma el diálogo. Úsalo para revisar visualmente que
# los datos caen en el campo correcto antes de soltar el lote completo.
DRY_RUN = True

# Cuántas personas probar en modo de prueba (None = todas)
LIMITE_PRUEBA = 3

# IDs de los campos, tomados del código fuente compartido.
# Ajustar aquí si el HTML cambia.
IDS = {
    "chk_tiene_csp": "frmEmpleados:blClave_input",
    "csp": "frmEmpleados:txtCSP",
    "curp": "frmEmpleados:txtCURP",
    "genero": "frmEmpleados:cboGenero",          # selectonemenu (PrimeFaces)
    "nombre": "frmEmpleados:txtNombre",
    "paterno": "frmEmpleados:txtPaterno",
    "materno": "frmEmpleados:txtMaterno",
    "fecha_nacimiento": "frmEmpleados:txtFnacimiento_input",
    "correo": "frmEmpleados:txtCorreo",
    "contrasenia": "frmEmpleados:txtContrasenia",
    "cp": "frmEmpleados:txtCSPN",
    "colonia": "frmEmpleados:txtColonia",
    "domicilio": "frmEmpleados:txtDomicilio",
    "clave_adscripcion": "frmEmpleados:txtClaveDep_input",   # autocomplete
    "nombre_dependencia": "frmEmpleados:txtNomDep_input",    # autocomplete
    "centro_trabajo": "frmEmpleados:txtCentroTrabajo",
    "puesto_nominal": "frmEmpleados:cboPuesto",              # selectonemenu (igual patrón que género)
    "fecha_ingreso": "frmEmpleados:txtFechaIngreso_input",
    "btn_guardar": "frmEmpleados:btnGuardar",
    "btn_confirmar_dialogo": "idDialogoConfirm:btnGuardar1",
}


def iniciar_navegador():
    opciones = webdriver.ChromeOptions()
    if PERFIL_CHROME:
        opciones.add_argument(f"user-data-dir={PERFIL_CHROME}")
    driver = webdriver.Chrome(options=opciones)
    driver.maximize_window()
    return driver


def esperar_elemento(driver, by, valor, timeout=ESPERA_SEGUNDOS):
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, valor))
    )


def esperar_interactuable(driver, id_campo, timeout=ESPERA_SEGUNDOS):
    elemento = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.ID, id_campo))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elemento)
    return elemento


def llenar_texto(driver, id_campo, valor):
    """Limpia y escribe un valor en un <input> de texto normal."""
    if not valor:
        return
    campo = esperar_interactuable(driver, id_campo)
    try:
        campo.clear()
        campo.send_keys(str(valor))
    except InvalidElementStateException:
        # Campo de solo lectura: se fuerza el valor por JavaScript.
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            campo, str(valor),
        )


SELECTORES_OPCIONES_COMBO = [
    "[role='option']",              # estándar de accesibilidad, más confiable
    "li.ui-selectonemenu-item",     # PrimeFaces clásico (tema jQuery-UI)
    "li.p-dropdown-item",           # PrimeFaces moderno (tema 'p-')
    "li.ui-selectonemenu-list-item",
]


def _opciones_visibles_combo(driver):
    for selector in SELECTORES_OPCIONES_COMBO:
        opciones = [op for op in driver.find_elements(By.CSS_SELECTOR, selector) if op.is_displayed()]
        if opciones:
            return opciones
    return []


def abrir_combo(driver, id_combo):
    """
    Abre un ui-selectonemenu de PrimeFaces. Usa clic por JavaScript (más
    confiable que el clic normal de Selenium para estos widgets) y, si no
    abrió nada, reintenta sobre el "_label" interno, que es donde algunas
    versiones de PrimeFaces enganchan el evento real.
    """
    candidatos_id = [id_combo, f"{id_combo}_label"]
    for candidato in candidatos_id:
        elementos = driver.find_elements(By.ID, candidato)
        if not elementos:
            continue
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elementos[0])
        driver.execute_script("arguments[0].click();", elementos[0])
        try:
            WebDriverWait(driver, 4).until(lambda d: len(_opciones_visibles_combo(d)) > 0)
            return
        except TimeoutException:
            continue
    raise NoSuchElementException(
        f"El combo {id_combo} no abrió sus opciones ni con clic directo ni con '_label'. "
        f"Puede que el ID real en esta versión de SICOPA sea distinto."
    )


def seleccionar_combo(driver, id_combo, texto_opcion):
    """
    Selecciona una opción en un combo. Prueba, en orden:
    1) Si el propio id es un <select> nativo del navegador (que es lo que
       muestran tus screenshots: un desplegable estilo Windows/Chrome, no
       un panel con li's), lo maneja con la clase Select de Selenium —
       esto NO requiere abrir el menú visualmente y es lo más confiable.
    2) Si hay un <select> oculto en "{id}_input" (patrón común de
       PrimeFaces cuando el widget visible es un div decorativo).
    3) Como último recurso, el panel clásico tipo PrimeFaces con clic +
       lista de <li>.
    """
    if not texto_opcion:
        return
    texto_opcion = str(texto_opcion).strip()

    for candidato_id in (id_combo, f"{id_combo}_input"):
        elementos = driver.find_elements(By.ID, candidato_id)
        if elementos and elementos[0].tag_name.lower() == "select":
            select_el = elementos[0]
            try:
                Select(select_el).select_by_visible_text(texto_opcion)
            except NoSuchElementException:
                opciones_reales = [o.text.strip() for o in Select(select_el).options]
                raise NoSuchElementException(
                    f"No se encontró la opción '{texto_opcion}' en el <select> {candidato_id}. "
                    f"Opciones reales: {opciones_reales}"
                )
            # Disparar 'change' por si SICOPA usa AJAX ligado a ese evento
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", select_el)
            return

    # Respaldo: widget de panel clásico (div + <li> visibles)
    abrir_combo(driver, id_combo)
    opciones = _opciones_visibles_combo(driver)
    for op in opciones:
        if op.text.strip().lower() == texto_opcion.lower():
            op.click()
            return
    disponibles = [op.text.strip() for op in opciones]
    raise NoSuchElementException(
        f"No se encontró la opción '{texto_opcion}' en el combo {id_combo}. "
        f"Opciones disponibles: {disponibles}"
    )


SELECTORES_SUGERENCIAS_AUTOCOMPLETE = [
    "[role='option']",
    "li.ui-autocomplete-item",
    "li.p-autocomplete-item",
]


def llenar_autocomplete(driver, id_input, valor):
    """
    Maneja un ui-autocomplete de PrimeFaces: escribe el valor,
    espera la lista de sugerencias y hace clic en la primera.
    """
    if not valor:
        return
    campo = esperar_interactuable(driver, id_input)
    campo.clear()
    campo.send_keys(str(valor))
    try:
        WebDriverWait(driver, 5).until(
            lambda d: any(
                op.is_displayed()
                for selector in SELECTORES_SUGERENCIAS_AUTOCOMPLETE
                for op in d.find_elements(By.CSS_SELECTOR, selector)
            )
        )
        for selector in SELECTORES_SUGERENCIAS_AUTOCOMPLETE:
            sugerencias = [op for op in driver.find_elements(By.CSS_SELECTOR, selector) if op.is_displayed()]
            if sugerencias:
                sugerencias[0].click()
                return
    except TimeoutException:
        # Si no hay sugerencias, deja el texto tecleado tal cual
        campo.send_keys(Keys.ESCAPE)


def llenar_fecha(driver, id_input, valor_dd_mm_aaaa):
    """
    Campo de calendario PrimeFaces. Algunos vienen marcados de solo
    lectura (obligan a usar el selector visual del calendario), lo que
    hace que .clear()/.send_keys() truenen con 'invalid element state'.
    En ese caso se fuerza el valor directo por JavaScript.
    """
    if not valor_dd_mm_aaaa:
        return
    campo = esperar_interactuable(driver, id_input)
    try:
        campo.clear()
        campo.send_keys(str(valor_dd_mm_aaaa))
        campo.send_keys(Keys.TAB)
    except InvalidElementStateException:
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            campo, str(valor_dd_mm_aaaa),
        )


def marcar_checkbox_csp(driver, tiene_csp, valor_csp):
    """
    OJO: en pruebas reales el campo CSP aparece deshabilitado/gris incluso
    con valor, lo que sugiere que SICOPA lo autocompleta solo (probablemente
    al escribir el CURP) y NO se debe escribir a mano ni depende del
    checkbox como se asumió al inicio. Por eso esta función ya NO intenta
    tocar el campo — solo se deja como registro de que faltaría confirmar
    ese comportamiento con alguien que conozca bien el flujo de SICOPA.
    Si más adelante confirman que sí hay que interactuar con él, aquí es
    donde se vuelve a programar.
    """
    return


def llenar_campo_seguro(driver, curp, nombre_campo, funcion, *args):
    """Envuelve el llenado de un campo para:
    - Reintentar solo si el elemento se volvió 'stale' (típico cuando otro
      campo dispara una actualización AJAX que reconstruye este pedazo del
      formulario justo cuando lo íbamos a tocar).
    - Reportar CUÁL campo falló, con screenshot del momento del error.
    """
    ultimo_error = None
    for intento in range(3):
        try:
            funcion(*args)
            return
        except StaleElementReferenceException as e:
            ultimo_error = e
            time.sleep(1)  # dar tiempo a que la actualización AJAX termine
        except Exception as e:
            ultimo_error = e
            break

    try:
        nombre_archivo = f"error_{curp}_{nombre_campo.replace(' ', '_')}.png"
        driver.save_screenshot(nombre_archivo)
    except Exception:
        nombre_archivo = "(no se pudo guardar el screenshot)"
    raise RuntimeError(
        f"Falló el campo '{nombre_campo}': {type(ultimo_error).__name__}: {ultimo_error} "
        f"[screenshot: {nombre_archivo}]"
    ) from ultimo_error


def registrar_empleado(driver, fijos, fila):
    driver.get(URL_FORMULARIO)
    try:
        esperar_elemento(driver, By.ID, IDS["curp"])
    except TimeoutException:
        # La primera carga tras el login a veces tarda más de lo normal.
        # Se refresca una vez y se le da más tiempo antes de rendirse.
        driver.get(URL_FORMULARIO)
        esperar_elemento(driver, By.ID, IDS["curp"], timeout=ESPERA_SEGUNDOS + 15)
    curp = fila["CURP"]

    marcar_checkbox_csp(driver, fila.get("Tiene CSP"), fila.get("CSP"))

    llenar_campo_seguro(driver, curp, "CURP", llenar_texto, driver, IDS["curp"], fila["CURP"])
    llenar_campo_seguro(driver, curp, "Genero", seleccionar_combo, driver, IDS["genero"], fila["Genero"])
    llenar_campo_seguro(driver, curp, "Nombre(s)", llenar_texto, driver, IDS["nombre"], fila["Nombre(s)"])
    llenar_campo_seguro(driver, curp, "Apellido paterno", llenar_texto, driver, IDS["paterno"], fila["Apellido paterno"])
    llenar_campo_seguro(driver, curp, "Apellido materno", llenar_texto, driver, IDS["materno"], fila["Apellido materno"])
    llenar_campo_seguro(driver, curp, "Fecha de nacimiento", llenar_fecha, driver, IDS["fecha_nacimiento"], fila["Fecha de nacimiento"])
    llenar_campo_seguro(driver, curp, "Correo", llenar_texto, driver, IDS["correo"], fila["Correo electronico"])
    llenar_campo_seguro(driver, curp, "Contraseña", llenar_texto, driver, IDS["contrasenia"], fila["Contrasenia"])

    llenar_campo_seguro(driver, curp, "CP", llenar_texto, driver, IDS["cp"], fijos["Código Postal (CP)"])
    llenar_campo_seguro(driver, curp, "Colonia", llenar_texto, driver, IDS["colonia"], fijos["Colonia"])
    llenar_campo_seguro(driver, curp, "Domicilio", llenar_texto, driver, IDS["domicilio"], fijos["Domicilio completo"])

    llenar_campo_seguro(driver, curp, "Clave adscripción", llenar_autocomplete, driver, IDS["clave_adscripcion"], fijos["Código de adscripción de unidad administrativa"])
    llenar_campo_seguro(driver, curp, "Nombre dependencia", llenar_autocomplete, driver, IDS["nombre_dependencia"], fijos["Nombre de dependencia"])
    llenar_campo_seguro(driver, curp, "Centro de trabajo", llenar_texto, driver, IDS["centro_trabajo"], fijos["Centro de trabajo"])
    llenar_campo_seguro(driver, curp, "Puesto nominal", seleccionar_combo, driver, IDS["puesto_nominal"], fijos["Puesto nominal equivalente"])
    llenar_campo_seguro(driver, curp, "Fecha de ingreso", llenar_fecha, driver, IDS["fecha_ingreso"], fila["Fecha de ingreso al organismo"])

    esperar_elemento(driver, By.ID, IDS["btn_guardar"])

    if DRY_RUN:
        print("    [DRY RUN] Datos cargados, NO se guardó. Revisa la pantalla y presiona Enter para continuar con la siguiente persona...")
        input()
        return

    driver.find_element(By.ID, IDS["btn_guardar"]).click()

    # Diálogo de confirmación de nombre
    try:
        boton_confirmar = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.ID, IDS["btn_confirmar_dialogo"]))
        )
        boton_confirmar.click()
    except TimeoutException:
        pass  # no siempre aparece el diálogo; continuar

    time.sleep(PAUSA_ENTRE_REGISTROS)


def main():
    fijos_df = pd.read_excel(ARCHIVO_EXCEL, sheet_name="Datos_Fijos")
    fijos = dict(zip(fijos_df["Campo"], fijos_df["Valor"]))

    empleados_df = pd.read_excel(ARCHIVO_EXCEL, sheet_name="Empleados")
    empleados_df = empleados_df.dropna(how="all")

    if DRY_RUN and LIMITE_PRUEBA:
        empleados_df = empleados_df.head(LIMITE_PRUEBA)
        print(f"*** MODO DE PRUEBA: solo se van a procesar {LIMITE_PRUEBA} personas y NO se va a guardar nada. ***\n")

    driver = iniciar_navegador()
    exitosos, fallidos = [], []

    driver.get(URL_FORMULARIO)
    print("\n>>> Se abrió Chrome. Inicia sesión manualmente en SICOPA.")
    print(">>> Cuando ya veas el formulario 'Registro de empleado de sector auxiliar', regresa aquí y presiona Enter para continuar...")
    input()

    try:
        for i, fila in empleados_df.iterrows():
            curp = fila.get("CURP", f"fila_{i}")
            try:
                registrar_empleado(driver, fijos, fila)
                exitosos.append(curp)
                print(f"[OK] {curp} registrado.")
            except Exception as e:
                mensaje_corto = str(e).split("Stacktrace:")[0].strip()
                fallidos.append((curp, mensaje_corto))
                print(f"[ERROR] {curp}: {mensaje_corto}")
    finally:
        driver.quit()

    print("\n--- RESUMEN ---")
    print(f"Registrados con éxito: {len(exitosos)}")
    print(f"Con error (revisar a mano): {len(fallidos)}")
    for curp, error in fallidos:
        print(f"  - {curp}: {error}")


if __name__ == "__main__":
    main()
