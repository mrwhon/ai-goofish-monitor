import asyncio
import json
from types import SimpleNamespace
import pytest
from src.cubi_monitor.transport import ChatTransport
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
