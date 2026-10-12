"""Read normal page traffic; explicitly authorized favorites use only the UI."""
import asyncio
import importlib.util
import math
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .contracts import CaptureBatch

SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
DETAIL_API = "mtop.taobao.idle.pc.detail"
LOGIN_API = "mtop.taobao.idlemessage.pc.loginuser.get"
NEXT_SELECTOR = "button[class*='search-pagination-arrow-container']:has([class*='search-pagination-arrow-right'])"


class CaptureError(RuntimeError):
    pass


def item_id_value(value):
    value = str(value)
    return value if re.fullmatch(r"[0-9]+", value) else None


def checked_payload(payload):
    if not isinstance(payload, dict):
        raise CaptureError("invalid_api_envelope")
    ret = payload.get("ret")
    if not isinstance(ret, list) or not all(isinstance(r, str) for r in ret):
        raise CaptureError("invalid_api_envelope")
    status = " ".join(ret).upper()
    if any(word in status for word in ("RGV", "BAXIA", "VALIDATE", "LOGIN", "SESSION_EXPIRED", "CAPTCHA", "CHALLENGE")):
        raise CaptureError("platform_boundary")
    if any(r.split("::", 1)[0].startswith("FAIL") for r in ret):
        raise CaptureError("page_api_failure")
    if not any(r.split("::", 1)[0] == "SUCCESS" for r in ret) or not isinstance(payload.get("data"), dict):
        raise CaptureError("invalid_api_envelope")
    return payload["data"]


def price_value(raw):
    if isinstance(raw, list):
        if any(not isinstance(p, dict) or not isinstance(p.get("text"), (str, int, float)) for p in raw):
            return None
        raw = "".join(str(p["text"]) for p in raw)
    text = str(raw).replace(",", "").replace("当前价", "").strip()
    match = re.fullmatch(r"[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)\s*(万)?\s*(?:元)?", text)
    if not match:
        return None
    price = float(match[1]) * (10000 if match[2] else 1)
    return price if math.isfinite(price) else None


def public_image(value):
    if not isinstance(value, str):
        raise CaptureError("image_schema_changed")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise CaptureError("image_schema_changed")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_search_checked(payload):
    """Return selected fields and pagination evidence, preserving unknown states."""
    data = checked_payload(payload)
    rows = data.get("resultList")
    if not isinstance(rows, list):
        raise CaptureError("search_schema_changed")
    next_values = [data[key] for key in ("nextPage", "hasNext") if key in data]
    if any(type(v) is not bool for v in next_values) or len(set(next_values)) > 1:
        raise CaptureError("search_pagination_schema_changed")
    has_next = next_values[0] if next_values else None
    result, skipped = [], 0
    for row in rows:
        if not isinstance(row, dict):
            raise CaptureError("search_card_schema_changed")
        if row.get("cardType") == "ad":
            skipped += 1
            continue
        row_data = row.get("data")
        row_item = row_data.get("item") if isinstance(row_data, dict) else None
        main = row_item.get("main") if isinstance(row_item, dict) else None
        if not isinstance(main, dict):
            raise CaptureError("search_card_schema_changed")
        # Only explicit advertising markers qualify. Unknown cards still fail.
        if main.get("isAd") is True:
            skipped += 1
            continue
        content = main.get("exContent")
        if not isinstance(content, dict):
            raise CaptureError("search_card_schema_changed")
        item_id = item_id_value(content.get("itemId", ""))
        if not item_id or not isinstance(content.get("title"), str) or not content["title"].strip():
            raise CaptureError("search_card_schema_changed")
        item = {"id": item_id, "platform": "goofish", "title": content["title"]}
        price = price_value(content.get("price"))
        if price is not None:
            item["price"] = price
        args = main.get("clickParam", {}).get("args", {}) if isinstance(main.get("clickParam", {}), dict) else None
        if not isinstance(args, dict):
            raise CaptureError("search_card_schema_changed")
        published = str(args.get("publishTime", ""))
        if re.fullmatch(r"[0-9]{1,16}", published):
            item["published_at"] = int(published) / 1000
        if content.get("picUrl"):
            item["thumbnail"] = public_image(content["picUrl"])
        result.append(item)
    if not rows and has_next is True:
        raise CaptureError("empty_search_with_next_page")
    return CaptureBatch(result, {"raw_cards": len(rows), "skipped_cards": skipped,
                                 "empty": not rows, "has_next": has_next})


def parse_search(payload):
    return parse_search_checked(payload).items


