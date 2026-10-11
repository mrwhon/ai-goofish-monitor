"""Synthetic public-field fixtures and fake pages; never connect a live browser."""
import asyncio
import copy
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from src.cubi_monitor.browser import (
    Browser, CaptureError, DETAIL_API, SEARCH_API, NEXT_SELECTOR,
    checked_payload, parse_detail, parse_search, parse_search_checked, price_value,
)
from src.cubi_monitor.contracts import CaptureBatch

FIXTURES = Path(__file__).parent / "fixtures" / "browser"


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def envelope(rows=None, **metadata):
    return {"ret": ["SUCCESS"], "data": {"resultList": rows or [], **metadata}}


def listing(item_id="123"):
    return {"data": {"item": {"main": {"exContent": {"itemId": item_id, "title": "测试商品"}}}}}


@pytest.mark.parametrize("value,expected", [
    ("¥1.05万", 10500), ("￥ 12,345.6 元", 12345.6), (0, 0),
    ([{"text": "当前价"}, {"text": "¥"}, {"text": "1.2"}, {"text": "万"}], 12000),
    (None, None), ("定金100元", None), ("面议", None), (True, None),
    ("-100", None), ("NaN", None), ("9" * 400, None), (["¥100"], None),
])
def test_prices(value, expected):
    assert price_value(value) == expected


def test_search_selected_fields_and_ads():
    batch = parse_search_checked(fixture("search"))
    assert isinstance(batch, CaptureBatch)
    assert batch.items[0] == {"id": "123", "platform": "goofish", "title": "离线测试电脑",
                              "price": 10500, "published_at": 1, "thumbnail": "https://example.test/image.jpg"}
    assert "price" not in batch.items[1]
    assert all("description" not in item and "images" not in item for item in batch.items)
    assert batch.coverage == {"raw_cards": 3, "skipped_cards": 1, "empty": False, "has_next": False}
    assert parse_search(fixture("search")) == batch.items


@pytest.mark.parametrize("payload,error", [
    (None, "invalid_api_envelope"), ({"ret": "SUCCESS", "data": {}}, "invalid_api_envelope"),
    ({"ret": ["NOT_SUCCESS"], "data": {}}, "invalid_api_envelope"),
    ({"ret": ["FAIL_SYS_USER_VALIDATE"], "data": {}}, "platform_boundary"),
    ({"ret": ["FAIL_SYS_SESSION_EXPIRED::LOGIN"], "data": {}}, "platform_boundary"),
    ({"ret": ["FAIL_SYS_SERVICE_UNAVAILABLE"], "data": {}}, "page_api_failure"),
    ({"ret": ["SUCCESS"], "data": {}}, "search_schema_changed"),
    (envelope([{}]), "search_card_schema_changed"),
    (envelope([{"data": {"item": None}}]), "search_card_schema_changed"),
    (envelope([{"data": {"item": {"main": {"exContent": {"itemId": "１２３", "title": "x"}}}}}]), "search_card_schema_changed"),
    (envelope(nextPage=True), "empty_search_with_next_page"),
    (envelope(nextPage="false"), "search_pagination_schema_changed"),
    (envelope(nextPage=True, hasNext=False), "search_pagination_schema_changed"),
])
def test_bad_search_is_never_successful_empty(payload, error):
    with pytest.raises(CaptureError, match=error):
        parse_search_checked(payload)


def test_true_empty_and_ad_only_are_distinct():
    assert parse_search_checked(envelope()).coverage["empty"] is True
    batch = parse_search_checked(envelope([{"cardType": "ad"}], nextPage=False))
    assert batch.items == [] and batch.coverage["empty"] is False
    assert batch.coverage["skipped_cards"] == 1


def test_detail_partial_fields_do_not_erase_evidence():
    assert parse_detail({"ret": ["SUCCESS"], "data": {"itemDO": {"itemId": "123"}}}, "123") == {"id": "123", "platform": "goofish"}
    payload = fixture("detail")
    item = parse_detail(payload, "123")
    assert item == {"id": "123", "platform": "goofish", "description": "测试详情", "price": 12000,
                    "images": ["https://example.test/detail.jpg"], "seller_id": "987"}
    payload["data"]["itemDO"]["desc"] = ""
    payload["data"]["itemDO"]["imageInfos"] = []
    assert parse_detail(payload, "123")["description"] == ""
    assert parse_detail(payload, "123")["images"] == []


