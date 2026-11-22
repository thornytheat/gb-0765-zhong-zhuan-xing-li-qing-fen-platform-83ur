from contextlib import closing

import pytest
from app.errors import DeskError
from app.manifest import canonical


def tamper(store, run, mutate):
    """模拟归档损坏：直接改库里的原件或结果。"""
    files, result = store.get(run)
    mutate(files, result)
    with closing(store.connect()) as db, db:
        db.execute('UPDATE runs SET inputs=?, result=? WHERE id=?', (canonical(files), canonical(result), run))


def archive_store(desk):
    return desk.application.extensions['transfer_archive']


def test_broken_result_recovers_with_same_number(desk, saved):
    store = archive_store(desk)
    tamper(store, saved['id'], lambda files, result: result['stages']['ledger']['counts'].update(loaded=99))
    assert desk.get(f"/api/evidence/{saved['id']}/export").status_code == 409
    recovered = desk.post(f"/api/evidence/{saved['id']}/recover").json
    assert (recovered['status'], recovered['restored']) == ('resumed', True)
    assert recovered['id'] == saved['id']
    assert desk.get(f"/api/evidence/{saved['id']}/export").status_code == 200
    assert desk.get(f"/api/evidence/{saved['id']}").json['manifest'] == saved['manifest']
    assert desk.get('/api/archive').json['count'] == 1


def test_healthy_archive_recover_is_noop(desk, saved):
    recovered = desk.post(f"/api/evidence/{saved['id']}/recover").json
    assert (recovered['status'], recovered['restored']) == ('resumed', False)
    assert desk.get(f"/api/archive/{saved['id']}").json['manifest'] == saved['manifest']


def test_original_mismatch_halts_without_writing(desk, saved):
    store = archive_store(desk)
    before = store.get(saved['id'])
    tamper(store, saved['id'], lambda files, result: files.update(**{'legs.csv': files['legs.csv'] + '\n'}))
    decision = desk.post(f"/api/evidence/{saved['id']}/recover").json
    assert (decision['status'], decision['reason']) == ('halted', 'ORIGINAL_MISMATCH')
    checks = {check['key']: check['ok'] for check in decision['checks']}
    assert checks['originals'] is False and checks['replay'] is None
    assert store.get(saved['id']) != before  # 损坏仍在
    assert desk.get(f"/api/evidence/{saved['id']}/export").status_code == 409
    assert desk.get('/api/archive').json['count'] == 1


def test_replay_drift_halts_and_conclusion_untouched(desk, saved):
    store = archive_store(desk)
    tamper(store, saved['id'], lambda files, result: result['manifest'].update(result_sha256='0' * 64))
    decision = desk.post(f"/api/evidence/{saved['id']}/recover").json
    assert (decision['status'], decision['reason']) == ('halted', 'REPLAY_DRIFT')
    assert desk.get(f"/api/archive/{saved['id']}").json['manifest']['result_sha256'] == '0' * 64
    assert desk.get('/api/archive').json['count'] == 1


def test_retry_after_repair_recovers_same_number(desk, saved):
    store = archive_store(desk)
    original = store.get(saved['id'])
    tamper(store, saved['id'], lambda files, result: files.update(**{'scans.csv': ''}))
    assert desk.post(f"/api/evidence/{saved['id']}/recover").json['status'] == 'halted'
    tamper(store, saved['id'], lambda files, result: files.update(**{'scans.csv': original[0]['scans.csv']}))
    recovered = desk.post(f"/api/evidence/{saved['id']}/recover").json
    assert recovered['status'] == 'resumed' and recovered['id'] == saved['id']
    assert desk.get('/api/archive').json['count'] == 1
    assert desk.get(f"/api/evidence/{saved['id']}/export").status_code == 200


def test_recover_missing_run(desk):
    result = desk.post('/api/evidence/unknown/recover')
    assert (result.status_code, result.json['code']) == (404, 'NO_RUN')


def test_restore_refuses_row_changed_under_it(desk, saved):
    store = archive_store(desk)
    files, result = store.get(saved['id'])
    with pytest.raises(DeskError, match='重试'):
        store.restore(saved['id'], files, dict(result, manifest=dict(result['manifest'], elapsed_us=1)), result)
    assert store.get(saved['id']) == (files, result)


def test_evidence_page_shows_halt_basis(desk, saved):
    store = archive_store(desk)
    tamper(store, saved['id'], lambda files, result: files.update(**{'manifest.csv': files['manifest.csv'] + '\n'}))
    page = desk.get(f"/desk/evidence?run={saved['id']}")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert '停签' in text and '原件摘要核验' in text and '批次复算对账' in text and '重试恢复' in text


def test_evidence_page_offers_recovery_when_reproducible(desk, saved):
    store = archive_store(desk)
    tamper(store, saved['id'], lambda files, result: result['stages']['ledger'].update(difference=8))
    page = desk.get(f"/desk/evidence?run={saved['id']}")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert '可恢复签发' in text and '恢复签发' in text


def test_evidence_page_shows_issuance_basis(desk, saved):
    text = desk.get(f"/desk/evidence?run={saved['id']}").get_data(as_text=True)
    assert '证据摘要已核验' in text and '原件摘要核验' in text and '去向对账守恒' in text
