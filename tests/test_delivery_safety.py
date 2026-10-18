import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from influencer_hub import channel_health, db, link_router, pipeline, selfcheck


@pytest.fixture
def destination():
    db.init()
    creator = db.add_influencer('Safety', 'creator-21')
    channel = db.add_channel(creator, 'telegram', '@safety', status='active')
    return creator, channel


def row(channel):
    return next(c for c in db.list_channels() if c['id'] == channel)


def streak(channel):
    con = db._connect()
    try:
        found = con.execute('SELECT consecutive FROM delivery_failures WHERE channel_id=?', (channel,)).fetchone()
        return found[0] if found else 0
    finally:
        con.close()


def test_three_permanent_only_and_audit(destination):
    _, ch = destination
    for n in (1, 2):
        db.record_delivery_result(ch, 'failed:ChatWriteForbiddenError')
        assert streak(ch) == n
        assert row(ch)['status'] == 'active'
    db.record_delivery_result(ch, 'failed:ChatWriteForbiddenError')
    assert row(ch)['status'] == 'paused'
    assert '3 consecutive permanent' in db.list_audit(action='channel_auto_paused')[0]['detail']
    db.update_channel_details(ch, status='active')
    assert streak(ch) == 0
    db.record_delivery_result(ch, 'failed:ChannelPrivateError')
    db.record_delivery_result(ch, 'posted')
    assert streak(ch) == 0


@pytest.mark.parametrize('result', ['failed:TimeoutError', 'failed:FloodWaitError',
    'failed:network disconnected', 'failed:unknown error', 'skipped', 'deferred'])
def test_nonpermanent_never_pauses(destination, result):
    _, ch = destination
    for _ in range(5):
        db.record_delivery_result(ch, result)
    assert row(ch)['status'] == 'active'
    assert streak(ch) == 0


def test_transient_breaks_streak_and_destinations_isolated(destination):
    creator, ch = destination
    other = db.add_channel(creator, 'telegram', '@other', status='active')
    db.record_delivery_result(ch, 'failed:ChannelPrivateError')
    db.record_delivery_result(ch, 'failed:TimeoutError')
    db.record_delivery_result(ch, 'failed:ChannelPrivateError')
    assert streak(ch) == 1
    assert streak(other) == 0


def test_watchdog_failure_does_not_break_delivery(destination, monkeypatch):
    creator, ch = destination
    async def send(*a, **kw):
        return 'posted'
    monkeypatch.setattr(pipeline, '_dispatch_to_channel', send)
    def broken(*a):
        raise RuntimeError('database temporarily unavailable')
    monkeypatch.setattr(db, 'record_delivery_result', broken)
    assert asyncio.run(pipeline.dispatch_to_channel({'id': creator}, row(ch), 'deal')) == 'posted'


def test_doctor_preview_and_transient_apply(destination, monkeypatch):
    _, ch = destination
    async def transient(*a):
        return {'ok': False, 'error': 'TimeoutError'}
    monkeypatch.setattr(channel_health.telegram_ops, 'inspect_channel', transient)
    assert asyncio.run(channel_health.scan(resolve=True, apply=True))[0]['action'] == 'deferred'
    assert row(ch)['status'] == 'active'
    async def denied(*a):
        return {'ok': True, 'can_post': False}
    monkeypatch.setattr(channel_health.telegram_ops, 'inspect_channel', denied)
    con = db._connect()
    before = list(con.iterdump())
    assert asyncio.run(channel_health.scan(resolve=True))[0]['action'] == 'would_pause'
    assert list(con.iterdump()) == before
    con.close()
    assert row(ch)['status'] == 'active'
    assert not db.list_audit(action='channel_fix')
    asyncio.run(channel_health.scan(resolve=True, apply=True))
    assert row(ch)['status'] == 'paused'


def test_nested_amazon_resigns_preserves_params():
    inner = 'https://www.amazon.in/dp/B012345678?tag=other-21&psc=1&custom=hello%20world&blank='
    outer = 'https://www.amazon.in/sspa/click?' + urlencode({'url': inner, 'spc': 'abc', 'x': '2'})
    result = link_router.apply_amazon_tag(outer, 'creator-21')
    params = parse_qs(urlparse(result).query)
    nested = parse_qs(urlparse(params['url'][0]).query, keep_blank_values=True)
    assert nested == {'tag': ['creator-21'], 'psc': ['1'], 'custom': ['hello world'], 'blank': ['']}
    assert params['spc'] == ['abc'] and params['x'] == ['2']


def test_heartbeat_starting_and_stale():
    import time
    assert 'starting' in str(selfcheck._worker_check({'state': 'starting', 'heartbeat_at': time.time()})).lower()
    assert 'stale' in str(selfcheck._worker_check({'state': 'running', 'heartbeat_at': time.time()-500})).lower()


def test_bundle_check_and_repeat():
    root = Path(__file__).resolve().parents[1]
    script = root / 'patches/apply-delivery-safety.py'
    for flag in ('--check', '--dry-run'):
        result = subprocess.run([sys.executable, str(script), flag], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    assert subprocess.run([sys.executable, str(script)], capture_output=True).returncode == 1


def test_dispatch_preserves_exception_class(destination, monkeypatch):
    creator, ch = destination
    class ChatWriteForbiddenError(Exception):
        pass
    async def denied(*args, **kwargs):
        raise ChatWriteForbiddenError('Posting is forbidden')
    monkeypatch.setattr(pipeline.telegram_ops, 'post_to_channel', denied)
    for _ in range(3):
        result = asyncio.run(pipeline.dispatch_to_channel({'id': creator}, row(ch), 'deal'))
        assert result.startswith('failed:ChatWriteForbiddenError:')
    assert row(ch)['status'] == 'paused'


def test_installer_fresh_apply_and_mismatch(tmp_path):
    import io
    import shutil
    import tarfile
    root = Path(__file__).resolve().parents[1]
    archive = subprocess.check_output(['git', 'archive', 'd288364a824cfea5d2ceae7881a16d92b7b5f07f'], cwd=root)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp_path)
    for name in ('apply-delivery-safety.py', 'delivery-safety.patch'):
        shutil.copyfile(root / 'patches' / name, tmp_path / 'patches' / name)
    script = tmp_path / 'patches/apply-delivery-safety.py'
    def run(*args):
        return subprocess.run([sys.executable, str(script), *args], cwd=tmp_path,
                              capture_output=True, text=True)
    source = tmp_path / 'influencer_hub/pipeline.py'
    original = source.read_bytes()
    assert run('--dry-run').returncode == 0
    assert source.read_bytes() == original
    assert run('--check').returncode == 1
    # One mismatched file must prevent changes to every other file.
    db_source = tmp_path / 'influencer_hub/db.py'
    db_original = db_source.read_bytes()
    source.write_text('broken target\n')
    assert run().returncode == 2
    assert db_source.read_bytes() == db_original
    source.write_bytes(original)
    applied = run()
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert run('--check').returncode == 0
    assert run().returncode == 1