@pytest.mark.parametrize("fields", [
    {"itemId": "124"}, {"itemId": "123", "id": "124"},
    {"itemId": "123", "desc": {}}, {"itemId": "123", "imageInfos": [{}]},
    {"itemId": "123", "imageInfos": "bad"},
])
def test_detail_identity_and_schema_fail(fields):
    with pytest.raises(CaptureError):
        parse_detail({"ret": ["SUCCESS"], "data": {"itemDO": fields}}, "123")


class Response:
    def __init__(self, payload=None, api=SEARCH_API, method="POST", host="h5api.m.goofish.com", status=200, path=None):
        self.url = f"https://{host}{path or '/h5/' + api + '/1.0/'}?token=not_read"
        self.request = SimpleNamespace(method=method)
        self.status = status
        self.body = payload
        self.reads = 0

    async def json(self):
        self.reads += 1
        if isinstance(self.body, Exception):
            raise self.body
        return copy.deepcopy(self.body)


class Locator:
    def __init__(self, page, name):
        self.page, self.name = page, name

    async def count(self):
        if self.name == NEXT_SELECTOR:
            return self.page.next_count
        if self.name in ("3天内", "最新"):
            return int(self.page.menu_open) * self.page.option_count
        return 1

    async def is_enabled(self):
        return self.page.next_enabled

    async def inner_text(self, **kwargs):
        return self.page.body

    async def wait_for(self, **kwargs):
        assert self.page.menu_open, "option was queried before the menu opened"

    async def click(self, **kwargs):
        self.page.clicks.append(self.name)
        if self.name == "新发布":
            self.page.menu_open = True
        elif self.name == "新降价":
            self.page.browser.responses = [Response(self.page.discount_payload or self.page.payloads[self.page.index])]
        elif self.name == NEXT_SELECTOR:
            self.page.index += 1
            self.page.browser.responses = [Response(self.page.payloads[self.page.index])]
        else:
            self.page.browser.responses = [Response(self.page.payloads[self.page.index])]

    async def evaluate_all(self, javascript, ids):
        self.page.expected_ids_read = ids
        assert "ids.includes" in javascript
        assert "image" not in javascript and "cookie" not in javascript
        return self.page.cards


class Page:
    def __init__(self, payloads=None):
        self.payloads = payloads or [envelope(nextPage=False)]
        self.index = 0
        self.url = "about:blank"
        self.body = "正常内容"
        self.next_count, self.next_enabled = 1, True
        self.option_count = 1
        self.menu_open = False
        self.discount_payload = None
        self.matched_responses = []
        self.clicks, self.listeners, self.cards = [], {}, []
        self.expected_ids_read = None

    def locator(self, selector):
        return Locator(self, selector)

    def get_by_text(self, text, exact):
        assert exact
        return Locator(self, text)

    def expect_response(self, predicate, timeout):
        page = self
        class Expectation:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                matches = [response for response in page.browser.responses if predicate(response)]
                if not matches:
                    raise asyncio.TimeoutError("expected response missing")
                page.matched_responses.extend(matches)
        return Expectation()

    async def wait_for_load_state(self, *args, **kwargs):
        pass

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        assert self.listeners.pop(event) == callback


class Reader:
    def __init__(self, page):
        self.page = page
        self.boundary_calls = 0

    async def navigate(self, page, url, args):
        assert page is self.page and args.ready_selector is None and args.busy_selector is None
        page.url = url
        page.browser.responses = [Response(page.payloads[page.index])]

    async def boundary_check(self, page):
        assert page is self.page
        self.boundary_calls += 1


def fake_browser(payloads=None):
    browser = Browser.__new__(Browser)
    browser.responses = []
    browser.page = Page(payloads)
    browser.page.browser = browser
    browser.reader = Reader(browser.page)
    return browser