def parse_detail(payload, expected_id):
    expected_id = item_id_value(expected_id)
    if not expected_id:
        raise ValueError("invalid_item_id")
    data = checked_payload(payload)
    item = data.get("itemDO")
    if not isinstance(item, dict) or item_id_value(item.get("itemId", item.get("id", ""))) != expected_id:
        raise CaptureError("detail_identity_or_schema_changed")
    if "itemId" in item and "id" in item and item_id_value(item["id"]) != expected_id:
        raise CaptureError("detail_identity_or_schema_changed")
    result = {"id": expected_id, "platform": "goofish"}
    for source, target in (("desc", "description"), ("title", "title")):
        if item.get(source) is not None:
            if not isinstance(item[source], str):
                raise CaptureError("detail_field_schema_changed")
            result[target] = item[source]
    seller = data.get("sellerDO")
    if seller is not None:
        if not isinstance(seller, dict):
            raise CaptureError("detail_field_schema_changed")
        if seller.get("sellerId") is not None:
            seller_id = item_id_value(seller["sellerId"])
            if not seller_id:
                raise CaptureError("detail_field_schema_changed")
            result["seller_id"] = seller_id
    if item.get("imageInfos") is not None:
        images = item["imageInfos"]
        if not isinstance(images, list) or any(not isinstance(i, dict) or not i.get("url") for i in images):
            raise CaptureError("detail_image_schema_changed")
        result["images"] = [public_image(i["url"]) for i in images]
    price = price_value(item.get("soldPrice"))
    if price is None:
        price = price_value(item.get("price"))
    if price is not None:
        result["price"] = price
    if "itemStatusStr" in item:
        result["availability"] = {"在线": "available", "卖掉了": "sold", "已卖出": "sold",
            "已售出": "sold", "已下架": "unavailable", "下架": "unavailable",
            "已删除": "unavailable"}.get(str(item["itemStatusStr"]), "unknown")
    return result


def _public_payload(value, field=""):
    """Never hand cookies, authorization or token fields to downstream callers."""
    if isinstance(value, dict):
        return {k: _public_payload(v, str(k)) for k, v in value.items()
                if not any(word in str(k).lower() for word in ("cookie", "token", "authorization", "password"))}
    if isinstance(value, list):
        return [_public_payload(v, field) for v in value]
    if isinstance(value, str) and field.lower().endswith("url") and value:
        parts = urlsplit(value)
        if parts.username or parts.password:
            raise CaptureError("private_url_in_page_response")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return value


def _is_api_response(response, api=None):
    parts = urlsplit(response.url)
    if parts.scheme != "https" or parts.hostname != "h5api.m.goofish.com" or parts.username or parts.password or response.request.method != "POST":
        return False
    apis = (api,) if api else (SEARCH_API, DETAIL_API, LOGIN_API)
    return any(re.fullmatch(r"/h5/" + re.escape(name) + r"/[0-9.]+/?", parts.path) for name in apis)


