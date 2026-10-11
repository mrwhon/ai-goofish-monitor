"""Generic persistence and runtime contracts; all data synthetic and network mocked."""
import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace

import pytest
from src.cubi_monitor.config import validate_config
from src.cubi_monitor.contracts import AnalysisError, AnalysisResult
from src.cubi_monitor.ledger import Ledger
from src.cubi_monitor.policy import criteria_id
from src.cubi_monitor.notifications import flush, message
from src.cubi_monitor.cli import scan
from src.cubi_monitor.browser import CaptureError


@pytest.fixture
def monitor():
    return validate_config({"schema":2,"monitors":[{"monitor_id":"a","queries":[{"keyword":"示例商品"}],"criteria":{"goal":"示例","max_price":10000},"baseline_refs":[{"item":{"id":"999","title":"基准","price":5000}}]}]})["monitors"][0]


@pytest.fixture
def ledger(tmp_path, monitor):
    instance = Ledger(tmp_path/"test.sqlite3")
    instance.register(monitor,criteria_id(monitor))
    yield instance
    instance.close()


def assess(ledger,monitor,item_id="123",decision="reserve",notify=False):
    return ledger.assess(monitor["monitor_id"],item_id,AnalysisResult(decision,"有明确依据",comparison="同口径"),criteria_id(monitor),monitor["evaluator_version"],notify=notify)


def observed(ledger,monitor):
    item = {"id":"123","title":"示例","price":5000,"description":"资料","images":["a"]}
    ledger.observe("a",item,"q1")
    assess(ledger,monitor)
    return item


def test_id_dedupe_all_decisions_and_reopen(ledger,monitor):
    item = observed(ledger,monitor)
    assert ledger.observe("a",{**item,"views":999,"relative_time":"刚刚"},"q2") is None
    assert len(ledger.pending("a",10)) == 1  # Only initial baseline remains.
    ledger.observe("a",{**item,"id":"456"},"q1")
    for decision in ("reserve","candidate","rejected"):
        assess(ledger,monitor,"456",decision)
        assert ledger.observe("a",{**item,"id":"456"},"q3") is None
    second = Ledger(ledger.path)
    assert second.status("a")["items"] == 3
    second.close()


def test_cumulative_price_and_stable_content(ledger,monitor):
    observed(ledger,monitor)
    assert ledger.observe("a",{"id":"123","price":4900},"list") is None
    assert ledger.observe("a",{"id":"123","price":4800},"list") is None
    assert ledger.observe("a",{"id":"123","price":4700},"list") == "price_changed"
    assert json.loads(ledger.item("a","123")["snapshot"])["description"] == "资料"
    assess(ledger,monitor)
    assert ledger.observe("a",{"id":"123","description":"新资料"},"detail") == "content_changed"


def test_ceiling_crossing_below_percentage(ledger,monitor):
    monitor["criteria"]["max_price"] = 5010
    ledger.register(monitor,criteria_id(monitor))
    observed(ledger,monitor)
    assert ledger.observe("a",{"id":"123","price":5020},"list") == "price_changed"


def test_thumbnail_and_unknown_price_do_not_erase_details(ledger,monitor):
    observed(ledger,monitor)
    assert ledger.observe("a",{"id":"123","price":None,"thumbnail":"b","title":"截断标题"},"list") is None
    snapshot = json.loads(ledger.item("a","123")["snapshot"])
    assert snapshot["price"] == 5000 and snapshot["images"] == ["a"] and snapshot["title"] == "示例"


@pytest.mark.parametrize("price",[float('nan'),float('inf'),-1])
def test_invalid_price_no_partial_insert(ledger,price):
    with pytest.raises(ValueError):
        ledger.observe("a",{"id":"321","price":price},"query")
    assert ledger.item("a","321") is None


def test_quotes_and_registration_never_overwrite_observed_price(ledger,monitor):
    monitor["baseline_refs"] = [{"item":{"id":"123","price":6000},"quote":5500}]
    monitor["watch_items"] = ["123"]
    ledger.register(monitor,criteria_id(monitor))
    ledger.observe("a",{"id":"123","price":5700},"list")
    ledger.register(monitor,criteria_id(monitor))
    row = ledger.item("a","123")
    assert row["quote"] == 5500 and row["watch"] == 1
    assert json.loads(row["snapshot"])["price"] == 5700


