"""Generic monitor CLI. One database lock owns all persistence/browser/send effects."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .analysis import Analyzer
from .browser import Browser, CaptureError
from .collection import collect_candidates
from .config import load_config
from .contracts import AnalysisError, AnalysisInput, AnalysisResult
from .ledger import Ledger, digest
from .notifications import flush, message
from .policy import criteria_id, matching_rule, triage
from .transport import ChatTransport


def prioritize(item, monitor):
    rule = matching_rule(item, monitor["criteria"])
    if rule:
        price, ceiling = item.get("price"), rule.get("ceiling")
        return max(.1, 2-price/ceiling) if price is not None and ceiling else 1
    return 0


def search_plan(monitor, prior, query_limit=None):
    queries = monitor["queries"][:query_limit] if query_limit else monitor["queries"]
    offset = prior % len(queries)
    queries = queries[offset:] + queries[:offset]
    policy = monitor["search_policy"]
    every = policy["sweep_every"]
    sweep = every > 0 and (prior+1) % every == 0
    plan, remaining = [], monitor["scan_budget"]["total_pages"]
    # Interleave sorts by query, rotating priority when a budget truncates coverage.
    for index, query in enumerate(queries):
        for sort in policy["sorts"]:
            wanted = 2 if sweep and index == 0 else monitor["scan_budget"]["pages"]
            pages = min(wanted, remaining)
            plan.append({"keyword": query["keyword"], "sort": sort, "pages": pages, "requested_pages": wanted, "window": "最新" if sweep and index == 0 else policy["publish_window"]})
            remaining -= pages
    return plan


def rule_triage(ledger, monitor, row):
    item = json.loads(row["snapshot"])
    decision, reason = triage(item, monitor)
    mid = monitor["monitor_id"]
    if decision == "rejected":
        ledger.assess(mid,row["id"],AnalysisResult("rejected",reason,analysis_source="rule"),criteria_id(monitor),monitor["evaluator_version"])
    else:
        with ledger.db:
            ledger.db.execute("UPDATE monitor_items SET priority=?,pending=CASE WHEN ?='deferred' THEN 'awaiting_price' ELSE pending END WHERE monitor_id=? AND item_key=?", (prioritize(item, monitor),decision,mid,row["item_key"]))
    return decision


def input_for(ledger, monitor, item_id):
    row = ledger.item(monitor["monitor_id"], item_id)
    item = json.loads(row["snapshot"])
    gaps = [name+" 未提供" for name in ("price","description","seller_id","availability") if not item.get(name)]
    if item.get("availability") == "unknown":
        gaps.append("在售状态尚未确认")
    item["image_count"] = len(item.pop("images",[]))
    item.pop("thumbnail",None)  # Text transport does not read pixels/URLs.
    if len(item.get("description", "")) > 8000:
        item["description"] = item["description"][:8000]
        gaps.append("描述超过8000字，本次只读取前段，完整文字仍留在账本")
    if len(item.get("title", "")) > 500:
        item["title"] = item["title"][:500]
        gaps.append("标题过长，已限量")
    return AnalysisInput(monitor["monitor_id"],criteria_id(monitor),monitor["evaluator_version"],row["revision"],item,monitor["criteria"],monitor.get("baseline_refs",[]),gaps)


def summary_event(ledger, monitor, kind, text, key):
    mid = monitor["monitor_id"]
    ledger.enqueue(mid,kind,{"summary":text},digest([mid,kind,key]))


def finalize_expiry(ledger, monitor, now=None):
    now = time.time() if now is None else now
    mid = monitor["monitor_id"]
    m = ledger.monitor(mid)
    if not m or not m["enabled"] or m["stopped"] or m["expires_at"] is None or now < m["expires_at"]:
        return
    runs = ledger.run_count(mid)
    assessments = ledger.db.execute("SELECT count(*) FROM assessments WHERE monitor_id=? AND evaluator_version!='legacy-v1'",(mid,)).fetchone()[0]
    status = ledger.status(mid)
    summary_event(ledger,monitor,"final",f"闲鱼监控试运行到期 [{mid}]\n已停止采集与AI调用。完成 {runs} 次巡检，{assessments} 次判断，保留 {status['items']} 个商品的历史。\n到期不代表已买到合适商品；详细数据保留在本机。",m["expires_at"])


async def scan(ledger, monitor, runtime, prepare=False, query_limit=None, analyzer=None, cache=None):
    mid = monitor["monitor_id"]
    if not monitor.get("enabled",True) or (not prepare and not ledger.active(mid)):
        return {"status":"inactive_or_expired",**ledger.status(mid)}
    if ledger.meta("blocked") or ledger.monitor(mid)["blocked"]:
        return {"status":"blocked","monitor_id":mid,"reason":"platform_boundary"}
    mode = "prepare" if prepare else "live"
    run = ledger.start_run(mid,mode)
    prior = ledger.run_count(mid,mode)
    coverage, analyzed, details = [], 0, 0
    detail_cache = {}
    status, error = "ok", None
    collection = None
    cache = {} if cache is None else cache
    initial = not prepare and prior == 0 and monitor["notification_policy"].get("initial_baseline_only",True)
    budget = {k:monitor["analysis_budget"][k] for k in ("input_chars","output_tokens")}
    try:
        analyzer = analyzer or Analyzer(ChatTransport(runtime["ai"],ledger),budget=budget)
        deadline = monitor["scan_budget"]["timeout"]
        if not prepare:
            deadline = min(deadline,ledger.monitor(mid)["expires_at"]-time.time())
        async with asyncio.timeout(deadline):
            async with Browser(runtime["reader_file"],runtime["cdp"]) as browser:
                watch = monitor.get("watch_items",[])
                # Successfully saved candidates become task watches without editing private config.
                watch = list(dict.fromkeys([*watch, *[r[0] for r in ledger.db.execute(
                    "SELECT i.id FROM monitor_items m JOIN items i USING(item_key) WHERE m.monitor_id=? AND m.watch=1",
                    (mid,))]]))
                favorite_entry, unresolved, changed_watch = None, set(), []
                if watch:
                    try:
                        items, favorite_entry = await browser.favorites(watch)
                    except CaptureError as exc:
                        if str(exc) not in ("favorite_card_schema_changed", "favorite_price_schema_changed", "favorite_duplicate_price_conflict"):
                            raise
                        items, favorite_entry = [], {"source": "favorites", "pages": 1, "found": 0,
                            "missing": watch, "unresolved": watch, "bounded": True, "truncated": True,
                            "errors": [{"reason": str(exc)}]}
                    coverage.append(favorite_entry)
                    unresolved.update([*favorite_entry.get("missing", []), *favorite_entry.get("unresolved", [])])
                    for item in items:
                        previous = ledger.item(mid,item["id"])
                        snapshot = json.loads(previous["snapshot"]) if previous else {}
                        if item.get("price") is not None and item["price"] != snapshot.get("price"):
                            changed_watch.append(item["id"])
                            item = {**item, "availability": "unknown"}
                        elif snapshot.get("availability") == "unknown":
                            changed_watch.append(item["id"])
                        ledger.observe(mid,item,"favorites",notify_changes=not prepare and not initial)
                    for wid in unresolved:
                        ledger.observe(mid,{"id":wid,"availability":"unknown"},"favorites")
                queries = monitor["queries"][:query_limit] if query_limit else monitor["queries"]
                if query_limit and len(queries)<len(monitor["queries"]):
                    coverage.append({"source":"budget","query_limit":query_limit,"truncated":True})
                for entry_plan in search_plan(monitor,prior,query_limit):
                    query, pages, sort, window = entry_plan["keyword"], entry_plan["pages"], entry_plan["sort"], entry_plan["window"]
                    if not pages:
                        coverage.append({"keyword":query,"filter":sort,"pages":0,"truncated":True,"stop_reason":"total_page_budget"})
                        continue
                    key = (query,pages,sort,window)
                    if key not in cache:
                        cache[key] = await browser.search(*key)
                    items, entry = cache[key]
                    coverage.append({**entry,"keyword":query,"filter":sort,"requested_pages":entry_plan["requested_pages"],"budget_truncated":pages < entry_plan["requested_pages"]})
                    for item in items:
                        ledger.observe(mid,item,query,notify_changes=not prepare and not initial)
                # Rule triage every pending relation, independent of the scarce detail/AI budget.
                for row in ledger.triage_rows(mid,monitor["scan_budget"]["triage_items"]):
                    rule_triage(ledger,monitor,row)
                # Unclear/changed watches get the spare detail budget before routine rotation.
                collection_slots = (monitor.get("collection_policy", {}).get("max_per_run", 1)
                                    if not prepare and monitor.get("collection_policy", {}).get("enabled") else 0)
                watch_slots = max(0,monitor["scan_budget"]["details"]-monitor["analysis_budget"]["calls"]-collection_slots)
                targets = sorted(unresolved)
                if targets:
                    # Partial rounds must rotate too; success-only cadence can starve another bad card.
                    attempts = ledger.db.execute("SELECT count(*) FROM runs WHERE monitor_id=? AND mode=?",(mid,mode)).fetchone()[0]-1
                    offset = attempts % len(targets)
                    targets = targets[offset:]+targets[:offset]
                targets = list(dict.fromkeys([*targets,*changed_watch,*([watch[prior % len(watch)]] if watch else [])]))
                resolved, failed_watches = set(), set()
                for wid in targets[:watch_slots]:
                    details += 1  # Failed reads also consume the detail allocation.
                    try:
                        fact = await browser.detail(wid)
                        detail_cache[wid] = fact
                        ledger.observe(mid,fact,"watch_detail",notify_changes=not prepare and not initial)
                        rule_triage(ledger,monitor,ledger.item(mid,wid))
                        if fact.get("price") is not None:
                            resolved.add(wid)
                    except (CaptureError,ValueError) as exc:
                        if any(mark in str(exc).upper() for mark in ("PLATFORM_BOUNDARY","VISIBLE_","LOGIN","VALIDATE","RGV","BAXIA")):
                            raise
                        coverage.append({"source":"watch_detail","id":wid,"error":type(exc).__name__,"truncated":True})
                        failed_watches.add(wid)
                        status = "partial"
                if favorite_entry is not None:
                    favorite_entry["resolved_by_detail"] = sorted(unresolved & resolved)
                    favorite_entry["unresolved_after_detail"] = sorted(unresolved-resolved)
                    unconfirmed = [wid for wid in dict.fromkeys([*changed_watch,*unresolved]) if json.loads(ledger.item(mid,wid)["snapshot"]).get("availability") == "unknown"]
                    favorite_entry["unconfirmed_status"] = unconfirmed
                    if unresolved-resolved or unconfirmed or favorite_entry.get("skipped_cards"):
                        status = "partial"
                queue = ledger.pending(mid,monitor["analysis_budget"]["calls"])
                reserved = (min(monitor.get("collection_policy", {}).get("max_per_run", 1),
                                max(0, monitor["scan_budget"]["details"]-1))
                            if not prepare and monitor.get("collection_policy", {}).get("enabled") else 0)
                for row in queue:
                    if row["id"] in failed_watches:
                        continue
                    if details >= monitor["scan_budget"]["details"]-reserved or analyzed >= monitor["analysis_budget"]["calls"]:
                        break
                    try:
                        if rule_triage(ledger,monitor,row) in ("rejected", "deferred"):
                            continue
                        if row["id"] in detail_cache:
                            fact = detail_cache[row["id"]]
                        else:
                            fact = await browser.detail(row["id"])
                            detail_cache[row["id"]] = fact
                            details += 1
                        pending = ledger.observe(mid,fact,"detail",notify_changes=not prepare and not initial)
                        if pending is None:
                            continue  # List changes resolved to the exact already-assessed detail.
                        request = input_for(ledger,monitor,row["id"])
                        decision, reason = triage(request.item,monitor)
                        if decision == "rejected":
                            result = AnalysisResult("rejected",reason,analysis_source="rule")
                        elif decision == "deferred":
                            rule_triage(ledger,monitor,ledger.item(mid,row["id"]))
                            continue
                        else:
                            analyzed += 1  # Failed calls also consume this run's call allocation.
                            result = await analyzer.analyze(request)
                        notify = result.decision == "candidate" and not prepare and not initial and ledger.active(mid)
                        ledger.assess(mid,row["id"],result,request.criteria_hash,request.evaluator_version,request.item_revision,notify)
                    except (AnalysisError,ValueError) as exc:
                        detail = str(exc).split(":", 1)[0]
                        safe = detail if re.fullmatch(r"[a-z_]{1,80}", detail) else type(exc).__name__
                        ledger.mark_error(mid,row["id"],safe)
                        status = "partial"
                if not prepare and status == "ok":
                    collection = await collect_candidates(ledger,monitor,browser,
                        detail_budget=monitor["scan_budget"]["details"]-details)
                    details += collection["detail_reads"]
                    if collection["uncertain"] or collection["failed"]:
                        status = "partial"
    except Exception as exc:
        status, error = "failed", type(exc).__name__
        detail = str(exc)
        if re.fullmatch(r"[a-z_]{1,80}", detail) and detail.startswith(("visible_","platform_","page_","search_","detail_","image_","favorite_","collection_","expected_","invalid_api_","network_not_idle","owned_target_")):
            error += ":" + detail
        if any(mark in detail for mark in ("platform_boundary","visible_login_or_challenge","LOGIN","SESSION_EXPIRED","VALIDATE","RGV","BAXIA")):
            ledger.meta("blocked","platform_boundary")
            with ledger.db:
                ledger.db.execute("UPDATE monitors SET blocked='platform_boundary' WHERE monitor_id=?",(mid,))
    with ledger.db:
        ledger.db.execute("UPDATE runs SET completed=?,status=?,coverage=?,analyses=?,details=?,error=? WHERE id=?",(time.time(),status,json.dumps(coverage,ensure_ascii=False),analyzed,details,error,run))
    if not prepare and status == "ok":
        state = ledger.status(mid)
        if prior == 0:
            expiry = datetime.fromtimestamp(state['expires_at'],ZoneInfo(monitor['schedule']['timezone'])).isoformat(timespec='minutes')
            summary_event(ledger,monitor,"startup",f"闲鱼商品监控：{monitor['duration_days']}天试运行已启动 [{mid}]\n本轮 {len(queries)} 个查询，详情 {details} 条，AI {analyzed} 次；已见 {state['items']} 个商品。\n首次安静建立基线；只有值得核实的新候选或重要变化才提醒。\n每天 {monitor['schedule']['time']}（{monitor['schedule']['timezone']}），截止 {expiry}。\n覆盖有限搜索页，不代表全市场；图片未检，商品质量须另行核实。",ledger.monitor(mid)["activated_at"])
        elif (prior+1)%7 == 0:
            summary_event(ledger,monitor,"heartbeat",f"闲鱼监控周报 [{mid}]\n累计成功巡检 {prior+1} 次；已见 {state['items']} 项，待复评 {state['pending']} 项（待有效报价 {state['awaiting_price']}）。\n本轮AI {analyzed} 次；详情 {details} 条；截断来源 {sum(bool(e.get('truncated') or e.get('budget_truncated')) for e in coverage)} 个。没有通知不代表没运行。",(prior+1)//7)
    return {"status":status,"monitor_id":mid,"mode":mode,"run_id":run,"coverage":coverage,"analyses":analyzed,"details":details,"error":error,"collection":collection,**ledger.status(mid)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",required=True)
    parser.add_argument("--database",required=True)
    parser.add_argument("--monitor")
    commands = parser.add_subparsers(dest="command",required=True)
    for name in ("register","status","activate","stop","resume","unblock","health","run"):
        commands.add_parser(name)
    capture = commands.add_parser("scan")
    capture.add_argument("--prepare",action="store_true")
    capture.add_argument("--query-limit",type=int,choices=range(1,9))
    send = commands.add_parser("flush")
    send.add_argument("--notify",required=True,action="store_true")
    reconcile = commands.add_parser("reconcile-send")
    reconcile.add_argument("event")
    reconcile.add_argument("--result",choices=("sent","retry"),required=True)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    path = Path(args.database)
    if not path.parent.is_dir():
        raise ValueError("database_parent_missing")
    selected = [m for m in config["monitors"] if not args.monitor or m["monitor_id"] == args.monitor]
    if not selected or (args.command in ("activate","stop","resume","unblock","reconcile-send") and len(selected) != 1):
        raise ValueError("explicit_single_monitor_required")
    os.umask(0o077)
    with open(str(path)+".lock","a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status":"already_running"}))
            return 0
        ledger = Ledger(path)
        try:
            for m in config["monitors"]:
                ledger.register(m,criteria_id(m))
            results, cache = [], {}
            for m in selected:
                mid = m["monitor_id"]
                result = None
                if args.command == "activate":
                    ledger.activate(mid)
                elif args.command == "stop":
                    with ledger.db:
                        ledger.db.execute("UPDATE monitors SET stopped=1 WHERE monitor_id=?",(mid,))
                elif args.command == "resume":
                    ledger.resume(mid)
                elif args.command == "unblock":
                    with ledger.db:
                        ledger.db.execute("DELETE FROM meta WHERE key='blocked'")
                        ledger.db.execute("UPDATE monitors SET blocked=NULL WHERE monitor_id=?",(mid,))
                elif args.command in ("scan","run"):
                    finalize_expiry(ledger,m)
                    result = asyncio.run(scan(ledger,m,config["runtime"],getattr(args,"prepare",False),getattr(args,"query_limit",None),cache=cache))
                    if args.command == "run":
                        result["delivery"] = flush(ledger,mid,config["runtime"]["sender"])
                elif args.command == "flush":
                    result = flush(ledger,mid,config["runtime"]["sender"])
                elif args.command == "health":
                    result = ledger.health(mid)
                elif args.command == "reconcile-send":
                    with ledger.db:
                        ledger.db.execute("UPDATE outbox SET status=? WHERE monitor_id=? AND event=? AND status='uncertain'",("sent" if args.result == "sent" else "pending",mid,args.event))
                results.append(result or ledger.status(mid))
            print(json.dumps(results,ensure_ascii=False))
            return int(any(r.get("status") in ("failed","partial","blocked","degraded") or r.get("delivery",{}).get("uncertain") or r.get("delivery",{}).get("retry") for r in results))
        finally:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