@pytest.mark.parametrize("fault", [None, "missing_api", "bad_api", "challenge", "different_error"])
def test_network_idle_fallback_requires_current_valid_api_and_safe_page(fault):
    browser = fake_browser()
    async def navigate(page, url, args):
        page.url = url
        if fault != "missing_api":
            browser.responses = [Response(envelope([listing()], nextPage=False) if fault != "bad_api" else {"ret": ["ERROR"]})]
        if fault == "challenge":
            page.body = "请完成验证"
        raise RuntimeError("different_error" if fault == "different_error" else "network_not_idle")
    browser.reader.navigate = navigate
    if fault is None:
        asyncio.run(browser.navigate("https://www.goofish.com/search?q=synthetic"))
        assert browser.settlement_fallbacks == 1 and browser.reader.boundary_calls == 2
    else:
        with pytest.raises((RuntimeError, CaptureError)):
            asyncio.run(browser.navigate("https://www.goofish.com/search?q=synthetic"))


@pytest.fixture(autouse=True)
def fast_waits(monkeypatch):
    async def no_sleep(seconds):
        pass
    monkeypatch.setattr("src.cubi_monitor.browser.asyncio.sleep", no_sleep)


@pytest.mark.parametrize("sort,window,clicks", [
    ("新发布", "3天内", ["新发布", "3天内"]),
    ("新发布", "最新", ["新发布", "最新"]),
])
def test_filter_menu_opens_before_selecting_option(sort, window, clicks):
    browser = fake_browser([fixture("search")])
    result = asyncio.run(browser.search("离线词", pages=1, sort=sort, publish_window=window))
    assert isinstance(result, CaptureBatch) and len(result.items) == 2
    assert browser.page.clicks == clicks
    assert result.coverage["skipped_cards"] == 1
    assert result.coverage["pages"] == 1 and result.coverage["has_next"] is False
    assert result.coverage["truncated"] is False
    assert browser.reader.boundary_calls == 3


def test_price_reduction_is_direct_sort_without_publish_dropdown():
    browser = fake_browser([fixture("search")])
    browser.page.discount_payload = envelope([listing("789")], nextPage=False)
    result = asyncio.run(browser.search("离线词", pages=1, sort="新降价", publish_window="3天内"))
    assert browser.page.clicks == ["新降价"]
    assert browser.page.menu_open is False
    assert asyncio.run(browser.page.get_by_text("最新", exact=True).count()) == 0
    assert [item["id"] for item in result.items] == ["789"]
    assert result.coverage["filter"] == "新降价" and result.coverage["window"] is None
    assert len(browser.page.matched_responses) == 1
    response = browser.page.matched_responses[0]
    assert response.request.method == "POST"
    assert f"/h5/{SEARCH_API}/1.0/" in response.url
    assert browser.reader.boundary_calls == 2


def test_filter_click_cannot_reuse_response_captured_before_click(monkeypatch):
    browser = fake_browser()
    async def stale_response_during_wait(seconds):
        browser.responses.append(Response(envelope([listing()], nextPage=False)))
    async def click_without_request(**kwargs):
        pass
    monkeypatch.setattr("src.cubi_monitor.browser.asyncio.sleep", stale_response_during_wait)
    control = Locator(browser.page, "3天内")
    control.click = click_without_request
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(browser.settle_click(control))
    assert browser.responses == []


def test_missing_filter_option_fails():
    browser = fake_browser()
    browser.page.option_count = 0
    with pytest.raises(CaptureError, match="search_filter_option_changed"):
        asyncio.run(browser.search("离线词"))


@pytest.mark.parametrize("has_next,pages,expected_pages,truncated", [
    (False, 2, 1, False), (True, 1, 1, True), (True, 2, 2, False),
])
def test_page_budget_and_end_reported(has_next, pages, expected_pages, truncated):
    browser = fake_browser([envelope([listing()], nextPage=has_next), envelope([listing("456")], nextPage=False)])
    result = asyncio.run(browser.search("离线词", pages=pages, sort=None))
    assert result.coverage["pages"] == expected_pages
    assert result.coverage["truncated"] is truncated
    assert len(result.coverage["page_reports"]) == expected_pages
    assert browser.page.clicks == ([NEXT_SELECTOR] if expected_pages == 2 else [])


