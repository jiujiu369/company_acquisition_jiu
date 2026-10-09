"""All page selectors and visible verification signals live here."""

PRODUCT_TITLE = (
    "h1[data-spm]", ".tb-main-title", ".ItemTitle--mainTitle", "h1",
)
PRODUCT_PRICE = (
    "[class*='Price--priceText']", ".tb-rmb-num", "[itemprop='price']",
)
PRODUCT_GALLERY = (
    "[class*='PicGallery'] img", "[class*='Thumbnail'] img", ".tb-gallery img",
    "[class*='mainPic'] img", "[class*='MainPic'] img",
)
PRODUCT_DETAIL_IMAGES = (".descV8-singleImage img",)
PRODUCT_DETAIL_CONTAINERS = (
    "#J_DivItemDesc", "#J_ItemDesc", "#description .content", "#description .desc",
    ".descV8-container", ".descV8-singleImage", "[data-role='product-description']",
)
DETAIL_EXCLUDED = (
    "script", "style", "video", ".recommend", "[class*='recommend']", "[class*='Recommend']",
    "[class*='price-explanation']", "[class*='priceExplanation']", ".platform-rules",
    ".shop-logo", ".shop-banner", "[data-role='recommendation']",
)
SHOP_ALL_PRODUCTS = ("a:has-text('全部商品')", "a:has-text('所有商品')", "a:has-text('全部宝贝')", "a:has-text('所有宝贝')")
SHOP_NEXT_PAGE = (
    "a.next", "a.J_NextPage", "a:has-text('下一页')", "button:has-text('下一页')",
    "button[aria-label='下一页']", "a[aria-label='下一页']",
)
SHOP_LOAD_MORE = ("button:has-text('加载更多')", "a:has-text('加载更多')")
PRODUCT_PARAMETER_ROWS = ("[class*='generalParamsInfoItem--']",)
PRODUCT_EMPHASIS_ROWS = ("[class*='emphasisParamsInfoItem--']",)
PRODUCT_ATTRIBUTES = (
    "[class*='Attribute'] li", ".attributes-list li", ".tb-property-type li",
)
PRODUCT_SKU = (
    "[class*='Sku'] [data-value]", "[class*='sku'] [data-value]", ".J_Prop li",
)
SHOP_PRODUCT_LINKS = (
    "a[href*='item.htm?id=']", "a[href*='detail.tmall.com/item']",
    "a[href*='item.taobao.com/item']",
)
SHOP_CATALOG_APIS = (
    "mtop.taobao.shop.simple.item.fetch", "mtop.taobao.shop.simple.fetch",
)
LOGIN_URL_PARTS = ("login.taobao.com", "passport.taobao.com", "login.tmall.com")
MANUAL_TEXT = (
    "验证码", "滑动验证", "请拖动滑块", "安全验证", "环境存在异常",
    "访问受限", "验证身份", "机器人验证",
)
INITIAL_STATE_NAMES = (
    "__INIT_DATA__", "__INITIAL_STATE__", "__PRELOADED_STATE__", "__NEXT_DATA__",
    "g_config", "TShop.Setup",
)
