"""Representative schema1 inputs; no private runtime data."""
import json
import sqlite3
from pathlib import Path
import pytest
from src.cubi_monitor.ledger import Ledger,LEGACY_ID


def legacy(path):
    db=sqlite3.connect(path)
    db.executescript('''
      CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE items(id TEXT PRIMARY KEY,snapshot TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,pending TEXT,assessed_snapshot TEXT,criteria TEXT,decision TEXT,reason TEXT,quote REAL);
      CREATE TABLE observations(seq INTEGER PRIMARY KEY,item_id TEXT REFERENCES items(id),at REAL,source TEXT,price REAL);
      CREATE TABLE decisions(seq INTEGER PRIMARY KEY,item_id TEXT REFERENCES items(id),at REAL,fingerprint TEXT,criteria TEXT,decision TEXT,reason TEXT);
      CREATE TABLE outbox(event TEXT PRIMARY KEY,item_id TEXT REFERENCES items(id),body TEXT,status TEXT,attempts INTEGER);
      CREATE TABLE runs(id INTEGER PRIMARY KEY,started REAL,completed REAL,status TEXT,coverage TEXT);
      PRAGMA user_version=1;
    ''')
    snapshot=json.dumps({"id":"123","title":"合成旧商品","price":6000,"description":"48GB"},ensure_ascii=False)
    db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)",("123",snapshot,1,2,None,snapshot,"old-criteria","watch","待核实",5500))
    db.execute("INSERT INTO observations VALUES(1,'123',2,'q1',6000)")
    db.execute("INSERT INTO observations VALUES(2,'123',3,'q2',5800)")
    db.execute("INSERT INTO decisions VALUES(1,'123',2,'old-fingerprint','old-criteria','watch','待核实')")
    for i,status in enumerate(("sent","uncertain","pending")):
        db.execute("INSERT INTO outbox VALUES(?,?,?,?,?)",(f"event-{i}","123",'{"frozen":"payload"}',status,i))
    db.execute("INSERT INTO runs VALUES(1,1,2,'ok','[]')")
    db.executemany("INSERT INTO meta VALUES(?,?)",[("activated_at","1"),("expires_at","1209601")])
    db.commit()
    return db,snapshot


def test_real_migration_preserves_identity_history_quotes_receipts_and_expiry(tmp_path):
    path=tmp_path/"old.sqlite3"
    db,snapshot=legacy(path)
    db.close()
    ledger=Ledger(path)
    assert ledger.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert Path(ledger.backup_path).is_file() and Path(ledger.backup_path).stat().st_mode & 0o777 == 0o600
    old=sqlite3.connect(ledger.backup_path)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1
    old.close()
    item=ledger.item(LEGACY_ID,"123")
    assert item["snapshot"] == snapshot and item["first_seen"] == 1 and item["last_seen"] == 2
    assert item["quote"] == 5500 and item["watch"] == 1 and item["decision"] == "reserve"
    result=ledger.db.execute("SELECT item_revision,result FROM assessments").fetchone()
    assert result[0] == "old-fingerprint" and json.loads(result[1])["decision"] == "watch"
    assert ledger.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    assert [(r["event"],r["status"],r["body"],r["attempts"]) for r in ledger.db.execute("SELECT * FROM outbox ORDER BY event")] == [(f"event-{i}",s,'{"frozen":"payload"}',i) for i,s in enumerate(("sent","uncertain","pending"))]
    assert ledger.monitor(LEGACY_ID)["expires_at"] == 1209601 and not ledger.active(LEGACY_ID,2)
    assert not ledger.db.execute("PRAGMA foreign_key_check").fetchall()
    ledger.close()
    reopened=Ledger(path)
    assert reopened.backup_path is None
    assert reopened.db.execute("SELECT count(*) FROM assessments").fetchone()[0] == 1
    reopened.close()


def test_migration_rollback_after_backup(tmp_path,monkeypatch):
    path=tmp_path/"old.sqlite3"
    db,_=legacy(path)
    db.close()
    before=path.read_bytes()
    original=Ledger._create
    def fail(instance):
        original(instance)
        raise RuntimeError("injected")
    monkeypatch.setattr(Ledger,"_create",fail)
    with pytest.raises(RuntimeError): Ledger(path)
    assert path.read_bytes() == before
    db=sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 3
    db.close()


@pytest.mark.parametrize("fault",["orphan","identity","unexpected_table"])
def test_invalid_legacy_fails_before_mutation(tmp_path,fault):
    path=tmp_path/"old.sqlite3"
    db,_=legacy(path)
    if fault == "orphan": db.execute("INSERT INTO observations VALUES(3,'999',4,'bad',1)")
    if fault == "identity": db.execute("UPDATE items SET snapshot='{"+'"id":"456"'+'}'+"'")
    if fault == "unexpected_table": db.execute("CREATE TABLE unknown(id INTEGER)")
    db.commit();db.close()
    before=path.read_bytes()
    with pytest.raises(ValueError): Ledger(path)
    assert path.read_bytes() == before and list(tmp_path.glob('*.bak')) == []