class Browser:
    def __init__(self, reader_file, endpoint):
        endpoint_parts = urlsplit(endpoint)
        if endpoint_parts.scheme != "http" or endpoint_parts.hostname not in ("localhost", "127.0.0.1") or endpoint_parts.username or endpoint_parts.password:
            raise ValueError("cdp_must_be_local")
        spec = importlib.util.spec_from_file_location("cubi_browser_reader", reader_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.reader_type = module.Reader
        self.endpoint = endpoint
        self.responses = []
        self.reader = self.page = self.connected_browser = None

    def _capture_response(self, response):
        if _is_api_response(response):
            self.responses.append(response)
            del self.responses[:-100]

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        try:
            self.connected_browser = await self.playwright.chromium.connect_over_cdp(self.endpoint)
            self.reader = self.reader_type(self.connected_browser)
            await self.reader.open()
            self.page = await self.reader.background_page()
            self.page.on("response", self._capture_response)
            return self
        except BaseException:
            await self._close()
            raise

    async def _close(self):
        try:
            if self.page is not None:
                self.page.remove_listener("response", self._capture_response)
            self.responses.clear()
        finally:
            try:
                if self.reader is not None:
                    await self.reader.close()  # Reader closes its owned target and disconnects.
                elif self.connected_browser is not None:
                    await self.connected_browser.close()
            finally:
                await self.playwright.stop()

    async def __aexit__(self, *args):
        await self._close()

    async def navigate(self, url):
        self.responses.clear()
        try:
            await self.reader.navigate(self.page, url, SimpleNamespace(ready_selector=None, busy_selector=None))
        except Exception as exc:
            # Persistent telemetry can prevent global network idle. Only accept a
            # current, valid page API result after the same strict boundary checks.
            path = urlsplit(url).path
            api = DETAIL_API if path == "/item" else SEARCH_API if path == "/search" else None
            if str(exc) != "network_not_idle" or api is None:
                raise
            await self.boundary()
            payload = await self.payload(api)
            if api == SEARCH_API:
                parse_search_checked(payload)
            else:
                parse_detail(payload, parse_qs(urlsplit(url).query)["id"][0])
            self.settlement_fallbacks = getattr(self, "settlement_fallbacks", 0)+1
            await asyncio.sleep(2.2)
        await self.boundary()

    async def boundary(self):
        await self.reader.boundary_check(self.page)
        text = await self.page.locator("body").inner_text(timeout=5000)
        if any(t in text for t in ("拖动滑块", "请完成验证", "访问过于频繁", "扫码登录", "短信登录")) or "login" in urlsplit(self.page.url).path:
            raise CaptureError("visible_platform_boundary")

    async def payload(self, api):
        matches = [r for r in self.responses if _is_api_response(r, api)]
        if not matches:
            raise CaptureError("expected_page_response_missing")
        response = matches[-1]
        if response.status != 200:
            raise CaptureError("page_api_http_failure")
        try:
            payload = await response.json()
        except (ValueError, TypeError):
            raise CaptureError("invalid_api_json") from None
        return _public_payload(payload)

    async def settle_click(self, locator):
        await asyncio.sleep(2.5)
        self.responses.clear()
        async with self.page.expect_response(lambda r: _is_api_response(r, SEARCH_API), timeout=10000):
            await locator.click(timeout=5000)
        await self.page.wait_for_load_state("networkidle", timeout=10000)
        await asyncio.sleep(2.2)
        await self.boundary()

    async def _pagination(self, api_has_next, empty):
        if api_has_next is False or empty:
            return False, None
        locator = self.page.locator(NEXT_SELECTOR)
        count = await locator.count()
        if count > 1:
            raise CaptureError("search_pagination_selector_changed")
        if count == 0:
            if api_has_next is True:
                raise CaptureError("search_next_page_control_missing")
            return None, None
        enabled = await locator.is_enabled()
        if api_has_next is True and not enabled:
            raise CaptureError("search_next_page_control_disabled")
        return enabled, locator if enabled else None

    async def search(self, keyword, pages=1, sort="新发布", publish_window="3天内"):
        if type(pages) is not int or not 1 <= pages <= 2:
            raise ValueError("invalid_search_pages")
        if sort not in ("新发布", "新降价", None, ""):
            raise ValueError("invalid_search_sort")
        if sort == "新发布" and publish_window not in ("最新", "1天内", "3天内", "7天内", "14天内"):
            raise ValueError("invalid_publish_window")
        await self.navigate("https://www.goofish.com/search?" + urlencode({"q": keyword}))
        window = publish_window if sort == "新发布" else None
        if sort:
            menu = self.page.get_by_text(sort, exact=True)
            if await menu.count() != 1:
                raise CaptureError("search_sort_selector_changed")
            if sort == "新发布":
                # Opening the menu is not evidence that the desired filter was applied.
                await menu.click(timeout=5000)
                await self.boundary()
                option = self.page.get_by_text(window, exact=True)
                await option.wait_for(state="visible", timeout=5000)
                if await option.count() != 1:
                    raise CaptureError("search_filter_option_changed")
                await self.settle_click(option)
            else:
                # Native price-reduction sorting applies on a single click.
                await self.settle_click(menu)
        items = {}
        coverage = {"keyword": keyword, "filter": sort, "window": window, "pages": 0,
                    "requested_pages": pages, "bounded": True, "skipped_cards": 0,
                    "page_reports": [], "empty": True, "has_next": None, "truncated": False}
        for page_no in range(pages):
            batch = parse_search_checked(await self.payload(SEARCH_API))
            has_next, next_control = await self._pagination(batch.coverage["has_next"], batch.coverage["empty"])
            items.update((item["id"], item) for item in batch.items)
            report = {"page": page_no + 1, **batch.coverage, "has_next": has_next}
            coverage["page_reports"].append(report)
            coverage["pages"] += 1
            coverage["skipped_cards"] += report["skipped_cards"]
            coverage["empty"] = coverage["empty"] and report["empty"]
            coverage["has_next"] = has_next
            if has_next is not True or page_no + 1 == pages:
                coverage["truncated"] = has_next is not False
                if coverage["truncated"]:
                    coverage["stop_reason"] = "page_budget" if has_next else "pagination_unknown"
                break
            await self.settle_click(next_control)
        return CaptureBatch(list(items.values()), coverage)

    async def detail(self, item_id):
        item_id = item_id_value(item_id)
        if not item_id:
            raise ValueError("invalid_item_id")
        await self.navigate(f"https://www.goofish.com/item?id={item_id}")
        return parse_detail(await self.payload(DETAIL_API), item_id)

    async def favorite(self, item_id, account_id, guard, before_click, allow_click=True):
        """Add one precise listing through its UI; never toggle a saved listing off."""
        item_id = item_id_value(item_id)
        if not item_id or not item_id_value(account_id):
            raise ValueError("invalid_collection_identity")
        await self.detail(item_id)
        await self.boundary()

        async def verify():
            parts = urlsplit(self.page.url)
            if parts.scheme != "https" or parts.hostname != "www.goofish.com" or parts.path != "/item":
                raise CaptureError("collection_target_changed")
            current = checked_payload(await self.payload(LOGIN_API)).get("userId")
            if isinstance(current, bool) or item_id_value(current) != str(account_id):
                raise CaptureError("collection_account_mismatch")
            return parse_detail(await self.payload(DETAIL_API), item_id)

        fact = await verify()
        if not guard(fact) or fact.get("availability") in ("sold", "unavailable", "unknown"):
            return "stale"
        saved = self.page.get_by_text("已收藏", exact=True)
        add = self.page.get_by_text("收藏", exact=True)
        if await saved.count() == 1 and await add.count() == 0:
            return "already_saved"
        if await add.count() != 1 or await saved.count() != 0:
            raise CaptureError("collection_control_changed")
        if not allow_click:
            return "unconfirmed"
        await asyncio.sleep(2.2)
        await self.boundary()
        refreshed = await verify()
        if not guard(refreshed) or refreshed.get("availability") in ("sold", "unavailable", "unknown") or not before_click():
            return "stale"
        # A changed UI cannot turn this into an unfavorite click.
        if await saved.count() != 0 or await add.count() != 1:
            raise CaptureError("collection_control_changed")
        await add.click(timeout=5000)
        await asyncio.sleep(2.2)
        await self.boundary()
        await verify()
        if await saved.count() != 1 or await add.count() != 0:
            raise CaptureError("collection_result_unconfirmed")
        return "added"

    async def favorites(self, expected_ids):
        expected_ids = {item_id_value(value) for value in expected_ids}
        if None in expected_ids:
            raise ValueError("invalid_item_id")
        coverage = {"source": "favorites", "found": 0, "expected": len(expected_ids),
                    "missing": sorted(expected_ids), "pages": 0, "bounded": True,
                    "empty": True, "has_next": False, "truncated": False, "skipped_cards": 0,
                    "unresolved": [], "errors": []}
        if not expected_ids:
            return CaptureBatch([], coverage)
        await self.navigate("https://www.goofish.com/collection")
        # Filter identities in-page before reading text, and collect no other cards.
        cards = await self.page.locator('a[href*="/item?"]').evaluate_all("""(els, ids) => els.filter(e => {
            const u = new URL(e.href); return u.hostname === 'www.goofish.com' &&
                u.pathname === '/item' && ids.includes(u.searchParams.get('id'));
        }).map(e => ({href:e.href, current_prices:
            [...e.querySelectorAll('[class^="priceLeft--"]')].map(p => p.innerText)}))""", sorted(expected_ids))
        items, found, unresolved = {}, set(), set()
        for card in cards:
            if not isinstance(card, dict) or not isinstance(card.get("href"), str):
                coverage["skipped_cards"] += 1
                coverage["errors"].append({"reason": "favorite_card_schema_changed"})
                continue
            parts = urlsplit(card["href"])
            ids = parse_qs(parts.query).get("id", [])
            if parts.hostname != "www.goofish.com" or parts.path != "/item" or len(ids) != 1 or ids[0] not in expected_ids:
                continue
            item_id = ids[0]
            found.add(item_id)
            prices = card.get("current_prices")
            values = {price_value(price) for price in prices} if isinstance(prices, list) and all(isinstance(p, str) for p in prices) else set()
            if len(values) != 1 or None in values:
                unresolved.add(item_id)
                coverage["errors"].append({"id": item_id, "reason": "favorite_price_schema_changed"})
                continue
            item = {"id": item_id, "platform": "goofish", "price": values.pop()}
            if item_id in items and items[item_id] != item:
                unresolved.add(item_id)
                coverage["errors"].append({"id": item_id, "reason": "favorite_duplicate_price_conflict"})
                continue
            items[item_id] = item
        for item_id in unresolved:
            items.pop(item_id, None)
        coverage.update(found=len(found), missing=sorted(expected_ids - found), pages=1,
                        empty=not found, unresolved=sorted(unresolved),
                        truncated=bool(expected_ids - found or unresolved or coverage["skipped_cards"]))
        return CaptureBatch(list(items.values()), coverage)
