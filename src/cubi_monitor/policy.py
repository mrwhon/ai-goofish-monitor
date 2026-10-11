"""Generic, conservative triage; observation thresholds are never recommendations."""
import hashlib
import json
import math
import re

from src.keyword_rule_engine import normalize_text
from .contracts import EVALUATOR_VERSION


def criteria_id(profile):
    """Only evaluation inputs invalidate cached assessments."""
    value = {
        "criteria": profile.get("criteria", {}),
        "baseline_refs": profile.get("baseline_refs", []),
        "evaluator_version": profile.get("evaluator_version", EVALUATOR_VERSION),
    }
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def triage(item, profile):
    """Reject only explicit hard exclusions; all unknown configurations need analysis."""
    criteria = profile.get("criteria", {})
    if not isinstance(criteria, dict):
        return "needs_analysis", "筛选条件格式未知，保留待复评"
    text = normalize_text(" ".join(str(item.get(k) or "") for k in ("title", "description")))
    exclusions = criteria.get("hard_exclusions", [])
    if not isinstance(exclusions, list):
        return "needs_analysis", "硬排除条件格式未知，保留待复评"
    compiled = []
    try:
        for exclusion in exclusions:
            pattern = exclusion.get("pattern") if isinstance(exclusion, dict) else exclusion
            reason = exclusion.get("reason") if isinstance(exclusion, dict) else None
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError("invalid exclusion")
            if reason is not None and (not isinstance(reason, str) or not reason.strip()):
                raise ValueError("invalid reason")
            compiled.append((re.compile(pattern, re.I), reason))
        for key in ("min_price", "max_price"):
            if key in criteria and (not _number(criteria[key]) or criteria[key] < 0):
                raise ValueError("invalid price bound")
        if criteria.get("min_price", 0) > criteria.get("max_price", math.inf):
            raise ValueError("inverted price bounds")
    except (ValueError, TypeError, re.error):
        return "needs_analysis", "硬排除条件无效，保留待复评"
    for pattern, reason in compiled:
        if pattern.search(text):
            return "rejected", reason or "命中已配置的明确硬排除条件"
    price = item.get("price")
    if not _number(price) or price < 0:
        return "needs_analysis", "完整售价未知，需核实后比较"
    if "min_price" in criteria and price < criteria["min_price"]:
        return "rejected", f"售价低于已配置最低单价{criteria['min_price']}"
    if "max_price" in criteria and price > criteria["max_price"]:
        return "rejected", f"售价超过已配置最高单价{criteria['max_price']}"
    rules = criteria.get("rules", [])
    if not isinstance(rules, list):
        return "needs_analysis", "观察规则格式未知，需AI比较"
    for rule in rules:
        try:
            if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str):
                raise ValueError("invalid rule")
            if not rule["pattern"].strip() or not _number(rule.get("ceiling")):
                raise ValueError("invalid rule")
            if re.search(rule["pattern"], text, re.I):
                position = "内" if price <= rule["ceiling"] else "外"
                return "needs_analysis", f"处于{rule.get('label', '已配置规则')}观察价{position}；仍需与基准按同口径比较"
        except (ValueError, TypeError, re.error):
            return "needs_analysis", "观察规则无效，保留待复评"
    return "needs_analysis", "现有规则不足以判断此商品，需AI复评并核对证据缺口"


def judge(item, profile):
    """Legacy tuple entry point, with the same conservative triage semantics."""
    return triage(item, profile)
