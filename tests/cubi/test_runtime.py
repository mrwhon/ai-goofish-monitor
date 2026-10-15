"""Runtime preparation executes under the real ledger lock, without external I/O."""
import fcntl
import json

import pytest
from playwright.async_api import Error, TimeoutError as BrowserTimeout

from src.cubi_monitor import cli
from src.cubi_monitor.config import validate_config
from src.cubi_monitor.contracts import RuntimePreparationError
from src.cubi_monitor.ledger import Ledger
from src.cubi_monitor.policy import criteria_id


def setup_batch(tmp_path, state='active'):
    config = validate_config({'schema': 2, 'runtime': {'sender': 'unused'}, 'monitors': [
        {'monitor_id': name, 'queries': [{'keyword': name}], 'criteria': {}}
        for name in ('pc', 'phone')]})
    path = tmp_path / 'db.sqlite3'
    with_ledger = Ledger(path)
    for m in config['monitors']:
        with_ledger.register(m, criteria_id(m))
        if state != 'unactivated':
            with_ledger.activate(m['monitor_id'], now=1 if state == 'expired' else None)
    if state == 'blocked':
        with_ledger.meta('blocked', 'platform_boundary')
    if state == 'stopped':
        with with_ledger.db:
            with_ledger.db.execute('UPDATE monitors SET stopped=1')
    with_ledger.close()
    file = tmp_path / 'config.json'
    file.write_text(json.dumps(config))
    return ['--config', str(file), '--database', str(path)], path


def test_preparation_is_once_locked_and_failed_batch_never_collects(tmp_path, monkeypatch, capsys):
    args, path = setup_batch(tmp_path)
    calls = []
    def prepare(runtime):
        with open(str(path) + '.lock') as competing:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(runtime)
        raise RuntimePreparationError('browser_unavailable')
    monkeypatch.setattr(cli, 'Browser', lambda *a: pytest.fail('browser'))
    monkeypatch.setattr(cli, 'Analyzer', lambda *a, **kw: pytest.fail('AI'))
    monkeypatch.setattr(cli, 'flush', lambda *a: {})
    assert cli.main([*args, 'run'], prepare_runtime=prepare, scheduled_failure_code=10) == 10
    assert len(calls) == 1
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 2
    assert all(r['status'] == 'failed' and r['details'] == r['analyses'] == 0 for r in results)
    db = Ledger(path)
    assert db.db.execute("SELECT count(*) FROM runs WHERE status='failed' AND error='RuntimePreparationError:browser_unavailable'").fetchone()[0] == 2
    db.close()


@pytest.mark.parametrize('state', ['expired', 'unactivated', 'stopped', 'blocked'])
def test_ineligible_batch_never_prepares(tmp_path, monkeypatch, state):
    args, _ = setup_batch(tmp_path, state)
    monkeypatch.setattr(cli, 'flush', lambda *a: {})
    cli.main([*args, 'run'], prepare_runtime=lambda runtime: pytest.fail('prepare'))


def test_successful_prepare_does_not_replay_an_executed_task(tmp_path, monkeypatch):
    args, _ = setup_batch(tmp_path)
    calls = []
    async def capture(ledger, monitor, runtime, *a, **kw):
        calls.append(monitor['monitor_id'])
        return {'status': 'failed', 'error': 'browser_disconnected', 'coverage': [{'pages': 1}], 'details': 1}
    monkeypatch.setattr(cli, 'scan', capture)
    monkeypatch.setattr(cli, 'flush', lambda *a: {})
    prepared = []
    assert cli.main([*args, 'run'], prepare_runtime=lambda r: prepared.append(1), scheduled_failure_code=10) == 10
    assert calls == ['pc', 'phone'] and prepared == [1]


def test_unexpected_preparation_error_keeps_crash_fallback(tmp_path):
    args, _ = setup_batch(tmp_path)
    def broken(runtime):
        raise RuntimeError('unexpected')
    with pytest.raises(RuntimeError, match='unexpected'):
        cli.main([*args, 'run'], prepare_runtime=broken, scheduled_failure_code=10)


