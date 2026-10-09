"""Parse buyer-side HTML and observed page JSON; never invent unavailable fields."""

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, NavigableString, Tag

from app.taobao import selectors
from app.sku_package import package_values


class InvalidSourceUrl(ValueError):
    pass


def _allowed_host(host: str) -> bool:
    return host in {"taobao.com", "tmall.com"} or host.endswith((".taobao.com", ".tmall.com"))


def canonical_product_url(url: str) -> tuple[str, str]:
    if not url or not url.strip():
        raise InvalidSourceUrl("商品 URL 不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or not _allowed_host(parsed.hostname or ""):
        raise InvalidSourceUrl("只接受淘宝或天猫前台商品 URL")
    product_id = parse_qs(parsed.query).get("id", [None])[0]
    if not product_id or not product_id.isdigit() or "item" not in parsed.path.lower():
        raise InvalidSourceUrl("URL 中缺少有效的商品 id")
    host = "detail.tmall.com" if "tmall.com" in (parsed.hostname or "") else "item.taobao.com"
    return f"https://{host}/item.htm?id={product_id}", product_id


def validate_shop_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or not _allowed_host(parsed.hostname or ""):
        raise InvalidSourceUrl("只接受淘宝或天猫前台店铺 URL")
    if "item" in parsed.path.lower() and parse_qs(parsed.query).get("id"):
        raise InvalidSourceUrl("请输入店铺 URL，不是商品 URL")
    from app.scraperapi_service import is_public_collection_url
    result = urlunparse(("https", parsed.netloc, parsed.path or "/", "", parsed.query, ""))
    if not is_public_collection_url(result):
        raise InvalidSourceUrl("只接受公开店铺首页或商品列表 URL")
    return result


def normalize_media_url(url: str | None, base_url: str) -> str | None:
    if not url or url.startswith(("data:", "blob:", "javascript:")):
        return None
    if url.startswith("//"):
        url = "https:" + url
    url = urljoin(base_url, url)
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return None
    if (parsed.hostname or "").endswith((".alicdn.com", ".taobaocdn.com")):
        original_path = re.sub(r"(\.(?:jpg|jpeg|png|webp|gif))_\d+x\d+[^/]*$", r"\1", parsed.path, flags=re.I)
        parsed = parsed._replace(path=original_path)
    return urlunparse(parsed)


def _first_text(soup: BeautifulSoup, css: tuple[str, ...]) -> str | None:
    for selector in css:
        node = soup.select_one(selector)
        if node:
            value = node.get_text(" ", strip=True)
            if value:
                return value
    return None


def _json_scripts(soup: BeautifulSoup) -> list[dict]:
    found = []
    decoder = json.JSONDecoder()
    for script in soup.find_all("script"):
        content = script.string or script.get_text()
        if not content:
            continue
        if script.get("type") == "application/ld+json":
            try:
                value = json.loads(content)
                found.extend(value if isinstance(value, list) else [value])
            except (ValueError, TypeError):
                pass
        for name in selectors.INITIAL_STATE_NAMES:
            match = re.search(r"(?:window\.)?" + re.escape(name) + r"\s*=\s*", content)
            if match:
                try:
                    value, _ = decoder.raw_decode(content[match.end():].lstrip())
                    if isinstance(value, dict):
                        found.append(value)
                except ValueError:
                    pass
        if "__ICE_APP_CONTEXT__" in content:
            match = re.search(r"var\s+b\s*=\s*", content)
            if match:
                try:
                    value, _ = decoder.raw_decode(content[match.end():].lstrip())
                    if isinstance(value, dict):
                        found.append(value)
                except ValueError:
                    pass
    return [item for item in found if isinstance(item, dict)]


def _walk(value, keys: set[str], depth=0):
    if depth > 12:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() == "ads" or any(word in key.lower() for word in ("recommend", "advert", "guess", "cart", "favorite")):
                continue
            if key.lower() in keys:
                yield child
            yield from _walk(child, keys, depth + 1)
    elif isinstance(value, list):
        for child in value[:200]:
            yield from _walk(child, keys, depth + 1)


def _text_value(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _price(value) -> str | None:
    if isinstance(value, dict):
        value = next((value[key] for key in ('price', 'value', 'priceText') if value.get(key) is not None and value.get(key) != ''), None)
    value = _text_value(value)
    if not value:
        return None
    if re.search(r'\d\s*[-~～至]\s*\d', value):
        return None  # A range is buyer display data, not one actual price.
    match = re.search(r"\d+(?:[,.]\d{1,2})?", value.replace(",", ""))
    if not match:
        return None
    try:
        return str(Decimal(match.group()).quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


def _urls(value, base_url: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("url") or item.get("src") or item.get("imageUrl")
        if isinstance(item, str):
            url = normalize_media_url(item, base_url)
            if url and url not in result:
                result.append(url)
    return result


@dataclass
class ParsedProduct:
    source_product_id: str
    source_url: str
    title: str
    price: str | None = None
    category: str | None = None
    attributes: dict = field(default_factory=dict)
    skus: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)  # Reads saved collection checkpoints from earlier versions.
    model: str | None = None
    specification: str | None = None
    raw_data: dict = field(default_factory=dict)
    detail_blocks: list[dict] = field(default_factory=list)
    category_data: dict = field(default_factory=dict)
    buyer_display: dict = field(default_factory=dict)


class TaobaoParser:
    def parse_shop_name(self, html: str) -> str | None:
        """Use shop-owned metadata; generic platform titles are not shop names."""
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        for item in _json_scripts(soup):
            seller = item.get("seller")
            if isinstance(seller, dict):
                candidates.extend((seller.get("shopName"), seller.get("sellerNick")))
            candidates.append(item.get("shopName"))
        for selector in ("[data-role='shop-name']", ".shop-name", "[class*='shopName']"):
            node = soup.select_one(selector)
            if node:
                candidates.append(node.get_text(" ", strip=True))
        for meta in soup.select("meta[property='og:site_name'], meta[name='shopName']"):
            candidates.append(meta.get("content"))
        if soup.title:
            title = soup.title.get_text(" ", strip=True)
            candidates.extend(part for part in re.split(r"[-_|｜]", title) if part)
        for value in candidates:
            name = re.sub(r"\s+", " ", _text_value(value) or "").strip()
            if (2 <= len(name) <= 100 and name not in {"首页", "全部商品", "所有商品", "商品列表", "店铺首页"}
                    and not re.fullmatch(r"(?:天猫|淘宝)(?:Tmall\.com|Taobao\.com)?", name, re.I)
                    and "tmall.com" not in name.lower() and "taobao.com" not in name.lower()):
                return name
        return None

    def parse_product(self, html: str, url: str, network_data: list[dict] | None = None,
                      *, detail_only: bool = False) -> ParsedProduct:
        canonical, product_id = canonical_product_url(url)
        soup = BeautifulSoup(html, "html.parser")
        scripts = _json_scripts(soup)
        # A gallery video is linked to this product, unlike unrelated page videos.
        product_videos = []
        for video in soup.select(".tb-gallery video, [class*='PicGallery'] video, [class*='mainPic'] video"):
            if any('recommend' in ' '.join(parent.get('class', [])).lower() for parent in video.parents if isinstance(parent, Tag)):
                continue
            candidate = video.get('src') or (video.find('source') or {}).get('src')
            media = normalize_media_url(candidate, canonical)
            if media:
                product_videos.append({'url':media, 'role':'product_video'})
        for excluded in soup.select(",".join(selectors.DETAIL_EXCLUDED)):
            excluded.decompose()
        data = [item for item in scripts + (network_data or []) if isinstance(item, dict)
                and all(str(value) == product_id for value in _walk(item, {'itemid', 'item_id', 'num_iid'})
                        if isinstance(value, (str, int)))]
        app_res = next((item.get("loaderData", {}).get("home", {}).get("data", {}).get("res")
                        for item in data if isinstance(item.get("loaderData"), dict)
                        and isinstance(item.get("loaderData", {}).get("home"), dict)
                        and isinstance(item.get("loaderData", {}).get("home", {}).get("data"), dict)
                        and isinstance(item.get("loaderData", {}).get("home", {}).get("data", {}).get("res"), dict)), None)
        item_data = app_res.get("item", {}) if app_res else {}
        product_nodes = [item for item in data if item.get("@type") == "Product"]
        title = _text_value(item_data.get("title"))
        title = title or next((_text_value(item.get("name")) for item in product_nodes if _text_value(item.get("name"))), None)
        title = title or _text_value((soup.find("meta", property="og:title") or {}).get("content"))
        title = title or _first_text(soup, selectors.PRODUCT_TITLE)
        if not title and soup.title:
            title = re.sub(r"[-_](?:tmall\.com)?天猫.*$", "", soup.title.get_text(strip=True), flags=re.I).strip()
        if not title:
            raise ValueError("未找到真实商品标题，需人工检查页面结构")
        price = _price((app_res or {}).get("skuCore", {}).get("sku2info", {}).get("0", {}).get("price"))
        for item in product_nodes:
            price = _price(item.get("offers"))
            if price:
                break
        if not price:
            meta = soup.find("meta", attrs={"property": "product:price:amount"})
            price = _price(meta.get("content")) if meta else None
        if not price:
            price = _price(_first_text(soup, selectors.PRODUCT_PRICE))
        images: list[dict] = []
        for media in _urls(item_data.get("images"), canonical):
            images.append({"url": media, "role": "main"})
        for item in product_nodes:
            for media in _urls(item.get("image"), canonical):
                images.append({"url": media, "role": "main"})
        meta_image = soup.find("meta", property="og:image")
        if meta_image:
            media = normalize_media_url(meta_image.get("content"), canonical)
            if media:
                images.append({"url": media, "role": "main"})
        if not item_data.get("images"):
            for selector in selectors.PRODUCT_GALLERY:
                for img in soup.select(selector):
                    media = normalize_media_url(img.get("data-original") or img.get("data-src") or img.get("data-lazy-src")
                                                or img.get("data-ks-lazyload") or img.get("src"), canonical)
                    if media:
                        images.append({"url": media, "role": "main"})
        images.extend(self.detail_images(BeautifulSoup(html, "html.parser"), canonical))
        images.extend(product_videos)
        # Optional explicitly named fields only; never infer purpose from image appearance.
        for key, role in {'whiteBackgroundImage':'white_background', 'sellPointImage':'selling_point',
                          'afterPurchaseVideo':'after_purchase_video'}.items():
            for media in _urls(item_data.get(key), canonical):
                images.append({'url':media, 'role':role})
        if not item_data.get("images"):
            for item in data:
                for candidate in _walk(item, {"images", "imageurls", "itemimages"}):
                    for media in _urls(candidate, canonical):
                        images.append({"url": media, "role": "main"})
        images = list({(item['url'], item.get('role')): item for item in images}.values())
        videos = []
        attributes = {}
        for selector in selectors.PRODUCT_EMPHASIS_ROWS:
            for row in soup.select(selector):
                value = row.select_one("[class*='emphasisParamsInfoItemTitle--']")
                name = row.select_one("[class*='emphasisParamsInfoItemSubTitle--']")
                if value and name and name.get_text(strip=True) and value.get_text(strip=True):
                    attributes[name.get_text(strip=True)] = value.get_text(strip=True)
        for selector in selectors.PRODUCT_PARAMETER_ROWS:
            for row in soup.select(selector):
                children = [child.get_text(" ", strip=True) for child in row.find_all(recursive=False)]
                if len(children) >= 2 and children[0] and children[1]:
                    attributes[children[0]] = children[1]
        for selector in selectors.PRODUCT_ATTRIBUTES:
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                if ":" in text or "：" in text:
                    key, value = re.split(r"[:：]", text, maxsplit=1)
                    attributes[key.strip()] = value.strip()
        skus = self._parse_skus(data, app_res)
        known_images = {item["url"] for item in images if item.get('role') == 'sku'}
        for sku in skus:
            sku_image = normalize_media_url(sku.get("image_url"), canonical)
            sku['image_url'] = sku_image
            if sku_image and sku_image not in known_images:
                images.append({"url": sku_image, "role": "sku"})
                known_images.add(sku_image)
        if detail_only:
            images = self.detail_images(soup, canonical)
        category = _text_value(item_data.get('categoryName')) or next((_text_value(value) for item in data for value in _walk(item, {"category"}) if _text_value(value)), None)
        category_path = next((value for item in data for value in _walk(item, {'categorypath', 'category_path'})
            if isinstance(value, list) and value and all(isinstance(name, str) and name.strip() for name in value)), [])
        if not category and category_path:
            category = category_path[-1]
        from app.preparation import allowed_name, sanitize_source_value
        attributes = {name:sanitize_source_value(value) for name,value in attributes.items() if allowed_name(name)}
        model = attributes.get("型号") or attributes.get("货号")
        specification = attributes.get("规格")
        result = ParsedProduct(product_id, canonical, title, price, category, attributes, skus,
                             images, videos, model, specification,
                             {"source_url": url, "structured_data": data, "attributes": attributes,
                             "images": images, "videos": videos, "sku": skus})
        result.detail_blocks = self.description_blocks(BeautifulSoup(html, 'html.parser'), canonical)
        result.category_data = {'name': category, 'path': category_path, 'source_category_id':
                                _text_value(item_data.get('categoryId'))}
        result.buyer_display = {'price': price, 'price_raw': _first_text(soup, selectors.PRODUCT_PRICE),
                                'skus': skus, 'source': 'public_product_page'}
        return result

    def description_blocks(self, soup: BeautifulSoup, url: str) -> list[dict]:
        for node in soup.select(','.join(selectors.DETAIL_EXCLUDED)):
            if node.parent is not None:
                node.decompose()
        roots = soup.select(','.join(selectors.PRODUCT_DETAIL_CONTAINERS))
        ids = {id(root) for root in roots}
        roots = [root for root in roots if not any(id(parent) in ids for parent in root.parents)]
        blocks = []
        for root in roots:
            for node in [root, *root.descendants]:
                if isinstance(node, NavigableString):
                    value = str(node).strip()
                    if value:
                        blocks.append({'type': 'text', 'text': value, 'order': len(blocks) + 1})
                elif isinstance(node, Tag) and (node.name in {'img', 'source'} or any(node.get(attr)
                        for attr in ('data-src', 'data-original', 'data-lazy-src', 'data-ks-lazyload', 'data-lazyload', 'data-srcset', 'srcset'))
                        or 'background' in node.get('style', '')):
                    clone = BeautifulSoup('<div data-role="product-description"></div>', 'html.parser')
                    # Only this node's resource; do not repeat descendant images here.
                    single = clone.new_tag(node.name, attrs=dict(node.attrs))
                    clone.div.append(single)
                    media = self.detail_images(clone, url)
                    for item in media:
                        blocks.append({'type': 'image', 'url': item['url'], 'order': len(blocks) + 1})
        return blocks

    def detail_images(self, soup: BeautifulSoup, url: str) -> list[dict]:
        """Read resources only inside recognized merchant description containers."""
        roots = soup.select(",".join(selectors.PRODUCT_DETAIL_CONTAINERS))
        excluded_ids = {id(node) for node in soup.select(",".join(selectors.DETAIL_EXCLUDED))}
        roots = [root for root in roots if not any(id(node) in excluded_ids for node in [root, *root.parents])]
        # Nested matches must not reorder resources or collect the same subtree twice.
        root_ids = {id(root) for root in roots}
        roots = [root for root in roots if not any(id(parent) in root_ids for parent in root.parents)]
        images, seen = [], set()
        for root in roots:
            for excluded in root.select(",".join(selectors.DETAIL_EXCLUDED)):
                if excluded.parent is not None:
                    excluded.decompose()
            for node in [root, *root.find_all(["img", "source", "div", "span", "p"] )]:
                candidates = []
                for key in ("data-original", "data-src", "data-lazy-src", "data-ks-lazyload", "data-lazyload"):
                    if node.get(key):
                        candidates.append(node.get(key))
                srcset = node.get("data-srcset") or node.get("srcset")
                if srcset:
                    sizes = []
                    for entry in srcset.split(","):
                        parts = entry.strip().split()
                        if parts:
                            try:
                                size = float(parts[1].rstrip("wx")) if len(parts) > 1 else 0
                            except ValueError:
                                size = 0
                            sizes.append((size, parts[0]))
                    if sizes:
                        candidates.append(max(sizes, key=lambda entry: entry[0])[1])
                if node.name == "img" and node.get("src"):
                    candidates.append(node.get("src"))
                candidates.extend(re.findall(r"background(?:-image)?\s*:[^;]*?url\(['\"]?([^)'\"]+)", node.get("style", ""), flags=re.I))
                for candidate in candidates:
                    media = normalize_media_url(candidate.strip(), url)
                    if not media:
                        continue
                    # Observed Alibaba CDN resize suffixes, preserving the original extension.
                    parsed = urlparse(media)
                    if (parsed.hostname or "").endswith((".alicdn.com", ".taobaocdn.com")):
                        path = re.sub(r"(\.(?:jpg|jpeg|png|webp|gif))_\d+x\d+[^/]*$", r"\1", parsed.path, flags=re.I)
                        media = urlunparse(parsed._replace(path=path))
                    if media not in seen:
                        seen.add(media)
                        images.append({"url": media, "role": "detail", "source": "detail_container",
                                       "order": len(images) + 1})
                    break
        return images

    def _parse_skus(self, data: list[dict], app_res: dict | None = None) -> list[dict]:
        result = []
        seen = set()
        def contexts(value, depth=0):
            if depth > 12:
                return
            if isinstance(value, dict):
                if isinstance(value.get('skuBase'), dict):
                    yield value
                for key, child in value.items():
                    if key.lower() == 'ads' or any(word in key.lower() for word in ('recommend', 'advert', 'guess', 'cart', 'favorite')):
                        continue
                    yield from contexts(child, depth + 1)
            elif isinstance(value, list):
                for child in value[:200]:
                    yield from contexts(child, depth + 1)
        for context in contexts([app_res, *data]):
            sku_base = context['skuBase']
            info = (context.get("skuCore") or {}).get("sku2info", {})
            props = {}
            for prop in sku_base.get("props") or []:
                pid = str(prop.get("pid", ""))
                for value in prop.get("values") or []:
                    props[(pid, str(value.get("vid", "")))] = (prop.get("name"), value.get("name"), value.get("image"))
            for sku in sku_base.get("skus") or []:
                sku_id = str(sku.get("skuId", ""))
                if not sku_id or sku_id in seen:
                    continue
                seen.add(sku_id)
                values = {}
                image_url = None
                for path in str(sku.get("propPath", "")).split(";"):
                    if ":" not in path:
                        continue
                    pid, vid = path.split(":", 1)
                    prop = props.get((pid, vid))
                    if prop:
                        values[prop[0] or pid] = prop[1] or vid
                        image_url = image_url or prop[2]
                sku_info = info.get(sku_id, {})
                result.append({"source_sku_id": sku_id, "name": " / ".join(str(value) for value in values.values()) or None,
                               "properties": values,
                               "price": _price(sku_info.get("price")),
                               "price_raw": sku_info.get('price'),
                               "stock": sku_info.get("quantity"), "image_url": image_url})
        for item in data:
            for value in _walk(item, {"skus", "skumap", "skulist"}):
                entries = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    sku_id = _text_value(entry.get("skuId") or entry.get("sku_id") or entry.get("id"))
                    if not sku_id or sku_id in seen:
                        continue
                    seen.add(sku_id)
                    result.append({"source_sku_id": sku_id,
                                   "name": _text_value(entry.get("name") or entry.get("skuName")),
                                   "properties": entry.get("properties") or entry.get("props") or {},
                                   "price": _price(entry.get("price") if entry.get('price') is not None else entry.get("priceText")),
                                   "price_raw": entry.get('price') if entry.get('price') is not None else entry.get('priceText'),
                                   "stock": entry.get('quantity') if entry.get('quantity') is not None else entry.get('stock'),
                                   "image_url": entry.get("image") or entry.get("imageUrl")})
        for sku in result:
            sku.update(package_values(sku.get('name'), sku.get('properties')))
            sku['package_price'] = sku.get('price')
        return result[:500]

    def discover_products(self, html: str, shop_url: str,
                          network_data: list[dict] | None = None) -> list[str]:
        validate_shop_url(shop_url)
        soup = BeautifulSoup(html, "html.parser")
        result = []
        for node in soup.select(",".join(selectors.DETAIL_EXCLUDED)):
            if node.name not in {"script", "style"} and node.parent is not None:
                node.decompose()
        for selector in selectors.SHOP_PRODUCT_LINKS:
            for link in soup.select(selector):
                try:
                    canonical, _ = canonical_product_url(urljoin(shop_url, link.get("href", "")))
                except InvalidSourceUrl:
                    continue
                if canonical not in result:
                    result.append(canonical)
        def visit(value, depth=0, catalog=False):
            if depth > 20:
                return
            if isinstance(value, dict):
                for key, child in value.items():
                    if any(word in key.lower() for word in ("recommend", "guess", "advert", "cart", "favorite")):
                        continue
                    if catalog and key.lower() in {"itemid", "item_id", "auctionid", "auction_id"} and str(child).isdigit():
                        host = "detail.tmall.com" if "tmall.com" in (urlparse(shop_url).hostname or "") else "item.taobao.com"
                        candidate = f"https://{host}/item.htm?id={child}"
                        if candidate not in result:
                            result.append(candidate)
                    elif catalog and isinstance(child, str) and "item.htm" in child and "id=" in child:
                        try:
                            candidate, _ = canonical_product_url(urljoin(shop_url, child))
                            if candidate not in result:
                                result.append(candidate)
                        except InvalidSourceUrl:
                            pass
                    visit(child, depth + 1, catalog or key.lower() in {
                        "items", "itemlist", "item_list", "auctions", "auctionlist", "products", "itemlistelement", "iteminfodto"})
            elif isinstance(value, list):
                for child in value:
                    visit(child, depth + 1, catalog)
        for payload in _json_scripts(soup) + (network_data or []):
            request_url = str(payload.get("request_url", "")).lower()
            if any(word in request_url for word in ("cart", "recommend", "favorite", "guess")):
                continue
            visit(payload)
            if "mtop.taobao.shop.simple.item.fetch" in urlparse(request_url).path:
                envelope = payload.get("data", {})
                data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
                # The modern catalog is data.data, unlike legacy itemList payloads.
                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    visit(data["data"], catalog=True)
        unique = []
        seen_ids = set()
        for candidate in result:
            _, product_id = canonical_product_url(candidate)
            if product_id not in seen_ids:
                seen_ids.add(product_id)
                unique.append(candidate)
        return unique
