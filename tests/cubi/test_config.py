import pytest
from src.cubi_monitor.config import validate_config


def example():
    return {"schema": 2, "runtime": {}, "monitors": [
        {"monitor_id": "camera", "queries": [{"keyword": "相机"}], "criteria": {"goal": "旅行拍照"}},
        {"monitor_id": "furniture", "queries": [{"keyword": "书桌"}], "criteria": {"goal": "小书房"}}]}


def test_two_product_categories():
    result = validate_config(example())
    assert result["monitors"][0]["duration_days"] == 14
    assert result["monitors"][1]["analysis_budget"]["calls"] == 3


def test_duplicate_monitor_and_detail_budget_rejected():
    config = example()
    config["monitors"][1]["monitor_id"] = "camera"
    with pytest.raises(ValueError, match="monitor_id"):
        validate_config(config)
    config = example()
    config["monitors"][0]["scan_budget"] = {"details": 1}
    with pytest.raises(ValueError, match="details_less"):
        validate_config(config)