def test_two_tasks_share_facts_but_decisions_and_criteria_isolate(ledger,monitor):
    other = {**copy.deepcopy(monitor),"monitor_id":"b"}
    ledger.register(other,criteria_id(other))
    observed(ledger,monitor)
    ledger.observe("b",{"id":"123"},"q2")
    assess(ledger,other,decision="rejected")
    assert ledger.item("a","123")["decision"] == "reserve"
    assert ledger.item("b","123")["decision"] == "rejected"
    monitor["criteria"]["goal"] = "更高目标"
    ledger.register(monitor,criteria_id(monitor))
    assert ledger.item("a","123")["pending"] == "criteria_changed"
    assert ledger.item("b","123")["pending"] is None
    ledger.observe("a",{"id":"123","price":4000},"list")
    assert ledger.item("b","123")["pending"] == "price_changed"
    assert ledger.db.execute("SELECT count(*) FROM items WHERE id='123'").fetchone()[0] == 1


def test_import_existing_fact_preserves_history_and_timestamps(ledger,monitor):
    observed(ledger,monitor)
    before = tuple(ledger.db.execute("SELECT * FROM items WHERE id='123'").fetchone())
    count = ledger.db.execute("SELECT count(*) FROM observations").fetchone()[0]
    other = {**copy.deepcopy(monitor),"monitor_id":"b","baseline_refs":[{"item":{"id":"123","title":"旧基准","price":6000},"quote":5500}],"watch_items":["123"]}
    ledger.register(other,criteria_id(other))
    assert tuple(ledger.db.execute("SELECT * FROM items WHERE id='123'").fetchone()) == before
    assert ledger.db.execute("SELECT count(*) FROM observations").fetchone()[0] == count
    assert ledger.item('b','123')['quote'] == 5500
    assert ledger.item('b','123')['watch'] == 1


def test_resume_preserves_original_expiry_and_cannot_restart_expired(ledger,monitor):
    with pytest.raises(ValueError,match='trial_not_resumable'):
        ledger.resume('a',1)
    ledger.activate('a',1)
    expiry = ledger.monitor('a')['expires_at']
    with ledger.db:
        ledger.db.execute("UPDATE monitors SET stopped=1 WHERE monitor_id='a'")
    ledger.resume('a',2)
    assert ledger.active('a',2)
    assert ledger.monitor('a')['expires_at'] == expiry
    with pytest.raises(ValueError,match='trial_not_resumable'):
        ledger.resume('a',expiry)


def test_atomic_assessment_outbox_and_stale_rejection(ledger,monitor):
    observed(ledger,monitor)
    ledger.db.execute("CREATE TRIGGER abort_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'failure'); END")
    ledger.observe("a",{"id":"123","price":4000},"list")
    with pytest.raises(sqlite3.IntegrityError):
        assess(ledger,monitor,decision="candidate",notify=True)
    assert ledger.item("a","123")["decision"] == "reserve"
    with pytest.raises(ValueError,match="stale"):
        ledger.assess("a","123",AnalysisResult("candidate","reason"),criteria_id(monitor),monitor["evaluator_version"],"old")


def test_relations_and_cross_task_notification_fk(ledger,monitor):
    observed(ledger,monitor)
    other = {**copy.deepcopy(monitor),"monitor_id":"b"}
    ledger.register(other,criteria_id(other))
    ledger.observe("b",{"id":"123"},"shared")
    aid = assess(ledger,monitor)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.enqueue("b","candidate",{},"bad","goofish:123",aid)
    ledger.db.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        ledger.observe("missing",{"id":"12"},"orphan")
    assert ledger.db.execute("SELECT count(*) FROM items WHERE id='12'").fetchone()[0] == 0


def test_immutable_expiry_stop_and_other_task(ledger,monitor):
    ledger.activate("a",1)
    assert ledger.active("a",14*86400) and not ledger.active("a",1+14*86400)
    with pytest.raises(ValueError): ledger.activate("a",2)
    other = {**copy.deepcopy(monitor),"monitor_id":"b"}
    ledger.register(other,criteria_id(other))
    ledger.activate("b",2)
    with ledger.db: ledger.db.execute("UPDATE monitors SET stopped=1 WHERE monitor_id='a'")
    assert not ledger.active("a",3) and ledger.active("b",3)


