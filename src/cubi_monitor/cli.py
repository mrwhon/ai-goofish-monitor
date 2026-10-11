import argparse
import asyncio
import fcntl
import html
import json
import os
from pathlib import Path
import subprocess
import time

from .browser import Browser
from .ledger import Ledger, material_digest
from .policy import criteria_id, judge


def load_profile(path):
    profile = json.loads(Path(path).read_text())
    if profile.get("schema") != 1:
        raise ValueError("unsupported_profile")
    if not 1 <= len(profile["queries"]) <= 8:
        raise ValueError("query_count_out_of_bounds")
    for query in profile["queries"]:
        if not isinstance(query["keyword"], str) or len(query["keyword"]) > 80:
            raise ValueError("invalid_keyword")
    if not 1 <= profile.get("pages", 2) <= 2 or not 1 <= profile.get("detail_limit", 3) <= 3:
        raise ValueError("capture_limit_out_of_bounds")
    if not 30 <= profile.get("timeout", 600) <= 900:
        raise ValueError("timeout_out_of_bounds")
    return profile


def message(body):
    body = json.loads(body)
    esc = lambda s: html.escape(str(s))
    quote = f"；已知议价 {esc(body['quote'])} 元（未重新确认）" if body.get("quote") is not None else ""
    return f"闲鱼待核实候选\n{esc(body['title'][:300])}\n标价 {esc(body['price'])} 元{quote}\n{esc(body['reason'][:700])}\n{esc(body['url'])}"


def flush(ledger, sender, now=None):
    if not ledger.active(now):
        raise ValueError("trial_inactive_or_expired")
    count = 0
    for event in ledger.outbox():
        body = json.loads(event["body"])
        current = ledger.db.execute("SELECT * FROM items WHERE id=?", (event["item_id"],)).fetchone()
        if current["pending"] or body.get("criteria") != current["criteria"] or body.get("fingerprint") != material_digest(json.loads(current["snapshot"])):
            ledger.finish_send(event["event"], False)
            continue  # Keep stale/ambiguous events for explicit reconciliation, never recommend stale facts.
        ledger.claim_send(event["event"])
        try:
            result = subprocess.run([sender, "xianyu-monitor", message(event["body"])], stdin=subprocess.DEVNULL, capture_output=True, timeout=45, check=False)
            ledger.finish_send(event["event"], result.returncode == 0)
            if result.returncode:
                break  # The sender's nonzero response cannot rule out network ambiguity.
            count += 1
        except (OSError, subprocess.TimeoutExpired):
            ledger.finish_send(event["event"], False)
            break
    return count


