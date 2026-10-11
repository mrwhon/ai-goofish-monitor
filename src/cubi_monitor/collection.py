"""Explicit, bounded collection of current candidates; SQLite meta holds action receipts."""
import asyncio
import json
import time

from .policy import criteria_id, triage


async def collect_candidates(ledger, monitor, browser, detail_budget=None):
    policy = monitor.get("collection_policy", {})
    mid = monitor["monitor_id"]
    summary = {"added": 0, "already_saved": 0, "uncertain": 0, "failed": 0, "stale": 0, "detail_reads": 0}
    if not policy.get("enabled") or not ledger.active(mid) or ledger.meta("blocked"):
        return summary
    account = str(policy["expected_account_id"])
    limit = policy["max_per_run"] if detail_budget is None else min(policy["max_per_run"], detail_budget)
    # Bounded SQL selection. Receipts are shared by account/item across tasks.
    rows = ledger.db.execute("""SELECT i.id FROM monitor_items m JOIN items i USING(item_key)
        WHERE m.monitor_id=? AND m.decision='candidate' AND m.pending IS NULL
        AND m.assessed_revision=i.revision AND m.criteria_hash=? AND m.evaluator_version=?
        AND NOT EXISTS (SELECT 1 FROM meta WHERE key=? || i.id
                        AND json_extract(value,'$.status') IN ('verified','unconfirmed'))
        ORDER BY m.priority DESC,i.id LIMIT ?""",
        (mid, criteria_id(monitor), monitor["evaluator_version"],
         f"collection:{account}:", max(0, limit))).fetchall()
    for row in rows:
        item_id = row["id"]
        key = f"collection:{account}:{item_id}"
        previous = json.loads(ledger.meta(key) or "{}")
        attempted = False

        def valid():
            current = ledger.item(mid, item_id)
            return (ledger.active(mid) and not ledger.meta("blocked") and not ledger.monitor(mid)["blocked"]
                    and current["decision"] == "candidate" and current["pending"] is None
                    and current["assessed_revision"] == current["revision"]
                    and current["criteria_hash"] == criteria_id(monitor)
                    and current["evaluator_version"] == monitor["evaluator_version"]
                    and triage(json.loads(current["snapshot"]), monitor)[0] not in ("rejected", "deferred"))

        def guard(fact):
            ledger.observe(mid, fact, "detail")
            return valid()

        def before_click():
            nonlocal attempted
            if not valid():
                return False
            ledger.meta(key, json.dumps({"status": "attempting", "monitor_id": mid, "at": time.time()}))
            attempted = True
            return True

        try:
            summary["detail_reads"] += 1
            state = await browser.favorite(item_id, account, guard, before_click,
                allow_click=previous.get("status") not in ("attempting", "uncertain"))
            summary[state if state in summary else "uncertain"] += 1
            if state in ("added", "already_saved"):
                ledger.meta(key, json.dumps({"status": "verified", "monitor_id": mid, "at": time.time()}))
                with ledger.db:
                    ledger.db.execute("UPDATE monitor_items SET watch=1 WHERE monitor_id=? AND item_key=?",
                                      (mid, "goofish:"+item_id))
            elif state == "unconfirmed":
                ledger.meta(key, json.dumps({"status": "unconfirmed", "monitor_id": mid, "at": time.time()}))
        except BaseException as exc:
            state = "uncertain" if attempted or previous.get("status") in ("attempting", "uncertain") else "preflight_failed"
            ledger.meta(key, json.dumps({"status": state, "monitor_id": mid, "at": time.time()}))
            summary["uncertain" if state == "uncertain" else "failed"] += 1
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            # Account/challenge/control failures stop writes for the whole round.
            raise
    return summary