@pytest.mark.parametrize("rc,status",[(0,"sent"),(1,"pending"),(2,"uncertain")])
def test_notification_ack_no_reanalysis(ledger,monitor,monkeypatch,rc,status):
    observed(ledger,monitor)
    ledger.activate("a")
    assess(ledger,monitor,decision="candidate",notify=True)
    assess(ledger,monitor,decision="candidate",notify=True)
    calls = []
    monkeypatch.setattr("src.cubi_monitor.notifications.subprocess.run",lambda *a,**kw:calls.append(a) or SimpleNamespace(returncode=rc))
    flush(ledger,"a","/fake")
    assert ledger.status("a")["outbox"] == {status:1}
    if status != "pending":
        flush(ledger,"a","/fake")
        assert len(calls) == 1


def test_stale_superseded_and_crash_uncertain(ledger,monitor,monkeypatch):
    observed(ledger,monitor)
    ledger.activate("a")
    assess(ledger,monitor,decision="candidate",notify=True)
    ledger.observe("a",{"id":"123","price":4000},"list")
    monkeypatch.setattr("src.cubi_monitor.notifications.subprocess.run",lambda *a,**kw:pytest.fail("stale send"))
    assert flush(ledger,"a","/fake")["superseded"] == 1
    assert ledger.status("a")["outbox"] == {"superseded":1}


def test_message_html_escape():
    assert "&lt;script&gt;" in message({"title":"<script>","reason":"<&>","url":"https://example.invalid","quote":5500})
    assert "5500" in message({"title":"x","quote":5500})


@pytest.mark.parametrize("version",[3,99])
def test_unknown_schema_preserves_bytes(tmp_path,version):
    path = tmp_path/"foreign.sqlite3"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version={version}")
    db.close()
    before = path.read_bytes()
    with pytest.raises(ValueError,match="unsupported_schema"): Ledger(path)
    assert path.read_bytes() == before


def test_foreign_zero_schema_rejected(tmp_path):
    path = tmp_path/"foreign.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE business(id INTEGER)")
    db.close()
    with pytest.raises(ValueError,match="unknown_existing"): Ledger(path)


def test_prepare_live_counts_and_missed_run(ledger):
    ledger.activate("a",1)
    run = ledger.start_run("a","prepare",2)
    with ledger.db: ledger.db.execute("UPDATE runs SET status='ok',completed=3 WHERE id=?",(run,))
    assert ledger.run_count("a") == 0 and ledger.run_count("a","prepare") == 1
    assert ledger.health("a",37*3600)["reason"] == "missed_run"
    assert ledger.health("a",15*86400)["status"] == "inactive"


def test_queue_preserves_changed_plausible_and_unknown(ledger,monitor):
    observed(ledger,monitor)
    ledger.observe("a",{"id":"123","price":4000},"list")
    ledger.observe("a",{"id":"124","price":4000},"list")
    ledger.observe("a",{"id":"125","price":4000},"list")
    with ledger.db: ledger.db.execute("UPDATE monitor_items SET priority=2 WHERE item_key='goofish:124'")
    ids = [r["id"] for r in ledger.pending("a",3)]
    assert "123" in ids and "124" in ids
    assert any(i in ids for i in ("125","999"))


def test_inactive_blocked_no_browser(ledger,monitor,monkeypatch):
    monkeypatch.setattr("src.cubi_monitor.cli.Browser",lambda *a:pytest.fail("browser"))
    assert asyncio.run(scan(ledger,monitor,{}))["status"] == "inactive_or_expired"
    ledger.activate("a")
    ledger.meta("blocked","platform_boundary")
    assert asyncio.run(scan(ledger,monitor,{},prepare=True))["status"] == "blocked"


class FakeBrowser:
    closed = 0
    def __init__(self,*args): pass
    async def __aenter__(self): return self
    async def __aexit__(self,*args): type(self).closed += 1
    async def search(self,*args):
        return [{"id":"123","title":"未知新配置","price":4000}],{"empty":False,"truncated":True,"pages":1}
    async def detail(self,item_id): return {"id":item_id,"description":"详细资料","price":4000}


class FakeAnalyzer:
    calls = 0
    async def analyze(self,request):
        self.calls += 1
        return AnalysisResult("reserve","需核实",comparison="缺少完整规格",evidence_gaps=["图片未检查"])


