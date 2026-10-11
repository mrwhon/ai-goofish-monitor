"""One versioned generic configuration; host settings live only in runtime."""
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .contracts import EVALUATOR_VERSION
from .policy import validate_rules


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid_{name}")
    return value


def validate_config(config):
    if not isinstance(config, dict) or config.get("schema") != 2:
        raise ValueError("unsupported_config")
    runtime = config.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("invalid_runtime")
    monitors = config.get("monitors")
    if not isinstance(monitors, list) or not 1 <= len(monitors) <= 10:
        raise ValueError("invalid_monitors")
    seen = set()
    for m in monitors:
        mid = m.get("monitor_id", "")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", mid) or mid in seen:
            raise ValueError("invalid_monitor_id")
        seen.add(mid)
        if type(m.get("enabled", True)) is not bool:
            raise ValueError("invalid_enabled")
        m.setdefault("enabled", True)
        queries = m.get("queries", [])
        if not isinstance(queries, list) or not 1 <= len(queries) <= 8:
            raise ValueError("invalid_queries")
        for q in queries:
            if not isinstance(q, dict) or not isinstance(q.get("keyword"), str) or not 1 <= len(q["keyword"].strip()) <= 80:
                raise ValueError("invalid_keyword")
        if not isinstance(m.get("criteria"), dict):
            raise ValueError("invalid_criteria")
        validate_rules(m["criteria"])
        m.setdefault("evaluator_version", EVALUATOR_VERSION)
        m.setdefault("baseline_refs", [])
        m.setdefault("watch_items", [])
        if any(not str(i).isdigit() for i in m["watch_items"]):
            raise ValueError("invalid_watch_id")
        collection = m.setdefault("collection_policy", {})
        if not isinstance(collection, dict):
            raise ValueError("invalid_collection_policy")
        collection.setdefault("enabled", False)
        collection.setdefault("max_per_run", 1)
        if type(collection["enabled"]) is not bool:
            raise ValueError("invalid_collection_enabled")
        integer(collection["max_per_run"], 1, 3, "collection_budget")
        if collection["enabled"] and not re.fullmatch(r"[0-9]+", str(collection.get("expected_account_id", ""))):
            raise ValueError("invalid_collection_account")
        if not isinstance(m["baseline_refs"], list) or len(m["baseline_refs"]) > 10:
            raise ValueError("invalid_baselines")
        for baseline in m["baseline_refs"]:
            if not isinstance(baseline,dict) or not isinstance(baseline.get("item"),dict):
                raise ValueError("invalid_baseline")
            item = baseline["item"]
            if not str(item.get("id","")).isdigit() or not isinstance(item.get("title"),str) or not item["title"].strip():
                raise ValueError("invalid_baseline_item")
            for field,value in (("baseline_price",item.get("price")),("quote",baseline.get("quote",1))):
                if type(value) not in (int,float) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"invalid_{field}")
        for field in ("min_price","max_price"):
            if field in m["criteria"]:
                value = m["criteria"][field]
                if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
                    raise ValueError("invalid_price_bound")
        m.setdefault("duration_days", 14)
        integer(m["duration_days"], 1, 90, "duration")
        s = m.setdefault("schedule", {"timezone": "Asia/Taipei", "time": "10:30"})
        ZoneInfo(s["timezone"])
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", s["time"]):
            raise ValueError("invalid_schedule")
        scan = m.setdefault("scan_budget", {})
        scan.setdefault("pages", 1)
        scan.setdefault("details", 3)
        scan.setdefault("timeout", 600)
        scan.setdefault("total_pages", 12)
        scan.setdefault("triage_items", 500)
        integer(scan["total_pages"], 1, 32, "total_pages")
        integer(scan["triage_items"], 1, 5000, "triage_items")
        integer(scan["pages"], 1, 2, "pages")
        integer(scan["details"], 1, 12, "details")
        integer(scan["timeout"], 30, 900, "timeout")
        search = m.setdefault("search_policy", {})
        if not isinstance(search, dict):
            raise ValueError("invalid_search_policy")
        search.setdefault("sorts", ["新发布", "新降价"])
        search.setdefault("publish_window", "3天内")
        search.setdefault("sweep_every", 7)
        if not isinstance(search["sorts"], list) or not search["sorts"] or any(s not in ("新发布", "新降价") for s in search["sorts"]) or len(set(search["sorts"])) != len(search["sorts"]):
            raise ValueError("invalid_search_sorts")
        if search["publish_window"] not in ("最新", "1天内", "3天内", "7天内", "14天内"):
            raise ValueError("invalid_publish_window")
        integer(search["sweep_every"], 0, 90, "sweep_every")
        ai = m.setdefault("analysis_budget", {})
        ai.setdefault("calls", 3)
        ai.setdefault("input_chars", 20000)
        ai.setdefault("output_tokens", 1800)
        integer(ai["calls"], 1, 6, "ai_calls")
        integer(ai["input_chars"], 2000, 30000, "ai_input")
        integer(ai["output_tokens"], 300, 3000, "ai_output")
        if scan["details"] < ai["calls"]:
            raise ValueError("details_less_than_analysis_budget")
        threshold = m["criteria"].get("price_change_fraction", .05)
        if not isinstance(threshold, (float, int)) or not math.isfinite(threshold) or not 0 < threshold <= 1:
            raise ValueError("invalid_price_change_fraction")
        m.setdefault("notification_policy", {"initial_baseline_only": True})
        m["notification_policy"].setdefault("watch_price_changes", True)
        for field in ("initial_baseline_only", "watch_price_changes"):
            if type(m["notification_policy"].get(field, True)) is not bool:
                raise ValueError("invalid_notification_policy")
    return config


def load_config(path):
    return validate_config(json.loads(Path(path).read_text()))