async def scan(ledger, profile, prepare=False, query_limit=None):
    if not prepare and not ledger.active():
        return {"status": "inactive_or_expired", **ledger.status()}
    if ledger.meta("blocked"):
        return {"status": "blocked", "reason": ledger.meta("blocked")}
    criteria = criteria_id(profile)
    ledger.requeue_criteria(criteria)
    started = time.time()
    with ledger.db:
        run = ledger.db.execute("INSERT INTO runs(started,status) VALUES (?,?)", (started, "running")).lastrowid
    coverage, assessed = [], 0
    status, error = "ok", None
    try:
        deadline = profile.get("timeout", 600)
        if not prepare:
            deadline = min(deadline, float(ledger.meta("expires_at")) - started)
        async with asyncio.timeout(deadline):
            async with Browser(profile["reader_file"], profile["cdp"]) as browser:
                watch_ids = [str(i["id"]) for i in profile.get("seeds", []) if i["decision"] == "watch"]
                items, entry = await browser.favorites(watch_ids)
                coverage.append(entry)
                for item in items:
                    ledger.observe(item, "favorites", criteria)
                if entry["missing"]:
                    status = "partial"
                # Alternate newest and native markdown listings; dates do not determine identity.
                prior_runs = ledger.db.execute("SELECT count(*) FROM runs WHERE status IN ('ok','partial')").fetchone()[0]
                sort = "新降价" if prior_runs % 3 == 2 else "新发布"
                queries = profile["queries"][:query_limit] if query_limit else profile["queries"]
                for query in queries:
                    window = "最新" if prior_runs % 7 == 6 else "3天内"
                    items, entry = await browser.search(query["keyword"], profile.get("pages", 2), sort, window)
                    coverage.append(entry)
                    for item in items:
                        pending = ledger.observe(item, query["keyword"], criteria)
                        if pending:
                            # Cheap list triage caches non-candidates; detail budget serves worthwhile leads.
                            decision, reason = judge(item, profile)
                            if decision == "reserve":
                                ledger.assess(item["id"], decision, reason, criteria)
                # Existing detail evidence is refreshed round-robin; never exceed the shared budget.
                pending = ledger.pending(profile.get("detail_limit", 3))
                for row in pending:
                    detail = await browser.detail(row["id"])
                    ledger.observe(detail, "detail", criteria)
                    current = ledger.db.execute("SELECT snapshot FROM items WHERE id=?", (row["id"],)).fetchone()
                    decision, reason = judge(json.loads(current["snapshot"]), profile)
                    ledger.assess(row["id"], decision, reason, criteria, notify=decision == "candidate")
                    assessed += 1
                remaining = profile.get("detail_limit", 3) - assessed
                if remaining and watch_ids:
                    offset = int(ledger.meta("watch_offset") or 0)
                    for j in range(min(remaining, len(watch_ids))):
                        item_id = watch_ids[(offset + j) % len(watch_ids)]
                        detail = await browser.detail(item_id)
                        changed = ledger.observe(detail, "watch_detail", criteria)
                        if changed:
                            row = ledger.db.execute("SELECT snapshot FROM items WHERE id=?", (item_id,)).fetchone()
                            decision, reason = judge(json.loads(row["snapshot"]), profile)
                            ledger.assess(item_id, decision, reason, criteria, notify=decision == "candidate")
                            assessed += 1
                    ledger.meta("watch_offset", offset + min(remaining, len(watch_ids)))
    except Exception as exc:
        # Preserve completed observations, mark partial coverage; never report an empty successful scan.
        status, error = "failed", type(exc).__name__ + ":" + str(exc)[:120]
        if any(word in str(exc) for word in ("boundary", "login", "challenge")):
            ledger.meta("blocked", error)
    with ledger.db:
        ledger.db.execute("UPDATE runs SET completed=?,status=?,coverage=? WHERE id=?", (time.time(), status, json.dumps(coverage, ensure_ascii=False), run))
    return {"status": status, "error": error, "run": run, "coverage": coverage, "assessed": assessed, "seconds": round(time.time() - started, 1), **ledger.status()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cubi daily read-only shortlist monitor. No daemon, browser launch, login export or purchase.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--database", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed")
    commands.add_parser("status")
    commands.add_parser("activate", help="Start the immutable 14-day window; does not install a scheduler")
    commands.add_parser("stop")
    commands.add_parser("unblock", help="Explicitly permit one new attempt after manual login/challenge recovery")
    scan_args = commands.add_parser("scan")
    scan_args.add_argument("--prepare", action="store_true", help="Authorized one-shot preactivation read; no sends")
    scan_args.add_argument("--query-limit", type=int, choices=range(1, 9))
    flush_args = commands.add_parser("flush", help="Explicit send through Cubi telegram-send.sh")
    flush_args.add_argument("--notify", action="store_true", required=True)
    retry = commands.add_parser("reconcile-send", help="Resolve one ambiguous send; do not automatically resend")
    retry.add_argument("event")
    retry.add_argument("--result", choices=("sent", "retry"), required=True)
    decision_args = commands.add_parser("decision")
    decision_args.add_argument("item_id")
    decision_args.add_argument("--decision", choices=("watch", "reserve", "rejected", "candidate"), required=True)
    decision_args.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    path = Path(args.database)
    if not path.parent.is_dir():
        raise ValueError("database_parent_missing")
    os.umask(0o077)
    with open(str(path) + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "already_running"}))
            return 0
        ledger = Ledger(path)
        try:
            result = None
            if args.command == "seed":
                ledger.seed(profile.get("seeds", []), criteria_id(profile))
            elif args.command == "activate":
                ledger.activate()
            elif args.command == "stop":
                ledger.meta("stopped", "1")
            elif args.command == "unblock":
                with ledger.db:
                    ledger.db.execute("DELETE FROM meta WHERE key='blocked'")
            elif args.command == "scan":
                result = asyncio.run(scan(ledger, profile, args.prepare, args.query_limit))
            elif args.command == "flush":
                result = {"sent": flush(ledger, profile["sender"])}
            elif args.command == "reconcile-send":
                with ledger.db:
                    ledger.db.execute("UPDATE outbox SET status=? WHERE event=? AND status='uncertain'", ("sent" if args.result == "sent" else "pending", args.event))
            elif args.command == "decision":
                ledger.assess(args.item_id, args.decision, args.reason, criteria_id(profile))
            print(json.dumps(result or ledger.status(), ensure_ascii=False))
            return 1 if result and result.get("status") in ("failed", "partial", "blocked") else 0
        finally:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
