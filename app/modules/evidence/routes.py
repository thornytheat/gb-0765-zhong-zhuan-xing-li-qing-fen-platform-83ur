from flask import Blueprint, jsonify
from app.modules.archive.routes import store
from app.pipeline import compute
from .bundle import certify
from .service import index, recover

bp = Blueprint('evidence', __name__)


@bp.get('/api/evidence/<run>')
def evidence(run):
    return jsonify(index(*store().get(run)))


@bp.post('/api/evidence/<run>/recover')
def resume(run):
    return jsonify(recover(store(), run, compute))


@bp.get('/api/evidence/<run>/export')
def export(run):
    response = jsonify(certify(*store().get(run)))
    response.headers['Content-Disposition'] = f'attachment; filename="transfer-{run}.json"'
    return response
