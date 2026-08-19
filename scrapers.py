"""
LIVE PRICE SCRAPERS — Apollo Pharmacy / Netmeds / Tata 1mg / PharmEasy

STATUS (Aug 2026):
- Apollo Pharmacy: VERIFIED, working. GET search.apollo247.com/v4/search
- Netmeds: VERIFIED, working. GET netmeds.com/ext/search/application/api/v1.0/products
- Tata 1mg: PLACEHOLDER — not yet verified.
- PharmEasy: PLACEHOLDER — not yet verified.

For the two PLACEHOLDER platforms below:
1. None of these sites offer a public pricing API. Prices load via internal
   JSON endpoints their own frontend JS calls (React/Next apps) — plain
   requests.get() on the page HTML usually will NOT contain the price.
2. To get the REAL endpoint: open the site in Chrome -> DevTools -> Network
   tab -> filter "Fetch/XHR" -> search a medicine -> find the request that
   returns JSON with price/product data -> copy its URL pattern + response
   shape here.
3. CAPTCHA / "Access Denied" / Cloudflare challenge = bot detection fired.
   There's no reliable way around this respectfully. If it happens
   consistently, fall back to deep-linking (already in the frontend) instead
   of scraping that platform.
4. Rate-limit yourself (1 request every 2-3s per platform) or you'll get
   IP-banned fast.
5. Every function below returns None on any failure instead of raising, so
   one broken scraper never takes down the others.
"""
import re
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/html",
}
TIMEOUT = 8


def _safe_get(url, **kw):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
        resp.raise_for_status()
        return resp
    except Exception:
        return None


def search_apollo(brand_name: str, pincode: str = ""):
    """
    VERIFIED via live DevTools capture (Aug 2026).
    Endpoint: GET https://search.apollo247.com/v4/search
    Plain GET, no auth required. pincode is optional but improves accuracy
    (affects stock/delivery, not price, in most cases).
    """
    resp = _safe_get(
        "https://search.apollo247.com/v4/search",
        params={"query": brand_name, "pincode": pincode},
    )
    if not resp:
        return None
    try:
        data = resp.json()
        products = data["data"]["productDetails"]["products"]
        if not products:
            return None
        item = products[0]
        return {
            "price": item.get("specialPrice") or item.get("price"),
            "mrp": item.get("price"),
            "in_stock": item.get("inStock", True),
            "url": f"https://www.apollopharmacy.in/otc/{item.get('urlKey')}" if item.get("urlKey") else None,
        }
    except Exception:
        return None


def search_netmeds(brand_name: str):
    """
    VERIFIED via live DevTools capture (Aug 2026).
    Endpoint: GET https://www.netmeds.com/ext/search/application/api/v1.0/products
    Netmeds migrated off Magento to the Fynd commerce platform mid-2026 —
    the old catalogsearch/result URL is dead. Plain GET, no auth required.
    """
    resp = _safe_get(
        "https://www.netmeds.com/ext/search/application/api/v1.0/products",
        params={"q": brand_name},
    )
    if not resp:
        return None
    try:
        data = resp.json()
        items = data.get("items")
        if not items:
            return None
        item = items[0]
        price = item.get("price", {})
        return {
            "price": price.get("effective", {}).get("min"),
            "mrp": price.get("marked", {}).get("min"),
            "in_stock": item.get("sellable", True),
            "url": f"https://www.netmeds.com/prescriptions/{item.get('slug')}" if item.get("slug") else None,
        }
    except Exception:
        return None


def search_tata1mg(brand_name: str):
    """PLACEHOLDER — verify via DevTools on 1mg.com."""
    resp = _safe_get(
        "https://www.1mg.com/pharmacy_api/v6/products/search",
        params={"name": brand_name},
    )
    if not resp:
        return None
    try:
        data = resp.json()
        item = data["data"][0]
        return {"price": item.get("price"), "in_stock": item.get("available", True)}
    except Exception:
        return None


def search_pharmeasy(brand_name: str):
    """PLACEHOLDER — verify via DevTools on pharmeasy.in."""
    resp = _safe_get(
        "https://pharmeasy.in/api/search",
        params={"name": brand_name},
    )
    if not resp:
        return None
    try:
        data = resp.json()
        item = data["products"][0]
        return {"price": item.get("discountedPrice") or item.get("price"), "in_stock": item.get("inStock", True)}
    except Exception:
        return None


PLATFORMS = {
    "1mg": search_tata1mg,
    "pharmeasy": search_pharmeasy,
    "apollo": search_apollo,
    "netmeds": search_netmeds,
}


def search_all_sources(brand_name: str, pincode: str = ""):
    """Runs every scraper; each one fails independently and returns None on failure."""
    out = {}
    for key, fn in PLATFORMS.items():
        out[key] = fn(brand_name, pincode) if key == "apollo" else fn(brand_name)
    return out