def test_unknown_pagination_is_conservative_and_dom_end_is_known():
    browser = fake_browser([envelope([listing()])])
    browser.page.next_count = 0
    coverage = asyncio.run(browser.search("词", sort=None)).coverage
    assert coverage["has_next"] is None and coverage["truncated"] is True
    assert coverage["stop_reason"] == "pagination_unknown"
    browser = fake_browser([envelope([listing()])])
    browser.page.next_enabled = False
    coverage = asyncio.run(browser.search("词", sort=None)).coverage
    assert coverage["has_next"] is False and coverage["truncated"] is False


@pytest.mark.parametrize("count,enabled", [(0, True), (1, False), (2, True)])
def test_next_api_without_usable_control_fails(count, enabled):
    browser = fake_browser([envelope([listing()], nextPage=True)])
    browser.page.next_count, browser.page.next_enabled = count, enabled
    with pytest.raises(CaptureError, match="search_.*page|search_pagination"):
        asyncio.run(browser.search("词", sort=None))


def test_empty_search_is_successful_known_end():
    browser = fake_browser([envelope()])
    result = asyncio.run(browser.search("词", sort=None))
    assert result.items == [] and result.coverage["empty"] is True
    assert result.coverage["pages"] == 1 and result.coverage["has_next"] is False
    assert result.coverage["truncated"] is False


@pytest.mark.parametrize("body,url", [("扫码登录", "https://www.goofish.com/search"), ("拖动滑块", "https://www.goofish.com/search"), ("正常", "https://www.goofish.com/login")])
def test_visible_boundaries_stop(body, url):
    browser = fake_browser()
    browser.page.body, browser.page.url = body, url
    with pytest.raises(CaptureError, match="visible_platform_boundary"):
        asyncio.run(browser.boundary())


def test_payload_filters_method_host_and_exact_api_and_redacts_private_fields():
    browser = fake_browser()
    good = Response(fixture("detail"), api=DETAIL_API)
    bad = [Response({}, method="GET"), Response({}, host="other.example"),
           Response({}, path="/h5/" + SEARCH_API + ".evil/1.0/"), Response({}, api="unrelated.api")]
    for response in [good, *bad]:
        browser._capture_response(response)
    assert browser.responses == [good]
    payload = asyncio.run(browser.payload(DETAIL_API))
    assert "cookie" not in payload["data"] and "accessToken" not in payload["data"]
    assert "token" not in payload["data"]["itemDO"]["imageInfos"][0]["url"]
    assert all(response.reads == 0 for response in bad)
    assert parse_detail(payload, "123")["price"] == 12000


@pytest.mark.parametrize("response,error", [
    (None, "expected_page_response_missing"),
    (Response({}, status=403), "page_api_http_failure"),
    (Response(ValueError("sensitive raw data")), "invalid_api_json"),
])
def test_payload_failures(response, error):
    browser = fake_browser()
    browser.responses = [response] if response else []
    with pytest.raises(CaptureError, match=error):
        asyncio.run(browser.payload(SEARCH_API))


def test_empty_favorites_do_not_navigate():
    browser = fake_browser()
    result = asyncio.run(browser.favorites([]))
    assert isinstance(result, CaptureBatch) and result.items == []
    assert result.coverage["pages"] == 0 and result.coverage["missing"] == []
    assert browser.page.url == "about:blank"


def test_favorites_reads_only_targets_and_handles_wan_units():
    browser = fake_browser()
    browser.page.cards = [
        {"href": "https://www.goofish.com/item?id=123", "text": "离线电脑 ¥1.05万"},
        {"href": "https://www.goofish.com/item?id=999", "text": "无关：坏价格"},
        {"href": "https://other.example/item?id=456", "text": "坏价格"},
    ]
    result = asyncio.run(browser.favorites(["123", "456"]))
    assert result.items == [{"id": "123", "platform": "goofish", "price": 10500}]
    assert browser.page.expected_ids_read == ["123", "456"]
    assert result.coverage["found"] == 1 and result.coverage["missing"] == ["456"]
    assert result.coverage["truncated"] is True