def test_unknown_gets_ai_and_replay_zero_calls(ledger,monitor,monkeypatch):
    monkeypatch.setattr("src.cubi_monitor.cli.Browser",FakeBrowser)
    monitor["baseline_refs"] = []
    ledger.register(monitor,criteria_id(monitor))
    analyzer = FakeAnalyzer()
    runtime = {"reader_file":"unused","cdp":"http://127.0.0.1:9222"}
    first = asyncio.run(scan(ledger,monitor,runtime,prepare=True,analyzer=analyzer))
    calls = analyzer.calls
    second = asyncio.run(scan(ledger,monitor,runtime,prepare=True,analyzer=analyzer))
    assert first["status"] == "ok" and calls > 0
    assert second["analyses"] == 0 and analyzer.calls == calls
    assert ledger.run_count("a") == 0 and not ledger.active("a")
    assert ledger.status("a")["outbox"] == {}


def test_ai_failure_keeps_pending_and_owned_close(ledger,monitor,monkeypatch):
    class Failure(FakeAnalyzer):
        async def analyze(self,request): raise AnalysisError("network")
    monkeypatch.setattr("src.cubi_monitor.cli.Browser",FakeBrowser)
    result = asyncio.run(scan(ledger,monitor,{"reader_file":"unused","cdp":"http://127.0.0.1:9222"},prepare=True,analyzer=Failure()))
    assert result["status"] == "partial" and ledger.item("a","123")["pending"]
    assert ledger.item("a","123")["assessed_revision"] is None
    assert FakeBrowser.closed > 0


def test_platform_boundary_blocks_all_future_attempts(ledger,monitor,monkeypatch):
    class Boundary(FakeBrowser):
        async def search(self,*args): raise CaptureError("platform_boundary")
    monkeypatch.setattr("src.cubi_monitor.cli.Browser",Boundary)
    result = asyncio.run(scan(ledger,monitor,{"reader_file":"unused","cdp":"http://127.0.0.1:9222"},prepare=True,analyzer=FakeAnalyzer()))
    assert result["status"] == "failed" and ledger.meta("blocked")
    assert asyncio.run(scan(ledger,monitor,{},prepare=True))["status"] == "blocked"


def test_expiry_summary_survives_pending_candidate_and_sends_once(ledger,monitor,monkeypatch):
    from src.cubi_monitor.cli import finalize_expiry
    observed(ledger,monitor)
    ledger.activate('a',1)
    assess(ledger,monitor,decision='candidate',notify=True)
    now=1+14*86400
    finalize_expiry(ledger,monitor,now)
    finalize_expiry(ledger,monitor,now)
    calls=[]
    monkeypatch.setattr('src.cubi_monitor.notifications.subprocess.run',lambda *a,**kw:calls.append(a) or SimpleNamespace(returncode=0))
    assert flush(ledger,'a','/fake',now)['sent']==1
    assert len(calls)==1 and ledger.status('a')['outbox']=={'sent':1,'superseded':1}
    assert flush(ledger,'a','/fake',now)['sent']==0


def test_interrupted_run_recovered_under_exclusive_owner(ledger):
    first=ledger.start_run('a','live',1)
    ledger.start_run('a','live',2)
    assert ledger.db.execute('SELECT status FROM runs WHERE id=?',(first,)).fetchone()[0]=='interrupted'


def test_database_lock_makes_concurrent_process_quiet(tmp_path,monkeypatch,capsys):
    import fcntl
    from src.cubi_monitor.cli import main
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'schema':2,'runtime':{},'monitors':[{'monitor_id':'a','queries':[{'keyword':'商品'}],'criteria':{}}]}))
    db=tmp_path/'db.sqlite3'
    with open(str(db)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert main(['--config',str(config),'--database',str(db),'status'])==0
    assert 'already_running' in capsys.readouterr().out and not db.exists()


def test_external_reader_challenge_also_blocks_future_capture(ledger,monitor,monkeypatch):
    class ReadError(RuntimeError): pass
    class Boundary(FakeBrowser):
        async def search(self,*args): raise ReadError('visible_login_or_challenge')
    monkeypatch.setattr('src.cubi_monitor.cli.Browser',Boundary)
    result=asyncio.run(scan(ledger,monitor,{'reader_file':'unused','cdp':'http://127.0.0.1:9222'},prepare=True,analyzer=FakeAnalyzer()))
    assert result['status']=='failed' and ledger.meta('blocked')=='platform_boundary'
    assert result['error']=='ReadError:visible_login_or_challenge'
