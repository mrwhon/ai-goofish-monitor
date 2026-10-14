import asyncio
import json
from types import SimpleNamespace
import pytest
from src.cubi_monitor.transport import ChatTransport, ClaudeTransport, configured_transport
from src.cubi_monitor.contracts import AnalysisError


class Memory:
    values={}
    def __init__(self): self.values={}
    def meta(self,key,value=None):
        if value is not None: self.values[key]=str(value)
        return self.values.get(key)


def settings():
    return {'url':'https://api.example.invalid/chat/completions','model':'configured','key_env':'FAKE_API_KEY','cost_cap_usd':3,'input_usd_per_million_ceiling':.6,'output_usd_per_million_ceiling':2.4}


def test_transport_bounded_no_tools_and_usage(monkeypatch):
    calls=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,bound):
            assert bound == 200001
            return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],'model':'served','usage':{'prompt_tokens':20,'completion_tokens':2}}).encode()
    monkeypatch.setenv('FAKE_API_KEY','synthetic-secret')
    monkeypatch.setattr('src.cubi_monitor.transport.urlopen',lambda req,**kw:calls.append((req,kw)) or Response())
    ledger=Memory()
    transport=ChatTransport(settings(),ledger)
    assert asyncio.run(transport('trusted','untrusted',500)) == '{}'
    payload=json.loads(calls[0][0].data)
    assert payload['max_tokens']==500 and payload['thinking']['type']=='disabled'
    assert 'tools' not in payload and calls[0][1]['timeout']==90
    assert 'synthetic-secret' not in ledger.values['ai_last_receipt']
    assert float(ledger.values['ai_reserved_usd'])>0


def test_transport_budget_or_missing_key_makes_no_call(monkeypatch):
    monkeypatch.setattr('src.cubi_monitor.transport.urlopen',lambda *a,**kw:pytest.fail('network'))
    ledger=Memory()
    ledger.meta('ai_reserved_usd',3)
    with pytest.raises(AnalysisError,match='cost_cap'):
        asyncio.run(ChatTransport(settings(),ledger)('x','y',500))
    ledger=Memory()
    monkeypatch.delenv('FAKE_API_KEY',raising=False)
    with pytest.raises(AnalysisError,match='credential'):
        asyncio.run(ChatTransport(settings(),ledger)('x','y',500))


def test_transport_uncertain_network_consumes_budget_no_retry(monkeypatch):
    monkeypatch.setenv('FAKE_API_KEY','synthetic')
    calls=[]
    def fail(*a,**kw):
        calls.append(1)
        raise TimeoutError('request has sensitive details')
    monkeypatch.setattr('src.cubi_monitor.transport.urlopen',fail)
    ledger=Memory()
    with pytest.raises(AnalysisError,match='ai_network_failure'):
        asyncio.run(ChatTransport(settings(),ledger)('x','y',500))
    assert len(calls)==1 and float(ledger.meta('ai_reserved_usd'))>0


def cli_settings():
    return {'transport': 'claude-cli', 'launcher': '/host/claude-glm', 'model': 'configured-glm', 'timeout': 120}


def cli_result():
    return {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1,
            'result': '{}', 'modelUsage': {'configured-glm': {'inputTokens': 20, 'outputTokens': 2}}}


def install_cli(monkeypatch, value=None, failure=None, client_model='configured-glm', served_model='configured-glm', content=None):
    calls, killed = [], []
    class Process:
        pid = 999999
        returncode = None
        async def communicate(self, data):
            calls[-1]['input'] = data
            if failure == 'timeout':
                raise asyncio.TimeoutError()
            if failure == 'cancel':
                raise asyncio.CancelledError()
            result = value or cli_result()
            events = [{'type': 'system', 'subtype': 'init', 'model': client_model},
                      {'type': 'assistant', 'message': {'model': served_model, 'content': content or [{'type': 'text', 'text': '{}'}]}}, result]
            calls[-1]['stdout'].write('\n'.join(json.dumps(e) for e in events).encode())
            self.returncode = 1 if failure == 'exit' else 0
        async def wait(self):
            self.returncode = -9
    async def create(*args, **kwargs):
        calls.append({'command': args, **kwargs})
        return Process()
    monkeypatch.setattr('src.cubi_monitor.transport.asyncio.create_subprocess_exec', create)
    monkeypatch.setattr('src.cubi_monitor.transport.os.killpg', lambda pid, sig: killed.append(pid))
    return calls, killed


def test_glm_cli_isolated_single_turn_and_receipt(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'unrelated-secret')
    calls, _ = install_cli(monkeypatch)
    ledger = Memory()
    assert asyncio.run(configured_transport(cli_settings(), ledger)('trusted', 'untrusted', 500)) == '{}'
    call = calls[0]; command = call['command']
    assert command[0] == '/host/claude-glm' and command[command.index('--tools') + 1] == ''
    assert command[command.index('--max-turns') + 1] == '1'
    assert command[command.index('--mcp-config') + 1] == '{"mcpServers":{}}'
    assert '--bare' in command and '--no-session-persistence' in command
    assert not any('KEY' in key or key.startswith('ANTHROPIC') for key in call['env'])
    assert call['input'] == b'untrusted'
    receipt = json.loads(ledger.meta('ai_last_receipt'))
    assert receipt['served_model'] == 'configured-glm' and receipt['usage']['outputTokens'] == 2


@pytest.mark.parametrize('field,value', [('num_turns', 2), ('is_error', True), ('modelUsage', {'other': {'outputTokens': 2}}), ('result', None)])
def test_glm_cli_bad_result_never_becomes_success(monkeypatch, field, value):
    result = cli_result(); result[field] = value
    calls, _ = install_cli(monkeypatch, result)
    ledger = Memory()
    with pytest.raises(AnalysisError):
        asyncio.run(ClaudeTransport(cli_settings(), ledger)('x', 'y', 500))
    assert len(calls) == 1 and ledger.meta('ai_last_receipt') is None


def test_cli_client_alias_and_actual_wire_model_are_separate(monkeypatch):
    result=cli_result();result['modelUsage']={'client-profile':{'inputTokens':20,'outputTokens':2}}
    install_cli(monkeypatch,result,client_model='client-profile')
    ledger=Memory()
    assert asyncio.run(ClaudeTransport(cli_settings(),ledger)('x','y',500)) == '{}'
    receipt=json.loads(ledger.meta('ai_last_receipt'))
    assert receipt['client_model']=='client-profile' and receipt['served_model']=='configured-glm'


@pytest.mark.parametrize('fault', ['wire-model', 'tool-use'])
def test_wrong_wire_model_or_tool_use_never_accepted(monkeypatch,fault):
    install_cli(monkeypatch,served_model='other' if fault=='wire-model' else 'configured-glm',
                content=[{'type':'tool_use','name':'Bash'}] if fault=='tool-use' else None)
    with pytest.raises(AnalysisError):
        asyncio.run(ClaudeTransport(cli_settings(),Memory())('x','y',500))


@pytest.mark.parametrize('failure', ['exit', 'timeout', 'cancel'])
def test_glm_cli_failure_no_retry_and_timeout_cleans_process(monkeypatch, failure):
    calls, killed = install_cli(monkeypatch, failure=failure)
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else AnalysisError):
        asyncio.run(ClaudeTransport(cli_settings(), Memory())('x', 'y', 500))
    assert len(calls) == 1
    assert killed == ([] if failure == 'exit' else [999999])


def test_unknown_transport_is_not_api_fallback():
    with pytest.raises(ValueError, match='unsupported_ai_transport'):
        configured_transport({'transport': 'misspelled'}, Memory())