@pytest.mark.parametrize("texts", [["价格待议"], ["¥100 原价¥200"], ["¥100", "¥200"]])
def test_unknown_or_conflicting_favorite_prices_fail(texts):
    browser = fake_browser()
    browser.page.cards = [{"href": "https://www.goofish.com/item?id=123", "text": text} for text in texts]
    with pytest.raises(CaptureError, match="favorite_.*price"):
        asyncio.run(browser.favorites(["123"]))


def test_detail_uses_identity_and_target_api():
    browser = fake_browser()
    async def navigate(url):
        browser.page.url = url
        browser.responses = [Response(fixture("detail"), api=DETAIL_API)]
    browser.navigate = navigate
    assert asyncio.run(browser.detail("123"))["description"] == "测试详情"
    assert browser.page.url.endswith("?id=123")
    with pytest.raises(ValueError, match="invalid_item_id"):
        asyncio.run(browser.detail("../123"))


def install_fake_playwright(monkeypatch, failure=None, cleanup_failure=False):
    events, page = [], Page()
    class OwnedReader:
        def __init__(self, browser):
            self.browser = browser
            if failure == "constructor":
                raise CaptureError("constructor_failure")
        async def open(self):
            events.append("reader_open")
            if failure == "open":
                raise CaptureError("open_failure")
        async def background_page(self):
            events.append("create_owned_target")
            if failure == "target":
                raise CaptureError("target_failure")
            return page
        async def close(self):
            events.extend(["close_owned_target", "disconnect"])
            if cleanup_failure:
                raise CaptureError("cleanup_failure")
    class Connection:
        async def close(self):
            events.append("disconnect")
    class Chromium:
        async def connect_over_cdp(self, endpoint):
            events.append("connect")
            if failure == "connect":
                raise CaptureError("connect_failure")
            return Connection()
    class Playwright:
        chromium = Chromium()
        async def stop(self):
            events.append("stop")
    class Manager:
        async def start(self):
            events.append("start")
            return Playwright()
    module = ModuleType("playwright.async_api")
    module.async_playwright = Manager
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    browser = Browser.__new__(Browser)
    browser.reader_type, browser.endpoint = OwnedReader, "http://127.0.0.1:9222"
    browser.reader = browser.page = browser.connected_browser = None
    browser.responses = []
    return browser, page, events


def test_context_borrows_reader_and_closes_owned_target_on_body_failure(monkeypatch):
    browser, page, events = install_fake_playwright(monkeypatch)
    async def run():
        async with browser:
            assert "response" in page.listeners
            raise CaptureError("body_failure")
    with pytest.raises(CaptureError, match="body_failure"):
        asyncio.run(run())
    assert page.listeners == {} and browser.responses == []
    assert events == ["start", "connect", "reader_open", "create_owned_target", "close_owned_target", "disconnect", "stop"]


@pytest.mark.parametrize("failure,closes", [("connect", False), ("constructor", False), ("open", True), ("target", True)])
def test_context_entry_failure_cleans_up(monkeypatch, failure, closes):
    browser, page, events = install_fake_playwright(monkeypatch, failure=failure)
    with pytest.raises(CaptureError, match=failure + "_failure"):
        asyncio.run(browser.__aenter__())
    assert events[-1] == "stop"
    assert ("close_owned_target" in events) is closes
    assert page.listeners == {}
    if failure == "constructor":
        assert events[-2:] == ["disconnect", "stop"]


def test_stop_runs_even_if_reader_cleanup_fails(monkeypatch):
    browser, page, events = install_fake_playwright(monkeypatch, failure="open", cleanup_failure=True)
    with pytest.raises(CaptureError, match="cleanup_failure"):
        asyncio.run(browser.__aenter__())
    assert events[-1] == "stop"


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1:9222", "http://remote.example:9222", "http://user:secret@127.0.0.1:9222"])
def test_remote_or_credentialed_cdp_rejected_before_reader_import(endpoint):
    with pytest.raises(ValueError, match="cdp_must_be_local"):
        Browser("does-not-exist", endpoint)
