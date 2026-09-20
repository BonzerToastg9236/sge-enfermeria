"""Visor de la bitácora de auditoría (solo Dirección, solo lectura)."""

from flask import Blueprint, render_template, request

from extensiones import db
from modelos import BitacoraAuditoria
from utilidades.paginacion import pagina_valida
from utilidades.seguridad import rol_requerido

auditoria_bp = Blueprint('auditoria', __name__)

REGISTROS_POR_PAGINA = 50


@auditoria_bp.route('/auditoria')
@rol_requerido('DIRECTIVO')
def bitacora():
    accion = request.args.get('accion', '').strip()
    termino = request.args.get('q', '').strip()
    page = pagina_valida(request.args.get('page', 1, type=int))

    consulta = BitacoraAuditoria.query
    if accion:
        consulta = consulta.filter(BitacoraAuditoria.accion == accion)
    if termino:
        consulta = consulta.filter(BitacoraAuditoria.matricula_fk == termino.upper())
    paginacion = consulta.order_by(BitacoraAuditoria.fecha.desc(), BitacoraAuditoria.id.desc()).paginate(
        page=page, per_page=REGISTROS_POR_PAGINA, error_out=False)

    acciones = [a for (a,) in db.session.query(BitacoraAuditoria.accion).distinct().order_by(BitacoraAuditoria.accion).all()]
    return render_template('auditoria.html', paginacion=paginacion, acciones=acciones, accion=accion, termino=termino)
