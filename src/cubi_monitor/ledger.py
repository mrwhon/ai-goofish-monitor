"""SQLite authority: shared facts, task-owned decisions, immutable history/outbox."""
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

STABLE = ("title", "description", "seller_id", "images", "availability")
LEGACY_ID = "legacy-purchase-review"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def material_digest(snapshot):
    return digest({k: snapshot[k] for k in (*STABLE, "price") if k in snapshot})


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def validate_item(item):
    if not str(item.get("id", "")).isdigit() or item.get("platform", "goofish") != "goofish":
        raise ValueError("invalid_item_id_or_platform")
    result = {k: item[k] for k in ("id", "platform", "price", "published_at", "thumbnail", *STABLE) if k in item}
    result["id"] = str(item["id"])
    result["platform"] = "goofish"
    if result.get("price") is not None:
        price = float(result["price"])
        if not math.isfinite(price) or price < 0:
            raise ValueError("invalid_price")
        result["price"] = price
    else:
        result.pop("price", None)  # Unknown list price cannot erase a known detail price.
    return result


DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE monitors(monitor_id TEXT PRIMARY KEY,config TEXT NOT NULL,criteria_hash TEXT NOT NULL,
 evaluator_version TEXT NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),activated_at REAL,
 expires_at REAL,stopped INTEGER NOT NULL DEFAULT 0 CHECK(stopped IN(0,1)),blocked TEXT,
 CHECK((activated_at IS NULL AND expires_at IS NULL) OR expires_at>activated_at));
CREATE TABLE items(item_key TEXT PRIMARY KEY,platform TEXT NOT NULL,id TEXT NOT NULL,snapshot TEXT NOT NULL,
 revision TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,UNIQUE(platform,id));
CREATE TABLE observations(seq INTEGER PRIMARY KEY,item_key TEXT NOT NULL REFERENCES items(item_key),
 at REAL NOT NULL,source TEXT NOT NULL,price REAL,snapshot TEXT);
CREATE TABLE monitor_items(monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
 item_key TEXT NOT NULL REFERENCES items(item_key),watch INTEGER NOT NULL DEFAULT 0 CHECK(watch IN(0,1)),
 quote REAL,pending TEXT,assessed_snapshot TEXT,assessed_revision TEXT,criteria_hash TEXT,evaluator_version TEXT,
 decision TEXT CHECK(decision IN('rejected','reserve','candidate')),reason TEXT,priority REAL NOT NULL DEFAULT 0,
 attempted_at REAL,error TEXT,PRIMARY KEY(monitor_id,item_key));
CREATE TABLE assessments(seq INTEGER PRIMARY KEY,monitor_id TEXT NOT NULL,item_key TEXT NOT NULL,
 at REAL NOT NULL,item_revision TEXT NOT NULL,criteria_hash TEXT NOT NULL,evaluator_version TEXT NOT NULL,
 snapshot TEXT,result TEXT NOT NULL,UNIQUE(seq,monitor_id,item_key),FOREIGN KEY(monitor_id,item_key) REFERENCES monitor_items(monitor_id,item_key));
CREATE INDEX assessment_lookup ON assessments(monitor_id,item_key,item_revision,criteria_hash,evaluator_version);
CREATE TABLE outbox(event TEXT PRIMARY KEY,monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
 item_key TEXT,assessment_id INTEGER,kind TEXT NOT NULL DEFAULT 'candidate',body TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN('pending','sent','uncertain','superseded')),
 attempts INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,
 FOREIGN KEY(monitor_id,item_key) REFERENCES monitor_items(monitor_id,item_key),
 FOREIGN KEY(assessment_id,monitor_id,item_key) REFERENCES assessments(seq,monitor_id,item_key));
CREATE TABLE runs(id INTEGER PRIMARY KEY,monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
 mode TEXT NOT NULL CHECK(mode IN('prepare','live')),started REAL NOT NULL,completed REAL,status TEXT NOT NULL,
 coverage TEXT NOT NULL DEFAULT '[]',details INTEGER NOT NULL DEFAULT 0,analyses INTEGER NOT NULL DEFAULT 0,error TEXT);
