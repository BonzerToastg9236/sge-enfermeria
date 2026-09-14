"""
Documentos del expediente: subida (fix #7 — validación de firma real de
archivo), consulta y borrado. Todas las rutas exigen sesión + rol.
"""

import os

from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, send_from_directory, current_app
from werkzeug.utils import secure_filename

from extensiones import db
from modelos import Alumno, DocumentoAlumno, TipoDocumento
from utilidades.seguridad import rol_requerido
from utilidades.archivos import extension_permitida, contenido_coincide_con_extension
from utilidades.fechas import ahora_utc
from servicios.academico import _max_periodos

documentos_bp = Blueprint('documentos', __name__)


# Mapeo de <name> del <input type="file"> -> TipoDocumento correspondiente
CAMPOS_DOCUMENTOS = {
    'archivo_domicilio': TipoDocumento.COMPROBANTE_DOMICILIO,
    'archivo_ine': TipoDocumento.INE,
    'archivo_curp': TipoDocumento.CURP_DOC,
    'archivo_acta': TipoDocumento.ACTA_NACIMIENTO,
    'archivo_certificado': TipoDocumento.CERTIFICADO_PREPA,
    'archivo_foto': TipoDocumento.FOTOGRAFIA,
}


@documentos_bp.route('/alumno/<matricula>/documentos', methods=['GET', 'POST'])
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def documentos(matricula):
    alumno = db.get_or_404(Alumno, matricula)

    if request.method == 'POST':
        # --- 1. Actualizar datos complementarios del expediente ---
        alumno.escuela_prepa = request.form.get('escuela_prepa', '').strip() or alumno.escuela_prepa
        alumno.alergias_condiciones = request.form.get('alergias_condiciones', '').strip() or None
        alumno.telefono = request.form.get('telefono', '').strip() or alumno.telefono
        alumno.telefono_tutor = request.form.get('telefono_tutor', '').strip() or None

        # --- 1b. Seguimiento administrativo y académico ---
        cuatrimestre_raw = request.form.get('cuatrimestre_actual', '').strip()
        if cuatrimestre_raw.isdigit():
            alumno.cuatrimestre_actual = int(cuatrimestre_raw)
        alumno.documentacion_pendiente = request.form.get('documentacion_pendiente', '').strip() or None
        alumno.materias_adeudadas = request.form.get('materias_adeudadas', '').strip() or None
        alumno.faltas_administrativas = request.form.get('faltas_administrativas', '').strip() or None

        # --- 2. Procesar cada archivo subido (todos opcionales) ---
        carpeta_alumno = os.path.join(current_app.config['UPLOAD_FOLDER'], alumno.matricula_id)
        archivos_guardados = 0

        for campo_form, tipo_doc in CAMPOS_DOCUMENTOS.items():
            archivo = request.files.get(campo_form)

            if not archivo or archivo.filename == '':
                continue  # No se seleccionó archivo para este campo, se omite

            if not extension_permitida(archivo.filename):
                flash(
                    f'El archivo de "{tipo_doc.value}" tiene un formato no permitido '
                    f'(solo PDF, JPG o PNG).',
                    'danger'
                )
                continue

            # La extensión la escribe quien sube el archivo; los bytes no
            # mienten. Se valida ANTES de escribir nada en disco.
            if not contenido_coincide_con_extension(archivo):
                flash(
                    f'El archivo de "{tipo_doc.value}" no se guardó: su contenido real '
                    f'no coincide con su extensión (no es un PDF/JPG/PNG válido). '
                    f'Si lo renombraste a mano, vuelve a exportarlo o escanearlo en el '
                    f'formato correcto.',
                    'danger'
                )
                continue

            os.makedirs(carpeta_alumno, exist_ok=True)
            nombre_seguro = secure_filename(archivo.filename)
            nombre_final = f'{tipo_doc.name}_{int(ahora_utc().timestamp())}_{nombre_seguro}'
            ruta_absoluta = os.path.join(carpeta_alumno, nombre_final)
            archivo.save(ruta_absoluta)

            documento = DocumentoAlumno(
                matricula_fk=alumno.matricula_id,
                tipo_documento=tipo_doc,
                nombre_archivo_original=archivo.filename,
                # OJO: aquí SIEMPRE forward-slash, aunque sea Windows, porque esta
                # ruta se usa para construir URLs (Flask sirve /static/... con '/').
                # os.path.join() usaría '\' en Windows y rompería el link (error 404).
                ruta_archivo=f'{alumno.matricula_id}/{nombre_final}',
            )
            db.session.add(documento)
            archivos_guardados += 1

        db.session.commit()

        if archivos_guardados:
            flash(f'Se guardaron {archivos_guardados} documento(s) y se actualizó la información.', 'success')
        else:
            flash('Se actualizó la información del expediente.', 'success')

        return redirect(url_for('documentos.documentos', matricula=matricula))

    documentos_alumno = (
        DocumentoAlumno.query
        .filter_by(matricula_fk=matricula)
        .order_by(DocumentoAlumno.fecha_subida.desc())
        .all()
    )
    return render_template(
        'documentos.html',
        alumno=alumno,
        documentos=documentos_alumno,
        max_cuatrimestres=_max_periodos()
    )


