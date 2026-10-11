"""Generic incremental contracts, with synthetic camera/furniture data only."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from src.cubi_monitor.cli import scan, search_plan, rule_triage
from src.cubi_monitor.config import validate_config
from src.cubi_monitor.contracts import AnalysisResult
from src.cubi_monitor.ledger import Ledger
from src.cubi_monitor.notifications import flush, message
from src.cubi_monitor.policy import criteria_id, triage


@pytest.fixture
def setup(tmp_path):
    m = validate_config({"schema": 2, "monitors": [{"monitor_id": "camera", "queries": [{"keyword": "相机"}], "criteria": {"goal": "摄影", "rules": [{"pattern": "相机", "ceiling": 4000, "group": "camera"}, {"pattern": "镜头", "ceiling": 2000, "group": "lens"}]}}]})["monitors"][0]
    ledger = Ledger(tmp_path / "monitor.sqlite3")
    ledger.register(m, criteria_id(m))
    yield ledger, m
    ledger.close()


def decide(ledger, m, item, decision="reserve"):
    ledger.observe(m["monitor_id"], item, "detail")
    ledger.assess(m["monitor_id"], item["id"], AnalysisResult(decision, "合成理由"), criteria_id(m), m["evaluator_version"])


def test_generic_default_and_daily_dual_sort(setup):
    _, m = setup
    assert m["scan_budget"]["pages"] == 1
    for round_no in (0, 1, 2, 3):
        plan = search_plan(m, round_no)
        assert [p["sort"] for p in plan] == ["新发布", "新降价"]
        assert [p["pages"] for p in plan] == [1, 1]
    desk = copy.deepcopy(m)
    desk["queries"] = [{"keyword": "书桌"}]
    desk["criteria"] = {"goal": "家具"}
    assert [p["pages"] for p in search_plan(desk, 1)] == [1, 1]


def test_total_pages_rotates_and_weekly_sweep_is_bounded(setup):
    _, m = setup
    m["queries"] = [{"keyword": "相机"}, {"keyword": "镜头"}]
    m["scan_budget"]["total_pages"] = 2
    assert sum(p["pages"] for p in search_plan(m, 0)) == 2
    assert search_plan(m, 0)[0]["keyword"] != search_plan(m, 1)[0]["keyword"]
    assert sum(p["pages"] for p in search_plan(m, 6)) == 2
    m["scan_budget"]["total_pages"] = 6
    plan = search_plan(m, 6)
    assert [p["pages"] for p in plan] == [2, 2, 1, 1]
    assert plan[0]["window"] == "最新" and plan[2]["window"] == "3天内"


@pytest.mark.parametrize("field,value", [("total_pages", 0), ("triage_items", 0)])
def test_invalid_budgets_fail_closed(setup, field, value):
    _, m = setup
    m["scan_budget"][field] = value
    with pytest.raises(ValueError):
        validate_config({"schema": 2, "monitors": [m]})


@pytest.mark.parametrize("value", [None, {"sorts": [{}]}, {"sorts": ["新发布", "新发布"]}])
def test_invalid_search_policy_fails_before_capture(setup, value):
    _, m = setup
    m["search_policy"] = value
    with pytest.raises(ValueError):
        validate_config({"schema": 2, "monitors": [m]})


def test_matching_target_crossing_is_task_specific(setup):
    ledger, m = setup
    other = copy.deepcopy(m)
    other["monitor_id"] = "other"
    other["criteria"]["rules"][0]["ceiling"] = 3000
    ledger.register(other, criteria_id(other))
    item = {"id": "123", "title": "相机", "price": 4010}
    decide(ledger, m, item)
    decide(ledger, other, item)
    assert ledger.observe("camera", {**item, "price": 3990}, "相机") == "price_changed"
    assert ledger.item("other", "123")["pending"] is None


def test_fields_and_negative_pattern_avoid_incidental_mentions(setup):
    _, m = setup
    m["criteria"]["hard_exclusions"] = [{"pattern": r"相机\s*v\s*2", "fields": ["title"], "exclude_pattern": "不含", "reason": "不选该版本"}]
    assert triage({"title": "相机 V 2", "price": 1000}, m)[0] == "rejected"
    assert triage({"title": "镜头", "description": "曾搭配相机V2", "price": 1000}, m)[0] == "needs_analysis"
    assert triage({"title": "不含相机V2，出售镜头", "price": 1000}, m)[0] == "needs_analysis"


def test_placeholder_deferred_until_valid_quote_without_losing_history(setup):
    ledger, m = setup
    m["criteria"]["defer_below_price"] = 100
    ledger.register(m, criteria_id(m))
    ledger.observe("camera", {"id": "123", "title": "相机", "price": 1}, "相机")
    assert rule_triage(ledger, m, ledger.item("camera", "123")) == "deferred"
    assert ledger.pending("camera") == []
    assert ledger.status("camera")["awaiting_price"] == 1
    assert ledger.observe("camera", {"id": "123", "price": 2500}, "新降价") == "new"
    assert ledger.pending("camera")[0]["id"] == "123"
    assert ledger.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2


def test_bounded_group_selection_with_large_history(setup):
    ledger, m = setup
    for i in range(300):
        ledger.observe("camera", {"id": str(1000+i), "title": "相机", "price": 1000}, "相机")
    ledger.observe("camera", {"id": "2000", "title": "镜头", "price": 1500}, "镜头")
    ledger.observe("camera", {"id": "3000", "title": "新类别", "price": 500}, "泛词")
    for row in ledger.triage_rows("camera", 500):
        rule_triage(ledger, m, row)
    statements = []
    ledger.db.set_trace_callback(statements.append)
    rows = ledger.pending("camera", 3)
    assert {r["id"] for r in rows} >= {"2000", "3000"}
    assert len(rows) == 3
    selects = [s for s in statements if s.startswith("SELECT i.*,mi.*")]
    assert selects and all("LIMIT" in s for s in selects)


def test_triage_cursor_eventually_reaches_later_items(setup):
    ledger, _ = setup
    for i in range(5):
        ledger.observe("camera", {"id": str(100+i), "title": "相机", "price": 1000}, "相机")
    first = {r["id"] for r in ledger.triage_rows("camera", 2)}
    second = {r["id"] for r in ledger.triage_rows("camera", 2)}
    assert len(first) == len(second) == 2 and first.isdisjoint(second)


def test_watch_price_drop_reserve_sends_once_without_ai(setup, monkeypatch):
    ledger, m = setup
    m["watch_items"] = ["123"]
    ledger.register(m, criteria_id(m))
    decide(ledger, m, {"id": "123", "title": "相机", "price": 4010})
    ledger.activate("camera")
    ledger.observe("camera", {"id": "123", "price": 3990}, "favorites", notify_changes=True)
    ledger.observe("camera", {"id": "123", "price": 3990}, "相机", notify_changes=True)
    ledger.observe("camera", {"id": "123", "description": "新补充资料，价格保持"}, "watch_detail", notify_changes=True)
    assert ledger.status("camera")["outbox"] == {"pending": 1}
    monkeypatch.setattr("src.cubi_monitor.notifications.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert flush(ledger, "camera", "unused")["sent"] == 1
    assert flush(ledger, "camera", "unused")["sent"] == 0
    assert ledger.db.execute("SELECT count(*) FROM assessments").fetchone()[0] == 1
    body = json.loads(ledger.db.execute("SELECT body FROM outbox").fetchone()[0])
    assert "关注商品降价" in message(body) and "4010" in message(body)


def test_watch_prepare_stop_and_stale_changes_cannot_send(setup):
    ledger, m = setup
    m["watch_items"] = ["123"]
    ledger.register(m, criteria_id(m))
    decide(ledger, m, {"id": "123", "title": "相机", "price": 4500})
    ledger.activate("camera")
    ledger.observe("camera", {"id": "123", "price": 4000}, "favorites")
    assert ledger.status("camera")["outbox"] == {}
    ledger.observe("camera", {"id": "123", "price": 3500}, "favorites", notify_changes=True)
    ledger.observe("camera", {"id": "123", "price": 4600}, "favorites", notify_changes=True)
    assert flush(ledger, "camera", "unused")["superseded"] == 1
    ledger.db.execute("UPDATE monitors SET stopped=1 WHERE monitor_id='camera'")
    ledger.db.commit()
    ledger.observe("camera", {"id": "123", "price": 3000}, "favorites", notify_changes=True)
    assert ledger.status("camera")["outbox"] == {"superseded": 1}


def test_detail_revert_to_assessed_snapshot_skips_ai(setup, monkeypatch):
    ledger, m = setup
    decide(ledger, m, {"id": "123", "title": "相机", "price": 3000, "description": "真实详情"})
    calls = []
    class Browser:
        def __init__(self, *a): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def search(self, keyword, pages, sort, window):
            calls.append((pages, sort))
            return [{"id": "123", "title": "相机", "price": 2500}], {"pages": 1}
        async def detail(self, item_id):
            return {"id": item_id, "title": "相机", "price": 3000, "description": "真实详情"}
    class Analyzer:
        async def analyze(self, request): pytest.fail("unchanged detail must not call AI")
    monkeypatch.setattr("src.cubi_monitor.cli.Browser", Browser)
    result = asyncio.run(scan(ledger, m, {"reader_file": "unused", "cdp": "unused"}, prepare=True, analyzer=Analyzer()))
    assert result["status"] == "ok" and result["analyses"] == 0
    assert calls == [(1, "新发布"), (1, "新降价")]
    assert ledger.db.execute("SELECT count(*) FROM assessments").fetchone()[0] == 1