@pytest.mark.parametrize('exc,stage,expected', [
    (Error('connect ECONNREFUSED http://secret.invalid/?token=private'), 'browser_connect', 'browser_unavailable'),
    (BrowserTimeout('private diagnostics'), 'browser_connect', 'browser_timeout'),
    (Error('Target page, context or browser has been closed'), 'capture', 'browser_disconnected'),
    (TimeoutError('private URL'), 'capture', 'capture_timeout'),
    (Error('unknown private page error'), 'capture', 'page_read_failed'),
])
def test_transport_error_classification_never_persists_raw_message(exc, stage, expected):
    assert cli.safe_error(exc, stage) == expected


@pytest.mark.parametrize('command,host_code,delivery,expected', [
    ('run', 1, {}, 1), ('run', 10, {}, 10), ('scan', 10, {}, 1),
    ('run', 10, {'uncertain': 1}, 1), ('run', 10, {'retry': 1}, 1),
])
def test_host_exit_code_is_explicit_and_preserves_delivery_fallback(tmp_path, monkeypatch, command, host_code, delivery, expected):
    args, _ = setup_batch(tmp_path)
    monkeypatch.setattr(cli, 'flush', lambda *a: delivery)
    def prepare(runtime):
        raise RuntimePreparationError('browser_timeout')
    assert cli.main([*args, command], prepare_runtime=prepare, scheduled_failure_code=host_code) == expected


def test_initial_connection_failure_short_circuits_remaining_tasks(tmp_path, monkeypatch, capsys):
    args, _ = setup_batch(tmp_path)
    entered = []
    class Offline:
        def __init__(self, *a): pass
        async def __aenter__(self):
            entered.append(1)
            raise Error('ECONNREFUSED secret URL')
        async def __aexit__(self, *a): pass
    monkeypatch.setattr(cli, 'Browser', Offline)
    monkeypatch.setattr(cli, 'configured_transport', lambda *a: None)
    monkeypatch.setattr(cli, 'Analyzer', lambda *a, **kw: None)
    monkeypatch.setattr(cli, 'flush', lambda *a: {})
    # Only the real scan body is used; provide its runtime parameters.
    config = json.loads((tmp_path / 'config.json').read_text())
    config['runtime'].update(ai={}, cdp='http://127.0.0.1:9999', reader_file='unused')
    (tmp_path / 'config.json').write_text(json.dumps(config))
    assert cli.main([*args, 'run'], scheduled_failure_code=10) == 10
    assert entered == [1]
    results = json.loads(capsys.readouterr().out)
    assert results[0]['error'] == 'browser_unavailable'
    assert results[1]['error'] == 'RuntimePreparationError:browser_unavailable'


def test_read_only_verification_never_calls_collection_writer(tmp_path, monkeypatch, capsys):
    args, _ = setup_batch(tmp_path)
    class ReadOnly:
        def __init__(self, *a): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def search(self, *a): return [], {'pages': 1, 'empty': True}
    monkeypatch.setattr(cli, 'Browser', ReadOnly)
    monkeypatch.setattr(cli, 'configured_transport', lambda *a: None)
    monkeypatch.setattr(cli, 'Analyzer', lambda *a, **kw: None)
    monkeypatch.setattr(cli, 'flush', lambda *a: {})
    monkeypatch.setattr(cli, 'collect_candidates', lambda *a, **kw: pytest.fail('favorite writer'))
    config = json.loads((tmp_path / 'config.json').read_text())
    config['runtime'].update(ai={}, cdp='http://127.0.0.1:9999', reader_file='unused')
    (tmp_path / 'config.json').write_text(json.dumps(config))
    assert cli.main([*args, 'run', '--no-collection']) == 0
    assert all(r['status'] == 'ok' and r['collection'] is None for r in json.loads(capsys.readouterr().out))
