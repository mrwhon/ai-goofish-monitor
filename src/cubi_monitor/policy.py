import re
from src.keyword_rule_engine import normalize_text
from .ledger import digest


def criteria_id(profile):
    return digest(profile["criteria"])


def judge(item, profile):
    """Conservative shortlist triage. Never claims hardware authenticity or health."""
    text = normalize_text(item.get("title", "") + " " + item.get("description", ""))
    if item.get("availability") == "unavailable":
        return "reserve", "商品已下架或售出；留存历史，不解释为硬件缺陷"
    price = item.get("price")
    if price is None or price <= 100:
        return "reserve", "价格缺失或可能是占位/定金，需核实完整售价"
    if re.search(r"魔改|改显存|扩容显存|置换|只换不卖|交换不卖", text):
        return "reserve", "存在改装或置换表述，先核实原厂规格和是否现金出售"
    if re.search(r"9070\s*xt|7900\s*(?:xtx|xt)", text):
        return "reserve", "AMD独显路线不符合当前本地模型显卡偏好"
    for rule in profile["criteria"]["rules"]:
        if re.search(rule["pattern"], text, re.I):
            if price <= rule["ceiling"]:
                return "candidate", f"符合{rule['label']}观察价≤{rule['ceiling']}元；需确认是否整套售价、内存/显存、原厂规格、可送修、硬盘健康及整机稳定性；尚未证明比基准更划算"
            return "reserve", f"超过{rule['label']}当前观察价{rule['ceiling']}元；继续等待"
    return "reserve", "现有规则不足以判断此配置；保留待核实，不自动取消收藏"
