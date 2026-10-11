"""Independent CLI contract tests; run with --confcutdir=tests/cubi."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.cubi_monitor.browser import CaptureError, parse_search, parse_detail, price_value, Browser
from src.cubi_monitor.cli import flush, scan, message, load_profile
from src.cubi_monitor.ledger import Ledger
from src.cubi_monitor.policy import criteria_id, judge


@pytest.fixture
def ledger(tmp_path):
    instance = Ledger(tmp_path / "test.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def profile():
    return {"schema": 1, "cdp": "http://127.0.0.1:9222", "reader_file": "unused", "pages": 2, "detail_limit": 3, "timeout": 30, "queries": [{"keyword": "9950X 主机"}], "seeds": [], "criteria": {"rules": [{"pattern": "9950", "ceiling": 9550, "label": "9950整机"}]}}


def observed(ledger):
    item = {"id": "123", "title": "9950X 主机", "price": 9550, "images": ["a"], "description": "48GB 3TB"}
    ledger.observe(item, "keyword1", "v1")
    ledger.assess("123", "rejected", "priority", "v1")
    return item


def test_global_dedupe_survives_reopen_and_all_decisions(ledger):
    item = observed(ledger)
    assert ledger.observe({**item, "views": 999, "visited": "刚刚"}, "keyword2", "v1") is None
    assert ledger.pending() == []
    assert ledger.status()["items"] == 1
    ledger.observe({**item, "id": "456"}, "keyword1", "v1")
    assert ledger.status()["items"] == 2  # Same seller/spec does not identify a physical machine.
    for decision in ("watch", "reserve", "candidate", "rejected"):
        ledger.assess("456", decision, "evidence", "v1")
        assert ledger.observe({**item, "id": "456"}, "keyword3", "v1") is None
    second = Ledger(ledger.db.execute("PRAGMA database_list").fetchone()[2])
    assert second.pending() == []
    second.close()


def test_price_threshold_cumulative_and_content_criteria(ledger):
    item = observed(ledger)
    assert ledger.observe({"id": "123", "price": 9300}, "list", "v1") is None
    assert ledger.observe({"id": "123", "price": 9200}, "list", "v1") is None
    assert ledger.observe({"id": "123", "price": 9000}, "list", "v1") == "price_changed"
    assert json.loads(ledger.pending()[0]["snapshot"])["description"] == item["description"]
    ledger.assess("123", "reserve", "wait", "v1")
    assert ledger.observe({"id": "123", "description": "64GB"}, "detail", "v1") == "content_changed"
    ledger.assess("123", "reserve", "wait", "v1")
    ledger.requeue_criteria("v2")
    assert ledger.pending()[0]["pending"] == "criteria_changed"


def test_list_thumbnail_not_full_detail_images(ledger):
    observed(ledger)
    ledger.observe({"id": "123", "thumbnail": "a"}, "list", "v1")
    ledger.assess("123", "reserve", "wait", "v1")
    assert ledger.observe({"id": "123", "thumbnail": "a"}, "list", "v1") is None


def test_quotes_are_not_listing_prices_and_seed_is_idempotent(ledger):
    seeds = [{"id": "123", "price": 11388, "quote": 10500, "decision": "watch", "reason": "waiting"}]
    ledger.seed(seeds, "v1")
    ledger.observe({"id": "123", "price": 11000}, "list", "v1")
    ledger.seed(seeds, "v1")
    row = ledger.db.execute("SELECT * FROM items").fetchone()
    assert row["quote"] == 10500
    assert json.loads(row["snapshot"])["price"] == 11000
    assert row["first_seen"] <= row["last_seen"]


@pytest.mark.parametrize("price", [float('nan'), float('inf'), -1])
def test_invalid_price_fails_without_partial_insert(ledger, price):
    with pytest.raises(ValueError):
        ledger.observe({"id": "123", "price": price}, "query", "v1")
    assert ledger.status()["items"] == 0


def test_atomic_decision_and_outbox_rollback(ledger):
    observed(ledger)
    ledger.db.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'test failure'); END;")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.assess("123", "candidate", "good", "v2", notify=True)
    assert ledger.db.execute("SELECT decision FROM items").fetchone()[0] == "rejected"
    assert ledger.db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


def test_trial_immutable_boundary_and_stop(ledger):
    assert not ledger.active(1)
    ledger.activate(1)
    assert ledger.active(1 + 14 * 86400 - 1)
    assert not ledger.active(1 + 14 * 86400)
    with pytest.raises(ValueError):
        ledger.activate(2)
    ledger.meta("stopped", "1")
    assert not ledger.active(3)


@pytest.mark.parametrize("returncode,status", [(0, "sent"), (2, "uncertain")])
def test_notification_acknowledgement_and_no_reanalysis(ledger, monkeypatch, returncode, status):
    observed(ledger)
    ledger.activate()
    ledger.assess("123", "candidate", "check", "v1", notify=True)
    ledger.assess("123", "candidate", "check", "v1", notify=True)
    assert len(ledger.outbox()) == 1
    calls = []
    monkeypatch.setattr("src.cubi_monitor.cli.subprocess.run", lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=returncode))
    flush(ledger, "/fake-sender")
    assert ledger.status()["outbox"] == {status: 1}
    flush(ledger, "/fake-sender")
    assert len(calls) == 1


def test_expired_or_stale_notifications_never_sent(ledger, monkeypatch):
    observed(ledger)
    ledger.assess("123", "candidate", "check", "v1", notify=True)
    with pytest.raises(ValueError):
        flush(ledger, "/fake-sender")
    ledger.activate()
    ledger.observe({"id": "123", "price": 20000}, "list", "v1")
    monkeypatch.setattr("src.cubi_monitor.cli.subprocess.run", lambda *args, **kwargs: pytest.fail("stale send"))
    assert flush(ledger, "/fake-sender") == 0
    assert ledger.status()["outbox"] == {"uncertain": 1}


def test_notification_html_escaped(ledger):
    text = message(json.dumps({"title": "<script>", "reason": "x&y", "price": 1, "url": "https://example.com/?a=1&b=2"}))
    assert "<script>" not in text and "x&amp;y" in text


@pytest.mark.parametrize("version", [2, 99])
def test_unsupported_schema_preserves_file(tmp_path, version):
    path = tmp_path / "db"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version={version}")
    db.close()
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Ledger(path)
    assert path.read_bytes() == before


def test_foreign_database_not_adopted(tmp_path):
    path = tmp_path / "db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE foreign_data(id)")
    db.close()
    with pytest.raises(ValueError):
        Ledger(path)


@pytest.mark.parametrize("payload", [{"ret": ["FAIL_SYS_USER_VALIDATE"], "data": {}}, {"ret": ["SUCCESS"], "data": {}}, {"data": {"resultList": []}}])
def test_schema_and_risk_are_not_successful_empty(payload):
    with pytest.raises(CaptureError):
        parse_search(payload)


def test_search_and_detail_selected_fields():
    payload = {"ret": ["SUCCESS::调用成功"], "data": {"resultList": [{"data": {"item": {"main": {"exContent": {"itemId": "123", "title": "电脑", "price": [{"text": "¥1.05万"}], "picUrl": "https://example.com/a?token=xxx"}, "clickParam": {"args": {"publishTime": "1000"}}}}}}]}}
    item = parse_search(payload)[0]
    assert item["price"] == 10500 and item["published_at"] == 1
    assert "token" not in item["thumbnail"]
    assert parse_search({"ret": ["SUCCESS"], "data": {"resultList": []}}) == []
    with pytest.raises(CaptureError):
        parse_detail({"ret": ["SUCCESS"], "data": {"itemDO": {"itemId": "456"}}}, "123")
    assert price_value("定金100元") is None


def test_inactive_and_blocked_scan_never_opens_browser(ledger, profile, monkeypatch):
    monkeypatch.setattr("src.cubi_monitor.cli.Browser", lambda *args: pytest.fail("opened browser"))
    assert asyncio.run(scan(ledger, profile))["status"] == "inactive_or_expired"
    ledger.meta("blocked", "challenge")
    assert asyncio.run(scan(ledger, profile, prepare=True))["status"] == "blocked"


def test_risk_scan_closes_owned_context_and_records_failure(ledger, profile, monkeypatch):
    closed = []
    class FakeBrowser:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append(True)
        async def favorites(self, ids): raise CaptureError("visible_platform_boundary")
    monkeypatch.setattr("src.cubi_monitor.cli.Browser", FakeBrowser)
    result = asyncio.run(scan(ledger, profile, prepare=True))
    assert result["status"] == "failed" and closed == [True]
    assert ledger.meta("blocked")
    assert ledger.db.execute("SELECT status FROM runs").fetchone()[0] == "failed"


def test_criteria_hash_scope_and_unknown_is_reserve(profile):
    version = criteria_id(profile)
    profile["timeout"] = 45
    assert criteria_id(profile) == version
    profile["criteria"]["budget"] = 30000
    assert criteria_id(profile) != version
    assert judge({"title": "新平台", "price": 8000}, profile)[0] == "reserve"
    assert judge({"title": "9950", "price": 90}, profile)[0] == "reserve"


def test_profile_limits_and_remote_cdp_refused(tmp_path, profile):
    profile["pages"] = 3
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    with pytest.raises(ValueError): load_profile(path)
    with pytest.raises(ValueError): Browser("unused", "https://remote.example:9222")
