"""Synthetic persistent candidate and live account/UI boundaries; no network."""
import asyncio
import copy
import json
import pytest
from src.cubi_monitor.browser import Browser, CaptureError, LOGIN_API
from src.cubi_monitor.collection import collect_candidates
from src.cubi_monitor.config import validate_config
from src.cubi_monitor.contracts import AnalysisResult
from src.cubi_monitor.ledger import Ledger
from src.cubi_monitor.policy import criteria_id


@pytest.fixture
def setup(tmp_path):
    m = validate_config({"schema":2,"monitors":[{"monitor_id":"camera",
        "queries":[{"keyword":"相机"}],"criteria":{"max_price":6000},
        "collection_policy":{"enabled":True,"expected_account_id":"77"}}]})['monitors'][0]
    ledger = Ledger(tmp_path/'ledger.sqlite3')
    ledger.register(m,criteria_id(m)); ledger.activate('camera')
    candidate(ledger,m,'123')
    yield ledger,m
    ledger.close()


def candidate(ledger,m,id):
    ledger.observe(m['monitor_id'],{'id':id,'title':'相机','price':5000},'detail')
    ledger.assess(m['monitor_id'],id,AnalysisResult('candidate','同规格基准之下'),criteria_id(m),m['evaluator_version'])


class Collector:
    def __init__(self, price=5000, state='added', fault=None):
        self.price,self.state,self.fault,self.calls,self.clicks=price,state,fault,[],0
    async def favorite(self,id,account,guard,before_click,allow_click):
        self.calls.append((id,account,allow_click))
        if not guard({'id':id,'title':'相机','price':self.price}): return 'stale'
        if self.fault=='preflight': raise CaptureError('collection_account_mismatch')
        if self.state=='already_saved': return self.state
        if not allow_click: return 'unconfirmed'
        assert before_click(); self.clicks+=1
        if self.fault=='after_click': raise CaptureError('collection_result_unconfirmed')
        if self.fault=='cancel': raise asyncio.CancelledError()
        return self.state


def test_added_dedupes_and_becomes_watch(setup):
    ledger,m=setup; b=Collector()
    assert asyncio.run(collect_candidates(ledger,m,b))['added']==1
    assert ledger.item('camera','123')['watch']==1
    assert json.loads(ledger.meta('collection:77:123'))['status']=='verified'
    asyncio.run(collect_candidates(ledger,m,b))
    assert len(b.calls)==b.clicks==1


@pytest.mark.parametrize('case',['disabled','stopped','expired','reserve','rejected','pending','criteria','revision','blocked','zero_budget'])
def test_ineligible_never_reads_or_writes(setup,case):
    ledger,m=setup
    if case=='disabled': m['collection_policy']['enabled']=False
    elif case=='stopped': ledger.db.execute('UPDATE monitors SET stopped=1')
    elif case=='expired': ledger.db.execute('UPDATE monitors SET activated_at=1,expires_at=2')
    elif case in ('reserve','rejected'): ledger.db.execute('UPDATE monitor_items SET decision=?',(case,))
    elif case=='pending': ledger.db.execute("UPDATE monitor_items SET pending='new'")
    elif case=='criteria': ledger.db.execute("UPDATE monitor_items SET criteria_hash='old'")
    elif case=='revision': ledger.db.execute("UPDATE monitor_items SET assessed_revision='old'")
    elif case=='blocked': ledger.meta('blocked','platform_boundary')
    b=Collector();asyncio.run(collect_candidates(ledger,m,b,detail_budget=0 if case=='zero_budget' else 1))
    assert not b.calls


def test_current_detail_invalidates_candidate(setup):
    ledger,m=setup;b=Collector(price=6500)
    assert asyncio.run(collect_candidates(ledger,m,b))['stale']==1
    assert b.clicks==0 and ledger.item('camera','123')['pending']


@pytest.mark.parametrize('fault',['after_click','cancel'])
def test_uncertain_and_interrupted_clicks_only_read_next_time(setup,fault):
    ledger,m=setup
    with pytest.raises((CaptureError,asyncio.CancelledError)):
        asyncio.run(collect_candidates(ledger,m,Collector(fault=fault)))
    assert json.loads(ledger.meta('collection:77:123'))['status']=='uncertain'
    b=Collector();assert asyncio.run(collect_candidates(ledger,m,b))['uncertain']==1
    assert b.calls==[('123','77',False)] and b.clicks==0
    asyncio.run(collect_candidates(ledger,m,b));assert len(b.calls)==1


def test_saved_uncertain_reconciles_and_two_tasks_share_receipt(setup):
    ledger,m=setup;ledger.meta('collection:77:123',json.dumps({'status':'attempting'}));b=Collector(state='already_saved')
    assert asyncio.run(collect_candidates(ledger,m,b))['already_saved']==1
    other={**copy.deepcopy(m),'monitor_id':'lens'}
    ledger.register(other,criteria_id(other));ledger.activate('lens');candidate(ledger,other,'123')
    asyncio.run(collect_candidates(ledger,other,b))
    assert len(b.calls)==1 and b.clicks==0


def test_preflight_is_not_uncertain_click(setup):
    ledger,m=setup
    with pytest.raises(CaptureError):asyncio.run(collect_candidates(ledger,m,Collector(fault='preflight')))
    assert json.loads(ledger.meta('collection:77:123'))['status']=='preflight_failed'


def test_bounded_selection(setup):
    ledger,m=setup
    for id in ('124','125'):candidate(ledger,m,id)
    b=Collector();asyncio.run(collect_candidates(ledger,m,b,detail_budget=1));assert len(b.calls)==1