@documentos_bp.route('/alumno/<matricula>/documento/<int:doc_id>/ver')
@rol_requerido('DIRECTIVO', 'ADMINISTRATIVO', 'CAPTURADOR')
def ver_documento(matricula, doc_id):
    """
    Sirve el archivo físico de un documento del expediente, exigiendo
    sesión activa + rol adecuado (a diferencia de servirlo directamente
    desde /static/, que no requiere ningún login).

    SECURITY-NOTE: verificamos explícitamente que el doc_id pedido
    pertenezca a LA MISMA matrícula que viene en la URL. Sin este chequeo,
    alguien con sesión válida pero de bajo rango podría cambiar el doc_id
    en la URL para intentar ver el documento de OTRO alumno cuya matrícula
    no conoce -- aunque ambos estén detrás del mismo control de rol, cada
    documento debe amarrarse a su propio expediente.
    """
    documento = db.get_or_404(DocumentoAlumno, doc_id)
    if documento.matricula_fk != matricula:
        abort(404)

    carpeta_absoluta = os.path.join(
        current_app.config['UPLOAD_FOLDER'],
        os.path.dirname(documento.ruta_archivo)
    )
    nombre_archivo = os.path.basename(documento.ruta_archivo)

    # send_from_directory ya protege internamente contra path traversal
    # (rutas tipo "../../etc/passwd"), pero igual construimos la ruta a
    # partir de datos que nosotros mismos generamos al subir el archivo
    # (ver documentos()), nunca a partir de un parámetro de la URL.
    return send_from_directory(carpeta_absoluta, nombre_archivo)


@documentos_bp.route('/documento/<int:doc_id>/eliminar', methods=['POST'])
@rol_requerido('DIRECTIVO')
def eliminar_documento(doc_id):
    """
    Elimina un documento subido por equivocación: borra el archivo físico
    del disco y su registro en la base de datos, y regresa al expediente
    del mismo alumno.
    """
    documento = db.get_or_404(DocumentoAlumno, doc_id)
    matricula = documento.matricula_fk
    tipo_valor = documento.tipo_documento.value

    ruta_absoluta = os.path.join(
        current_app.config['UPLOAD_FOLDER'],
        documento.ruta_archivo.replace('/', os.sep)
    )

    try:
        if os.path.exists(ruta_absoluta):
            os.remove(ruta_absoluta)
    except OSError:
        # Si el archivo físico ya no está en disco, no bloqueamos el borrado
        # del registro en BD; igual se lo informamos al usuario.
        flash('El archivo físico ya no se encontró en el servidor; se eliminó el registro.', 'warning')

    db.session.delete(documento)
    db.session.commit()

    flash(f'Se eliminó el documento "{tipo_valor}".', 'success')
    return redirect(url_for('documentos.documentos', matricula=matricula))
