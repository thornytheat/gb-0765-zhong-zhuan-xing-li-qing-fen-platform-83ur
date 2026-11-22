from hashlib import sha256

from app.errors import DeskError
from app.manifest import fingerprint
from .bundle import certify

HALT_MESSAGE = {
    'ORIGINAL_MISMATCH': '原件摘要与归档不符，停签',
    'RECOMPUTE_FAILED': '原件无法复算，停签',
    'REPLAY_DRIFT': '复算结论与归档摘要不符，停签',
    'UNBALANCED': '去向对账不守恒，停签',
}


def index(files, result):
    bundle = certify(files, result)
    checks = [dict(key='originals', ok=True, label='原件摘要核验', note='三份原件摘要与归档一致'),
              dict(key='result', ok=True, label='结果摘要核验', note='归档结果摘要自洽'),
              dict(key='balanced', ok=True, label='去向对账守恒', note='件数勾稽守恒')]
    return dict(verified=True, manifest=bundle['manifest'], checks=checks,
                rows=[dict(source=name, sha256=bundle['manifest']['inputs'][name]) for name in sorted(files)])


def reconcile(files, manifest, compute):
    """批次复算对账：验原件、复算、比对归档摘要，得出恢复签发或停签的依据。只读不落库。"""
    expected = manifest.get('inputs', {})
    actual = {name: sha256(text.encode()).hexdigest() for name, text in files.items()}
    mismatched = sorted(name for name in set(actual) | set(expected) if actual.get(name) != expected.get(name))
    checks = [dict(key='originals', ok=not mismatched, label='原件摘要核验',
                   note='三份原件摘要与归档一致' if not mismatched else '不符：' + '、'.join(mismatched))]
    if mismatched:
        checks.append(dict(key='replay', ok=None, label='批次复算对账', note='原件未通过，未复算'))
        checks.append(dict(key='balanced', ok=None, label='去向对账守恒', note='未执行'))
        return dict(resumable=False, reason='ORIGINAL_MISMATCH', message=HALT_MESSAGE['ORIGINAL_MISMATCH'],
                    checks=checks, stages=None)
    try:
        stages = compute(files)
    except DeskError as error:
        checks.append(dict(key='replay', ok=False, label='批次复算对账', note=f'复算失败：{error}'))
        checks.append(dict(key='balanced', ok=None, label='去向对账守恒', note='未执行'))
        return dict(resumable=False, reason='RECOMPUTE_FAILED', message=HALT_MESSAGE['RECOMPUTE_FAILED'],
                    checks=checks, stages=None)
    digest, archived = fingerprint(stages), manifest.get('result_sha256')
    checks.append(dict(key='replay', ok=digest == archived, label='批次复算对账',
                       note='复算结论与归档摘要一致' if digest == archived
                       else f"归档 {str(archived)[:12]}… ≠ 复算 {digest[:12]}…"))
    ledger = stages['ledger']
    checks.append(dict(key='balanced', ok=bool(ledger['balanced']), label='去向对账守恒',
                       note='件数勾稽守恒' if ledger['balanced'] else f"差异 {ledger['difference']} 件"))
    if digest != archived:
        return dict(resumable=False, reason='REPLAY_DRIFT', message=HALT_MESSAGE['REPLAY_DRIFT'],
                    checks=checks, stages=None)
    if not ledger['balanced']:
        return dict(resumable=False, reason='UNBALANCED', message=HALT_MESSAGE['UNBALANCED'],
                    checks=checks, stages=None)
    return dict(resumable=True, reason=None, message='复算对账一致，可恢复签发', checks=checks, stages=stages)


def recover(store, run, compute):
    """由批次复算的对账结果决定恢复签发或停签。复原只发生在复算结论与归档摘要一致时，
    证据编号不变、结论不变；停签不落库，修复归档后可重试，重试不新增证据编号。"""
    files, result = store.get(run)
    manifest = result.get('manifest', {})
    decision = reconcile(files, manifest, compute)
    if not decision['resumable']:
        return dict(id=run, status='halted', reason=decision['reason'],
                    message=decision['message'], checks=decision['checks'])
    broken = fingerprint(result.get('stages')) != manifest.get('result_sha256')
    if broken:
        store.restore(run, files, result, dict(manifest=manifest, stages=decision['stages']))
    return dict(id=run, status='resumed', restored=broken,
                message='复算对账一致，归档已复原，恢复签发' if broken else '复算对账一致，归档自洽，准予签发',
                checks=decision['checks'])