"""


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self.backup_path = None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("unsupported_schema")
            if version == 0:
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("unknown_existing_database")
                with self.db:
                    self.db.execute("BEGIN IMMEDIATE")
                    self._create()
            elif version == 1:
                self._migrate()
            if self.db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("foreign_key_violation")
        except Exception:
            self.db.close()
            raise

    def _create(self):
        for statement in DDL.split(";"):
            if statement.strip():
                self.db.execute(statement)
        self.db.execute("PRAGMA user_version=2")

    def _migrate(self):
        names = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names != {"meta", "items", "observations", "decisions", "outbox", "runs"}:
            raise ValueError("unsupported_legacy_shape")
        if self.db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("legacy_orphans")
        old = {t: [dict(r) for r in self.db.execute(f'SELECT * FROM "{t}"')] for t in names}
        # Validate every input before altering its schema. Never infer task membership.
        for row in old["items"]:
            snap = validate_item(json.loads(row["snapshot"]))
            if snap["id"] != str(row["id"]):
                raise ValueError("legacy_identity_mismatch")
        self.backup_path = self.path + f".schema1-{time.time_ns()}.bak"
        fd = os.open(self.backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        backup = sqlite3.connect(self.backup_path)
        try:
            self.db.backup(backup)
        finally:
            backup.close()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for table in names:
                self.db.execute(f'ALTER TABLE "{table}" RENAME TO "legacy_{table}"')
            self._create()
            self.db.executemany("INSERT INTO meta VALUES(?,?)", [(r["key"], r["value"]) for r in old["meta"]])
            meta = {r["key"]: r["value"] for r in old["meta"]}
            self.db.execute("INSERT INTO monitors VALUES(?,?,?,?,?,?,?,?,?)", (LEGACY_ID, encoded({"monitor_id": LEGACY_ID, "enabled": False}), "legacy", "legacy-v1", 0, meta.get("activated_at"), meta.get("expires_at"), int(meta.get("stopped", 0)), meta.get("blocked")))
            for row in old["items"]:
                key = "goofish:" + str(row["id"])
                self.db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?)", (key, "goofish", str(row["id"]), row["snapshot"], material_digest(json.loads(row["snapshot"])), row["first_seen"], row["last_seen"]))
                decision = "reserve" if row["decision"] == "watch" else row["decision"]
                self.db.execute("INSERT INTO monitor_items(monitor_id,item_key,watch,quote,pending,assessed_snapshot,assessed_revision,criteria_hash,evaluator_version,decision,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (LEGACY_ID,key,int(row["decision"] == "watch"),row["quote"],row["pending"],row["assessed_snapshot"], material_digest(json.loads(row["assessed_snapshot"])) if row["assessed_snapshot"] else None,row["criteria"],"legacy-v1",decision,row["reason"]))
            for row in old["observations"]:
                self.db.execute("INSERT INTO observations(seq,item_key,at,source,price) VALUES(?,?,?,?,?)", (row["seq"], "goofish:" + str(row["item_id"]), row["at"], row["source"], row["price"]))
            for row in old["decisions"]:
                self.db.execute("INSERT INTO assessments(seq,monitor_id,item_key,at,item_revision,criteria_hash,evaluator_version,result) VALUES(?,?,?,?,?,?,?,?)", (row["seq"],LEGACY_ID,"goofish:"+str(row["item_id"]),row["at"],row["fingerprint"],row["criteria"],"legacy-v1",encoded({"decision": row["decision"], "reason": row["reason"], "analysis_source": "legacy"})))
            for row in old["outbox"]:
                self.db.execute("INSERT INTO outbox(event,monitor_id,item_key,body,status,attempts,created_at) VALUES(?,?,?,?,?,?,?)", (row["event"],LEGACY_ID,"goofish:"+str(row["item_id"]) if row["item_id"] else None,row["body"],row["status"],row["attempts"],time.time()))
            for row in old["runs"]:
                self.db.execute("INSERT INTO runs(id,monitor_id,mode,started,completed,status,coverage) VALUES(?,?,?,?,?,?,?)", (row["id"],LEGACY_ID,"prepare",row["started"],row["completed"],row["status"],row["coverage"]))
            for old_table, new_table in (("items","items"),("observations","observations"),("decisions","assessments"),("outbox","outbox"),("runs","runs")):
                if self.db.execute(f"SELECT count(*) FROM {new_table}").fetchone()[0] != len(old[old_table]):
                    raise ValueError("migration_count_mismatch")
            for table in ("outbox", "decisions", "observations", "runs", "items", "meta"):
                self.db.execute(f'DROP TABLE "legacy_{table}"')
            if self.db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("migration_foreign_key_violation")

    def close(self):
        self.db.close()

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,str(value)))
        row = self.db.execute("SELECT value FROM meta WHERE key=?",(key,)).fetchone()
        return row[0] if row else None

    def register(self, monitor, criteria_hash):
        mid = monitor["monitor_id"]
        old = self.monitor(mid)
        with self.db:
            self.db.execute("INSERT INTO monitors(monitor_id,config,criteria_hash,evaluator_version,enabled) VALUES(?,?,?,?,?) ON CONFLICT(monitor_id) DO UPDATE SET config=excluded.config,criteria_hash=excluded.criteria_hash,evaluator_version=excluded.evaluator_version,enabled=excluded.enabled", (mid,encoded(monitor),criteria_hash,monitor["evaluator_version"],int(monitor.get("enabled",True))))
            if old and (old["criteria_hash"] != criteria_hash or old["evaluator_version"] != monitor["evaluator_version"]):
                self.db.execute("UPDATE monitor_items SET pending='criteria_changed' WHERE monitor_id=?", (mid,))
                self.db.execute("UPDATE outbox SET status='superseded' WHERE monitor_id=? AND kind='candidate' AND status='pending'", (mid,))
        for baseline in monitor.get("baseline_refs", []):
            item = baseline.get("item", {})
            if not item:
                continue
            key = "goofish:"+str(item["id"])
            if not self.item(mid,item["id"]):
                existing = self.db.execute("SELECT 1 FROM items WHERE item_key=?", (key,)).fetchone()
                if existing:
                    with self.db:
                        self.db.execute("INSERT INTO monitor_items(monitor_id,item_key,pending) VALUES(?,?,'new')", (mid,key))
                else:
                    self.observe(mid,item,"confirmed_baseline")
            with self.db:
                self.db.execute("UPDATE monitor_items SET quote=COALESCE(?,quote),watch=? WHERE monitor_id=? AND item_key=?", (baseline.get("quote"),int(str(item["id"]) in monitor.get("watch_items", [])),mid,key))
        for item_id in monitor.get("watch_items", []):
            if not self.item(mid,item_id):
                if self.db.execute("SELECT 1 FROM items WHERE item_key=?",("goofish:"+str(item_id),)).fetchone():
                    with self.db:
                        self.db.execute("INSERT INTO monitor_items(monitor_id,item_key,pending) VALUES(?,?,'new')", (mid,"goofish:"+str(item_id)))
                else:
                    self.observe(mid,{"id":str(item_id)},"watch_registration")
            with self.db:
                self.db.execute("UPDATE monitor_items SET watch=1 WHERE monitor_id=? AND item_key=?", (mid,"goofish:"+str(item_id)))

    def monitor(self, mid):
        row = self.db.execute("SELECT * FROM monitors WHERE monitor_id=?",(mid,)).fetchone()
        return dict(row) if row else None

    def activate(self, mid, now=None):
        now = time.time() if now is None else now
        m = self.monitor(mid)
        if not m or not m["enabled"]:
            raise ValueError("monitor_disabled_or_missing")
        if m["activated_at"] is not None:
            raise ValueError("trial_already_activated")
        days = json.loads(m["config"]).get("duration_days",14)
        with self.db:
            self.db.execute("UPDATE monitors SET activated_at=?,expires_at=? WHERE monitor_id=?", (now,now+days*86400,mid))

    def resume(self, mid, now=None):
        now = time.time() if now is None else now
        m = self.monitor(mid)
        if not m or not m["enabled"] or m["activated_at"] is None or not m["activated_at"] <= now < m["expires_at"]:
            raise ValueError("trial_not_resumable")
        with self.db:
            self.db.execute("UPDATE monitors SET stopped=0 WHERE monitor_id=?", (mid,))

    def active(self, mid, now=None):
        now = time.time() if now is None else now
        m = self.monitor(mid)
        return bool(m and m["enabled"] and not m["stopped"] and m["activated_at"] is not None and m["activated_at"] <= now < m["expires_at"])

    def observe(self, mid, item, source, now=None):
        now = time.time() if now is None else now
        item = validate_item(item)
        key = item["platform"]+":"+item["id"]
        old = self.db.execute("SELECT * FROM items WHERE item_key=?",(key,)).fetchone()
        snapshot = json.loads(old["snapshot"]) if old else {}
        # A list heading often truncates the actual description-derived detail title.
        if old and source not in ("detail","watch_detail") and "description" in snapshot:
            item.pop("title", None)
        snapshot.update(item)
        revision = material_digest(snapshot)
        with self.db:
            self.db.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?) ON CONFLICT(item_key) DO UPDATE SET snapshot=excluded.snapshot,revision=excluded.revision,last_seen=excluded.last_seen", (key,item["platform"],item["id"],encoded(snapshot),revision,now,now))
            self.db.execute("INSERT INTO observations(item_key,at,source,price,snapshot) VALUES(?,?,?,?,?)", (key,now,source,item.get("price"),encoded(item)))
            self.db.execute("INSERT OR IGNORE INTO monitor_items(monitor_id,item_key,pending) VALUES(?,?,'new')",(mid,key))
            # Shared fact change independently invalidates each existing task relation.
            relations = self.db.execute("SELECT mi.*,m.config,m.criteria_hash AS wanted_criteria,m.evaluator_version AS wanted_evaluator FROM monitor_items mi JOIN monitors m USING(monitor_id) WHERE item_key=?",(key,)).fetchall()
            for relation in relations:
                pending = relation["pending"]
                assessed = json.loads(relation["assessed_snapshot"]) if relation["assessed_snapshot"] else None
                if assessed:
                    pending = None
                    criteria = json.loads(relation["config"]).get("criteria", {})
                    if relation["criteria_hash"] != relation["wanted_criteria"] or relation["evaluator_version"] != relation["wanted_evaluator"]:
                        pending = "criteria_changed"
                    elif any(snapshot.get(k) != assessed.get(k) for k in STABLE):
                        pending = "content_changed"
                    elif snapshot.get("price") is not None and assessed.get("price") is not None:
                        previous = assessed["price"]
                        threshold = criteria.get("price_change_fraction",.05)
                        ceiling = criteria.get("max_price")
                        crossing = ceiling is not None and (previous > ceiling) != (snapshot["price"] > ceiling)
                        if crossing or (previous > 0 and abs(snapshot["price"]/previous-1) >= threshold-1e-9) or (previous == 0 and snapshot["price"] != 0):
                            pending = "price_changed"
                self.db.execute("UPDATE monitor_items SET pending=? WHERE monitor_id=? AND item_key=?",(pending,relation["monitor_id"],key))
        return self.item(mid,item["id"])["pending"]

    def item(self, mid, item_id):
        row = self.db.execute("SELECT i.*,mi.* FROM items i JOIN monitor_items mi USING(item_key) WHERE mi.monitor_id=? AND i.platform='goofish' AND i.id=?",(mid,str(item_id))).fetchone()
        return dict(row) if row else None

    def assess(self, mid, item_id, result, criteria_hash, evaluator_version, expected_revision=None, notify=False, now=None):
        now = time.time() if now is None else now
        data = asdict(result) if hasattr(result,"__dataclass_fields__") else result
        if data.get("decision") not in ("rejected","reserve","candidate") or not data.get("reason","").strip():
            raise ValueError("invalid_decision")
        row, monitor = self.item(mid,item_id), self.monitor(mid)
        if row is None or (expected_revision is not None and row["revision"] != expected_revision) or monitor["criteria_hash"] != criteria_hash or monitor["evaluator_version"] != evaluator_version:
            raise ValueError("stale_assessment")
        with self.db:
            existing = self.db.execute("SELECT seq FROM assessments WHERE monitor_id=? AND item_key=? AND item_revision=? AND criteria_hash=? AND evaluator_version=? AND result=? ORDER BY seq DESC LIMIT 1",(mid,row["item_key"],row["revision"],criteria_hash,evaluator_version,encoded(data))).fetchone()
            seq = existing[0] if existing else self.db.execute("INSERT INTO assessments(monitor_id,item_key,at,item_revision,criteria_hash,evaluator_version,snapshot,result) VALUES(?,?,?,?,?,?,?,?)",(mid,row["item_key"],now,row["revision"],criteria_hash,evaluator_version,row["snapshot"],encoded(data))).lastrowid
            self.db.execute("UPDATE monitor_items SET pending=NULL,assessed_snapshot=?,assessed_revision=?,criteria_hash=?,evaluator_version=?,decision=?,reason=?,error=NULL WHERE monitor_id=? AND item_key=?",(row["snapshot"],row["revision"],criteria_hash,evaluator_version,data["decision"],data["reason"],mid,row["item_key"]))
            if notify:
                snapshot = json.loads(row["snapshot"])
                body = {"monitor_id":mid,"id":str(item_id),"fingerprint":row["revision"],"criteria":criteria_hash,"evaluator_version":evaluator_version,"title":snapshot.get("title", ""),"price":snapshot.get("price"),"quote":row["quote"],"url":f"https://www.goofish.com/item?id={item_id}",**data}
                self.enqueue(mid,"candidate",body,digest([mid,row["item_key"],row["revision"],criteria_hash,evaluator_version]),row["item_key"],seq,now,commit=False)
        return seq

    def enqueue(self, mid, kind, body, event, item_key=None, assessment_id=None, now=None, commit=True):
        self.db.execute("INSERT OR IGNORE INTO outbox(event,monitor_id,item_key,assessment_id,kind,body,created_at) VALUES(?,?,?,?,?,?,?)", (event,mid,item_key,assessment_id,kind,encoded(body),time.time() if now is None else now))
        if commit:
            self.db.commit()

    def mark_error(self, mid, item_id, error, now=None):
        with self.db:
            self.db.execute("UPDATE monitor_items SET attempted_at=?,error=? WHERE monitor_id=? AND item_key=?", (time.time() if now is None else now,error[:160],mid,"goofish:"+str(item_id)))

    def pending(self, mid, limit=3):
        rows = [dict(r) for r in self.db.execute("SELECT i.*,mi.* FROM items i JOIN monitor_items mi USING(item_key) WHERE mi.monitor_id=? AND mi.pending IS NOT NULL ORDER BY mi.attempted_at IS NOT NULL,mi.attempted_at,mi.priority DESC,i.first_seen DESC,i.id",(mid,))]
        changed = [r for r in rows if r["pending"] != "new"]
        unknown = [r for r in rows if r["pending"] == "new" and r["priority"] <= 0]
        plausible = [r for r in rows if r["pending"] == "new" and r["priority"] > 0]
        chosen = []
        for lane in (changed,plausible,unknown):
            if lane and len(chosen) < limit:
                chosen.append(lane[0])
        keys = {r["item_key"] for r in chosen}
        return (chosen + [r for r in rows if r["item_key"] not in keys])[:limit]

    def start_run(self, mid, mode, now=None):
        with self.db:
            # The process lock proves old running rows have no live writer.
            self.db.execute("UPDATE runs SET status='interrupted',completed=? WHERE monitor_id=? AND status='running'",(time.time() if now is None else now,mid))
            return self.db.execute("INSERT INTO runs(monitor_id,mode,started,status) VALUES(?,?,?,'running')",(mid,mode,time.time() if now is None else now)).lastrowid

    def run_count(self, mid, mode="live"):
        return self.db.execute("SELECT count(*) FROM runs WHERE monitor_id=? AND mode=? AND status='ok'",(mid,mode)).fetchone()[0]

    def health(self, mid, now=None):
        now = time.time() if now is None else now
        m = self.monitor(mid)
        if not self.active(mid,now):
            return {"status":"inactive","monitor_id":mid}
        last = self.db.execute("SELECT max(completed) FROM runs WHERE monitor_id=? AND mode='live' AND status='ok'",(mid,)).fetchone()[0]
        reason = "blocked" if m["blocked"] else "missed_run" if now-(last or m["activated_at"])>36*3600 else None
        return {"status":"degraded" if reason else "ok","reason":reason,"last_success_at":last,"monitor_id":mid}

    def status(self, mid):
        m = self.monitor(mid)
        return {"monitor_id":mid,"items":self.db.execute("SELECT count(*) FROM monitor_items WHERE monitor_id=?",(mid,)).fetchone()[0],"pending":self.db.execute("SELECT count(*) FROM monitor_items WHERE monitor_id=? AND pending IS NOT NULL",(mid,)).fetchone()[0],"outbox":{r[0]:r[1] for r in self.db.execute("SELECT status,count(*) FROM outbox WHERE monitor_id=? GROUP BY status",(mid,))},"activated_at":m["activated_at"] if m else None,"expires_at":m["expires_at"] if m else None,"active":self.active(mid),"blocked":m["blocked"] if m else None}
