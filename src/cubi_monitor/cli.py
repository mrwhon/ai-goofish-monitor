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
from .config import load_config
from .contracts import AnalysisError, AnalysisInput, AnalysisResult
from .ledger import Ledger, digest
from .notifications import flush, message
from .policy import criteria_id, triage
from .transport import ChatTransport


def prioritize(item, monitor):
    text = item.get("title", "")+" "+item.get("description", "")
    for rule in monitor["criteria"].get("rules", []):
        if re.search(rule["pattern"], text, re.I):
            price, ceiling = item.get("price"), rule.get("ceiling")
            return max(.1, 2-price/ceiling) if price is not None and ceiling else 1
    return 0


def input_for(ledger, monitor, item_id):
    row = ledger.item(monitor["monitor_id"], item_id)
    item = json.loads(row["snapshot"])
    gaps = [name+" 未提供" for name in ("price","description","seller_id","availability") if not item.get(name)]
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
    status, error = "ok", None
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
                if watch:
                    items, entry = await browser.favorites(watch)
                    coverage.append(entry)
                    for item in items:
                        ledger.observe(mid,item,"favorites")
                    if entry.get("missing"):
                        status = "partial"
                queries = monitor["queries"][:query_limit] if query_limit else monitor["queries"]
                if query_limit and len(queries)<len(monitor["queries"]):
                    coverage.append({"source":"budget","query_limit":query_limit,"truncated":True})
                sort = "新降价" if prior % 3 == 2 else "新发布"
                window = "最新" if prior % 7 == 6 else "3天内"
                for query in queries:
                    key = (query["keyword"],monitor["scan_budget"]["pages"],sort,window)
                    if key not in cache:
                        cache[key] = await browser.search(*key)
                    items, entry = cache[key]
                    coverage.append(entry)
                    for item in items:
                        ledger.observe(mid,item,query["keyword"])
                # Rule triage every pending relation, independent of the scarce detail/AI budget.
                for row in ledger.pending(mid,100000):
                    item = json.loads(row["snapshot"])
                    decision, reason = triage(item,monitor)
                    if decision == "rejected":
                        ledger.assess(mid,row["id"],AnalysisResult("rejected",reason,analysis_source="rule"),criteria_id(monitor),monitor["evaluator_version"])
                    else:
                        with ledger.db:
                            ledger.db.execute("UPDATE monitor_items SET priority=? WHERE monitor_id=? AND item_key=?",(prioritize(item,monitor),mid,row["item_key"]))
                # One rotating watch refresh cannot consume the exploration allocation.
                if watch and monitor["scan_budget"]["details"]>monitor["analysis_budget"]["calls"]:
                    wid = watch[prior % len(watch)]
                    fact = await browser.detail(wid)
                    ledger.observe(mid,fact,"watch_detail")
                    details += 1
                queue = ledger.pending(mid,monitor["analysis_budget"]["calls"])
                for row in queue:
                    if details >= monitor["scan_budget"]["details"] or analyzed >= monitor["analysis_budget"]["calls"]:
                        break
                    try:
                        fact = await browser.detail(row["id"])
                        ledger.observe(mid,fact,"detail")
                        details += 1
                        request = input_for(ledger,monitor,row["id"])
                        decision, reason = triage(request.item,monitor)
                        if decision == "rejected":
                            result = AnalysisResult("rejected",reason,analysis_source="rule")
                        else:
                            analyzed += 1  # Failed calls also consume this run's call allocation.
                            result = await analyzer.analyze(request)
                        notify = result.decision == "candidate" and not prepare and not initial and ledger.active(mid)
                        ledger.assess(mid,row["id"],result,request.criteria_hash,request.evaluator_version,request.item_revision,notify)
                    except (AnalysisError,ValueError) as exc:
                        ledger.mark_error(mid,row["id"],type(exc).__name__)
                        status = "partial"
    except Exception as exc:
        status, error = "failed", type(exc).__name__
        detail = str(exc)
        if re.fullmatch(r"[a-z_]{1,80}", detail) and detail.startswith(("visible_","platform_","page_","search_","detail_","image_","favorite_","expected_","invalid_api_","network_not_idle","owned_target_")):
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
            summary_event(ledger,monitor,"heartbeat",f"闲鱼监控周报 [{mid}]\n累计成功巡检 {prior+1} 次；已见 {state['items']} 项，待复评 {state['pending']} 项。没有通知不代表没运行。",(prior+1)//7)
    return {"status":status,"monitor_id":mid,"mode":mode,"run_id":run,"coverage":coverage,"analyses":analyzed,"details":details,"error":error,**ledger.status(mid)}


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
