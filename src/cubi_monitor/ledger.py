import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager

STABLE = ("title", "description", "seller_id", "images", "thumbnail", "availability")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def material_digest(snapshot):
    return digest({k: snapshot[k] for k in (*STABLE, "price") if k in snapshot})


def validate_item(item):
    if not str(item.get("id", "")).isdigit():
        raise ValueError("invalid_item_id")
    if "price" in item and item["price"] is not None:
        price = float(item["price"])
        if not math.isfinite(price) or price < 0:
            raise ValueError("invalid_price")
        item = {**item, "price": price}
    return {k: item[k] for k in ("id", "price", "published_at", *STABLE) if k in item}


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("unsupported_schema")
        if version == 0 and self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            self.db.close()
            raise ValueError("unknown_existing_database")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, first_seen REAL NOT NULL,
            last_seen REAL NOT NULL, pending TEXT, assessed_snapshot TEXT,
            criteria TEXT, decision TEXT, reason TEXT, quote REAL);
          CREATE TABLE IF NOT EXISTS observations (
            seq INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id),
            at REAL NOT NULL, source TEXT NOT NULL, price REAL);
          CREATE TABLE IF NOT EXISTS decisions (
            seq INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id),
            at REAL NOT NULL, fingerprint TEXT NOT NULL, criteria TEXT NOT NULL,
            decision TEXT NOT NULL, reason TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox (
            event TEXT PRIMARY KEY, item_id TEXT REFERENCES items(id), body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','uncertain')),
            attempts INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, started REAL NOT NULL, completed REAL,
            status TEXT NOT NULL, coverage TEXT NOT NULL DEFAULT '[]');
          PRAGMA user_version=1;
        """)

    def close(self):
        self.db.close()

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def activate(self, now=None):
        now = time.time() if now is None else now
        if self.meta("activated_at") is not None:
            raise ValueError("trial_already_activated")
        with self.db:
            self.db.executemany("INSERT INTO meta VALUES (?,?)", [("activated_at", str(now)), ("expires_at", str(now + 14 * 86400))])

    def active(self, now=None):
        now = time.time() if now is None else now
        expiry = self.meta("expires_at")
        return self.meta("stopped") != "1" and expiry is not None and now < float(expiry)

    def observe(self, item, source, criteria, now=None):
        now = time.time() if now is None else now
        item = validate_item(item)
        item_id = str(item["id"])
        old = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        snapshot = json.loads(old["snapshot"]) if old else {}
        # Partial list observations must never erase a previously read description.
        snapshot.update(item)
        pending = old["pending"] if old else "new"
        if old and old["assessed_snapshot"]:
            assessed = json.loads(old["assessed_snapshot"])
            if old["criteria"] != criteria:
                pending = "criteria_changed"
            elif any(k in item and item[k] != assessed.get(k) for k in STABLE):
                pending = "content_changed"
            elif item.get("price") is not None and assessed.get("price"):
                if abs(item["price"] / assessed["price"] - 1) >= 0.05 - 1e-9:
                    pending = "price_changed"
        with self.db:
            self.db.execute("""INSERT INTO items(id,snapshot,first_seen,last_seen,pending) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot,last_seen=excluded.last_seen,pending=excluded.pending""",
                (item_id, json.dumps(snapshot, ensure_ascii=False), now, now, pending))
            self.db.execute("INSERT INTO observations(item_id,at,source,price) VALUES (?,?,?,?)", (item_id, now, source, item.get("price")))
        return pending

    def requeue_criteria(self, criteria):
        with self.db:
            self.db.execute("UPDATE items SET pending='criteria_changed' WHERE criteria IS NOT NULL AND criteria!=?", (criteria,))

    def assess(self, item_id, decision, reason, criteria, notify=False, now=None):
        if decision not in ("rejected", "reserve", "candidate", "watch") or not reason.strip():
            raise ValueError("invalid_decision")
        row = self.db.execute("SELECT * FROM items WHERE id=?", (str(item_id),)).fetchone()
        if row is None:
            raise ValueError("unknown_item")
        now = time.time() if now is None else now
        snapshot = json.loads(row["snapshot"])
        fingerprint = material_digest(snapshot)
        event = digest([str(item_id), fingerprint, criteria, decision])
        with self.db:
            self.db.execute("INSERT INTO decisions(item_id,at,fingerprint,criteria,decision,reason) VALUES (?,?,?,?,?,?)", (str(item_id), now, fingerprint, criteria, decision, reason))
            self.db.execute("UPDATE items SET assessed_snapshot=snapshot,criteria=?,pending=NULL,decision=?,reason=? WHERE id=?", (criteria, decision, reason, str(item_id)))
            if notify:
                body = {"id": str(item_id), "fingerprint": fingerprint, "criteria": criteria, "title": snapshot.get("title", ""), "price": snapshot.get("price"), "quote": row["quote"], "reason": reason, "url": f"https://www.goofish.com/item?id={item_id}"}
                self.db.execute("INSERT OR IGNORE INTO outbox(event,item_id,body) VALUES (?,?,?)", (event, str(item_id), json.dumps(body, ensure_ascii=False)))

    def pending(self, limit=3):
        return [dict(r) for r in self.db.execute("SELECT * FROM items WHERE pending IS NOT NULL ORDER BY first_seen,id LIMIT ?", (limit,))]

    def seed(self, seeds, criteria):
        for seed in seeds:
            if self.db.execute("SELECT 1 FROM items WHERE id=?", (str(seed["id"]),)).fetchone():
                continue
            self.observe(seed, "seed", criteria)
            self.assess(seed["id"], seed["decision"], seed["reason"], criteria)
            if seed.get("quote") is not None:
                with self.db:
                    self.db.execute("UPDATE items SET quote=? WHERE id=?", (seed["quote"], str(seed["id"])))

    def outbox(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY rowid LIMIT 3")]

    def claim_send(self, event):
        # Commit before the external effect: an interrupted send is uncertain, not auto-retried.
        with self.db:
            self.db.execute("UPDATE outbox SET status='uncertain',attempts=attempts+1 WHERE event=? AND status='pending'", (event,))

    def finish_send(self, event, confirmed):
        with self.db:
            self.db.execute("UPDATE outbox SET status=? WHERE event=?", ("sent" if confirmed else "uncertain", event))

    def status(self):
        return {"items": self.db.execute("SELECT count(*) FROM items").fetchone()[0], "pending": self.db.execute("SELECT count(*) FROM items WHERE pending IS NOT NULL").fetchone()[0], "outbox": {r[0]: r[1] for r in self.db.execute("SELECT status,count(*) FROM outbox GROUP BY status")}, "activated_at": self.meta("activated_at"), "expires_at": self.meta("expires_at"), "active": self.active()}