class UI:
    def __init__(self,saved=False,duplicates=False,no_ack=False):
        self.url='https://www.goofish.com/item?id=123';self.saved,self.duplicates,self.no_ack,self.clicks=saved,duplicates,no_ack,0
    def get_by_text(self,text,exact=True):
        p=self
        class Control:
            async def count(self):return (2 if p.duplicates else 1) if (text=='已收藏')==p.saved else 0
            async def click(self,**kwargs):
                assert text=='收藏' and not p.saved;p.clicks+=1
                if not p.no_ack:p.saved=True
        return Control()


def ui_browser(monkeypatch,account=77,id='123',**kwargs):
    b=object.__new__(Browser);b.page=UI(**kwargs)
    async def wait(*a):pass
    async def detail(item_id):return {'id':item_id}
    async def payload(api):
        return {'ret':['SUCCESS::调用成功'],'data':{'userId':account} if api==LOGIN_API else {'itemDO':{'itemId':id,'title':'相机','soldPrice':5000}}}
    b.detail,b.boundary,b.payload=detail,wait,payload
    monkeypatch.setattr('src.cubi_monitor.browser.asyncio.sleep',wait)
    return b


@pytest.mark.parametrize('saved',[True,False])
def test_ui_only_adds_and_verifies(monkeypatch,saved):
    b=ui_browser(monkeypatch,saved=saved);intents=[]
    state=asyncio.run(b.favorite('123','77',lambda f:True,lambda:intents.append(1) or True))
    assert state==('already_saved' if saved else 'added')
    assert b.page.clicks==(0 if saved else 1) and len(intents)==b.page.clicks


@pytest.mark.parametrize('kwargs,error',[({'account':78},'collection_account_mismatch'),({'account':None},'collection_account_mismatch'),({'id':'456'},'detail_identity_or_schema_changed'),({'duplicates':True},'collection_control_changed')])
def test_identity_or_ambiguous_control_prevents_click(monkeypatch,kwargs,error):
    b=ui_browser(monkeypatch,**kwargs)
    with pytest.raises(CaptureError,match=error):asyncio.run(b.favorite('123','77',lambda f:True,lambda:pytest.fail('intent')))
    assert b.page.clicks==0


def test_ui_unconfirmed_does_not_reclick(monkeypatch):
    b=ui_browser(monkeypatch,no_ack=True)
    with pytest.raises(CaptureError,match='collection_result_unconfirmed'):asyncio.run(b.favorite('123','77',lambda f:True,lambda:True))
    assert b.page.clicks==1
    assert asyncio.run(b.favorite('123','77',lambda f:True,lambda:pytest.fail('intent'),allow_click=False))=='unconfirmed'
    assert b.page.clicks==1


@pytest.mark.parametrize('policy',[{'enabled':'yes'},{'enabled':True},{'enabled':True,'expected_account_id':'bad'},{'max_per_run':4}])
def test_configuration_fails_closed(policy):
    with pytest.raises(ValueError,match='collection'):
        validate_config({'schema':2,'monitors':[{'monitor_id':'a','queries':[{'keyword':'q'}],'criteria':{},'collection_policy':policy}]})


@pytest.mark.parametrize('prepare,decision',[(True,'candidate'),(False,'reserve'),(False,'candidate')])
def test_real_scan_invokes_collection_only_live_candidate(setup,monkeypatch,prepare,decision):
    from src.cubi_monitor.cli import scan
    ledger,m=setup
    m['analysis_budget']['calls']=1
    m['scan_budget']['details']=3
    ledger.db.execute("UPDATE monitor_items SET decision='reserve'")
    b=Collector()
    class ScanBrowser:
        def __init__(self,*args):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def search(self,*args):return ([{'id':'456','title':'相机','price':5000}],{'pages':1})
        async def detail(self,id):return {'id':id,'title':'相机','price':5000}
        async def favorite(self,*args,**kwargs):return await b.favorite(*args,**kwargs)
    class Analyzer:
        async def analyze(self,request):return AnalysisResult(decision,'同配置比较',comparison='已有依据')
    monkeypatch.setattr('src.cubi_monitor.cli.Browser',ScanBrowser)
    result=asyncio.run(scan(ledger,m,{'reader_file':'unused','cdp':'http://127.0.0.1:9222'},prepare=prepare,analyzer=Analyzer()))
    assert result['status']=='ok' and result['details']<=3
    assert b.clicks==(1 if not prepare and decision=='candidate' else 0)


def test_expiry_between_detail_and_click_prevents_write(setup):
    ledger,m=setup
    class Expiring(Collector):
        async def favorite(self,id,account,guard,before_click,allow_click):
            assert guard({'id':id,'title':'相机','price':5000})
            ledger.db.execute('UPDATE monitors SET activated_at=1,expires_at=2')
            assert not before_click()
            return 'stale'
    assert asyncio.run(collect_candidates(ledger,m,Expiring()))['stale']==1
    assert ledger.meta('collection:77:123') is None


def test_account_is_rechecked_immediately_before_click(monkeypatch):
    b=ui_browser(monkeypatch)
    original=b.payload;calls=0
    async def changed(api):
        nonlocal calls
        value=await original(api)
        if api==LOGIN_API:
            calls+=1
            if calls>1:value['data']['userId']=78
        return value
    b.payload=changed
    with pytest.raises(CaptureError,match='collection_account_mismatch'):
        asyncio.run(b.favorite('123','77',lambda f:True,lambda:pytest.fail('intent')))
    assert b.page.clicks==0
