"""Read only public product fields from normal page traffic; no signed API calls."""
import asyncio
import importlib.util
import re
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit


class CaptureError(RuntimeError):
    pass


def checked_payload(payload):
    ret = str(payload.get("ret", []))
    if any(word in ret.upper() for word in ("FAIL", "RGV", "BAXIA", "VALIDATE", "LOGIN")):
        raise CaptureError("platform_boundary")
    if "SUCCESS" not in ret.upper() or not isinstance(payload.get("data"), dict):
        raise CaptureError("invalid_api_envelope")
    return payload["data"]


def price_value(raw):
    if isinstance(raw, list):
        raw = "".join(str(p.get("text", "")) for p in raw if isinstance(p, dict))
    text = str(raw).replace(",", "").replace("当前价", "").strip()
    match = re.fullmatch(r"[¥￥]?\s*(\d+(?:\.\d+)?)\s*(万)?", text)
    if not match:
        return None
    return float(match[1]) * (10000 if match[2] else 1)


def parse_search(payload):
    # Field path is adapted from upstream src/parsers.py at the pinned source ref.
    data = checked_payload(payload)
    if not isinstance(data.get("resultList"), list):
        raise CaptureError("search_schema_changed")
    result = []
    for row in data["resultList"]:
        main = row.get("data", {}).get("item", {}).get("main", {})
        content = main.get("exContent", {})
        item_id = str(content.get("itemId", ""))
        if not item_id.isdigit() or not content.get("title"):
            # An ad/unknown card is not an empty result and must remain observable.
            raise CaptureError("search_card_schema_changed")
        args = main.get("clickParam", {}).get("args", {})
        published = str(args.get("publishTime", ""))
        result.append({"id": item_id, "title": content["title"], "price": price_value(content.get("price")), "published_at": float(published) / 1000 if published.isdigit() else None, "thumbnail": str(content.get("picUrl", "")).split("?")[0]})
    return result


def parse_detail(payload, expected_id):
    data = checked_payload(payload)
    item = data.get("itemDO")
    if not isinstance(item, dict) or str(item.get("itemId", item.get("id", ""))) != str(expected_id):
        raise CaptureError("detail_identity_or_schema_changed")
    result = {"id": str(expected_id), "description": str(item.get("desc", "")), "seller_id": str(data.get("sellerDO", {}).get("sellerId", "")), "images": [str(i["url"]).split("?")[0] for i in item.get("imageInfos", []) if i.get("url")]}
    price = price_value(item.get("soldPrice", item.get("price")))
    if price is not None:
        result["price"] = price
    if item.get("title"):
        result["title"] = str(item["title"])
    return result


class Browser:
    def __init__(self, reader_file, endpoint):
        endpoint_parts = urlsplit(endpoint)
        if endpoint_parts.scheme != "http" or endpoint_parts.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("cdp_must_be_local")
        spec = importlib.util.spec_from_file_location("cubi_browser_reader", reader_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.reader_type = module.Reader
        self.endpoint = endpoint
        self.responses = []

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self.reader = None
        try:
            browser = await self.playwright.chromium.connect_over_cdp(self.endpoint)
            self.reader = self.reader_type(browser)
            await self.reader.open()
            self.page = await self.reader.background_page()
            self.page.on("response", lambda response: self.responses.append(response))
            return self
        except BaseException:
            if self.reader:
                await self.reader.close()
            await self.playwright.stop()
            raise

    async def __aexit__(self, *args):
        try:
            await self.reader.close()
        finally:
            await self.playwright.stop()

    async def navigate(self, url):
        self.responses.clear()
        await self.reader.navigate(self.page, url, SimpleNamespace(ready_selector=None, busy_selector=None))
        await self.boundary()

    async def boundary(self):
        await self.reader.boundary_check(self.page)
        text = await self.page.locator("body").inner_text(timeout=5000)
        if any(t in text for t in ("拖动滑块", "请完成验证", "访问过于频繁", "扫码登录", "短信登录")) or "login" in urlsplit(self.page.url).path:
            raise CaptureError("visible_platform_boundary")

    async def payload(self, api):
        matches = [r for r in self.responses if urlsplit(r.url).hostname == "h5api.m.goofish.com" and api in urlsplit(r.url).path]
        if not matches:
            raise CaptureError("expected_page_response_missing")
        response = matches[-1]
        if response.status != 200:
            raise CaptureError("page_api_http_failure")
        return await response.json()

    async def settle_click(self, locator):
        self.responses.clear()
        await asyncio.sleep(2.5)
        await locator.click(timeout=5000)
        await self.page.wait_for_load_state("networkidle", timeout=10000)
        await asyncio.sleep(2.2)
        await self.boundary()

    async def search(self, keyword, pages=2, sort="新发布", publish_window="3天内"):
        await self.navigate("https://www.goofish.com/search?" + urlencode({"q": keyword}))
        if sort:
            locator = self.page.get_by_text(sort, exact=True)
            if await locator.count() != 1:
                raise CaptureError("search_sort_selector_changed")
            await self.settle_click(locator)
            option = self.page.get_by_text(publish_window if sort == "新发布" else "最新", exact=True)
            if await option.count() != 1:
                raise CaptureError("search_filter_option_changed")
            await self.settle_click(option)
        items = []
        coverage = {"keyword": keyword, "filter": sort, "window": publish_window if sort == "新发布" else "最新", "pages": 0, "bounded": True}
        for page_no in range(pages):
            items.extend(parse_search(await self.payload("mtop.taobao.idlemtopsearch.pc.search")))
            coverage["pages"] += 1
            if page_no + 1 < pages:
                locator = self.page.locator("button[class*='search-pagination-arrow-container']:has([class*='search-pagination-arrow-right']):not([disabled])")
                if await locator.count() != 1 or not await locator.is_enabled():
                    break
                await self.settle_click(locator)
        return items, coverage

    async def detail(self, item_id):
        if not str(item_id).isdigit():
            raise ValueError("invalid_item_id")
        await self.navigate(f"https://www.goofish.com/item?id={item_id}")
        return parse_detail(await self.payload("mtop.taobao.idle.pc.detail"), item_id)

    async def favorites(self, expected_ids):
        await self.navigate("https://www.goofish.com/collection")
        cards = await self.page.locator('a[href*="/item?"]').evaluate_all("""els => els.map(e => ({href:e.href, text:e.innerText, image:e.querySelector('img')?.src}))""")
        items = []
        for card in cards:
            match = re.search(r"[?&]id=(\d+)", card["href"])
            if not match or match[1] not in expected_ids:
                continue
            text = card["text"].strip()
            price = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", text)
            if not price:
                raise CaptureError("favorite_price_schema_changed")
            items.append({"id": match[1], "price": float(price[1])})
        found = {i["id"] for i in items}
        return items, {"source": "favorites", "found": len(found), "expected": len(expected_ids), "missing": sorted(set(expected_ids) - found)}
