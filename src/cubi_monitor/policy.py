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


def rule_text(item, rule):
    return normalize_text(" ".join(str(item.get(k) or "") for k in rule.get("fields", ["title", "description"])))


def matches(item, rule):
    text = rule_text(item, rule)
    return bool(re.search(rule["pattern"], text, re.I)) and not (rule.get("exclude_pattern") and re.search(rule["exclude_pattern"], text, re.I))


def matching_rule(item, criteria):
    return next((r for r in criteria.get("rules", []) if matches(item, r)), None)


def price_crossed(item, previous, current, criteria):
    bounds = [criteria.get("max_price")]
    rule = matching_rule(item, criteria)
    if rule:
        bounds.append(rule["ceiling"])
    return any(b is not None and (previous > b) != (current > b) for b in bounds)


def validate_rules(criteria):
    for kind in ("rules", "hard_exclusions"):
        rules = criteria.get(kind, [])
        if not isinstance(rules, list) or len(rules) > 32:
            raise ValueError("invalid_rules")
        for raw in rules:
            rule = {"pattern": raw} if isinstance(raw, str) and kind == "hard_exclusions" else raw
            if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str) or not rule["pattern"].strip():
                raise ValueError("invalid_rule")
            try:
                re.compile(rule["pattern"], re.I)
                if rule.get("exclude_pattern"):
                    re.compile(rule["exclude_pattern"], re.I)
            except (re.error, TypeError):
                raise ValueError("invalid_rule_pattern") from None
            fields = rule.get("fields", ["title", "description"])
            if not isinstance(fields, list) or not fields or any(f not in ("title", "description") for f in fields):
                raise ValueError("invalid_rule_fields")
            if kind == "rules" and (not _number(rule.get("ceiling")) or rule["ceiling"] <= 0):
                raise ValueError("invalid_rule_ceiling")
            if "reason" in rule and (not isinstance(rule["reason"], str) or not rule["reason"].strip()):
                raise ValueError("invalid_rule_reason")
            if "group" in rule and (not isinstance(rule["group"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", rule["group"])):
                raise ValueError("invalid_rule_group")
    if "defer_below_price" in criteria and (not _number(criteria["defer_below_price"]) or criteria["defer_below_price"] < 0):
        raise ValueError("invalid_defer_price")


def triage(item, profile):
    """Reject only explicit hard exclusions; all unknown configurations need analysis."""
    if item.get("availability") in ("sold", "unavailable"):
        return "rejected", "商品明确已售出或下架，不再作为在售候选"
    criteria = profile.get("criteria", {})
    if not isinstance(criteria, dict):
        return "needs_analysis", "筛选条件格式未知，保留待复评"
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
            fields = exclusion.get("fields", ["title", "description"]) if isinstance(exclusion, dict) else ["title", "description"]
            excluded = exclusion.get("exclude_pattern") if isinstance(exclusion, dict) else None
            re.compile(pattern, re.I)
            if excluded:
                re.compile(excluded, re.I)
            compiled.append(({"pattern": pattern, "fields": fields, "exclude_pattern": excluded}, reason))
        for key in ("min_price", "max_price"):
            if key in criteria and (not _number(criteria[key]) or criteria[key] < 0):
                raise ValueError("invalid price bound")
        if criteria.get("min_price", 0) > criteria.get("max_price", math.inf):
            raise ValueError("inverted price bounds")
    except (ValueError, TypeError, re.error):
        return "needs_analysis", "硬排除条件无效，保留待复评"
    for rule, reason in compiled:
        if matches(item, rule):
            return "rejected", reason or "命中已配置的明确硬排除条件"
    price = item.get("price")
    if not _number(price) or price < 0:
        return ("deferred" if "defer_below_price" in criteria else "needs_analysis"), "完整售价未知，需核实后比较"
    if "defer_below_price" in criteria and price < criteria["defer_below_price"]:
        return "deferred", "标价不足以代表完整交易价，保留待有效报价，不占AI配额"
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
            if matches(item, rule):
                position = "内" if price <= rule["ceiling"] else "外"
                return "needs_analysis", f"处于{rule.get('label', '已配置规则')}观察价{position}；仍需与基准按同口径比较"
        except (ValueError, TypeError, re.error):
            return "needs_analysis", "观察规则无效，保留待复评"
    return "needs_analysis", "现有规则不足以判断此商品，需AI复评并核对证据缺口"


def judge(item, profile):
    """Legacy tuple entry point, with the same conservative triage semantics."""
    return triage(item, profile)
