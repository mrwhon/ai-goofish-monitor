"""Outbox delivery. A pre-effect claim survives interruption as uncertain."""
import html
import json
import subprocess
import time

from .policy import triage


def message(body):
    body = json.loads(body) if isinstance(body,str) else body
    if "summary" in body:
        return html.escape(body["summary"][:3500])
    esc = lambda value: html.escape(str(value))
    quote = f"；已知议价 {esc(body['quote'])} 元（未重新确认）" if body.get("quote") is not None else ""
    gaps = "；".join(body.get("evidence_gaps", []))
    kind = {"watch_price_drop":"关注商品降价", "price_drop":"降价候选", "newly_discovered":"首次发现候选", "updated":"变化候选"}.get(body.get("change_kind"), "待核实候选")
    previous = f"；此前标价 {esc(body['previous_price'])} 元" if body.get("previous_price") is not None else ""
    return (f"闲鱼{kind} [{esc(body.get('monitor_id',''))}]\n{esc(body.get('title','')[:250])}\n"
            f"标价 {esc(body.get('price'))} 元{previous}{quote}\n{esc(body.get('reason','')[:650])}\n"
            f"比较：{esc(body.get('comparison','')[:900])}\n缺口：{esc(gaps[:500])}\n{esc(body.get('url',''))}")


def flush(ledger, mid, sender, now=None):
    now = time.time() if now is None else now
    events = ledger.db.execute("SELECT * FROM outbox WHERE monitor_id=? AND status='pending' ORDER BY created_at,event LIMIT 5",(mid,)).fetchall()
    counts = {"sent":0,"uncertain":0,"retry":0,"superseded":0}
    for event in events:
        monitor = ledger.monitor(mid)
        if monitor["stopped"] or not monitor["enabled"]:
            break
        if not ledger.active(mid,now) and event["kind"] != "final":
            with ledger.db:
                ledger.db.execute("UPDATE outbox SET status='superseded' WHERE event=?",(event["event"],))
            counts["superseded"] += 1
            continue
        if event["kind"] == "candidate":
            body = json.loads(event["body"])
            current = ledger.item(mid,body["id"])
            if not current or triage(json.loads(current["snapshot"]), json.loads(monitor["config"]))[0] == "rejected" or current["pending"] or current["decision"] != "candidate" or body.get("criteria") != monitor["criteria_hash"] or body.get("evaluator_version") != monitor["evaluator_version"] or body.get("fingerprint") != current["revision"]:
                with ledger.db:
                    ledger.db.execute("UPDATE outbox SET status='superseded' WHERE event=?",(event["event"],))
                counts["superseded"] += 1
                continue
            if json.loads(current["snapshot"]).get("availability") == "unknown":
                continue  # Keep pending until a bounded detail read confirms the current state.
        elif event["kind"] == "price_change":
            body = json.loads(event["body"])
            current = ledger.item(mid,body["id"])
            snapshot = json.loads(current["snapshot"]) if current else {}
            if not current or triage(snapshot, json.loads(monitor["config"]))[0] == "rejected" or body.get("criteria") != monitor["criteria_hash"] or body.get("price") != snapshot.get("price"):
                with ledger.db:
                    ledger.db.execute("UPDATE outbox SET status='superseded' WHERE event=?",(event["event"],))
                counts["superseded"] += 1
                continue
            if snapshot.get("availability") == "unknown":
                continue
        with ledger.db:
            claimed = ledger.db.execute("UPDATE outbox SET status='uncertain',attempts=attempts+1 WHERE event=? AND status='pending'",(event["event"],)).rowcount
        if not claimed:
            continue
        try:
            result = subprocess.run([sender,"xianyu-monitor",message(event["body"])],stdin=subprocess.DEVNULL,capture_output=True,timeout=45,check=False)
            # Sender exit 1 is guaranteed preflight failure. Exit 2 may be network ambiguity.
            status = "sent" if result.returncode == 0 else "pending" if result.returncode == 1 else "uncertain"
        except (OSError,subprocess.TimeoutExpired):
            status = "uncertain"
        with ledger.db:
            ledger.db.execute("UPDATE outbox SET status=? WHERE event=?",(status,event["event"]))
        counts[{"pending":"retry"}.get(status,status)] += 1
        if status != "sent":
            break
    return counts
