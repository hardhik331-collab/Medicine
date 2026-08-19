"""
LIVE PRICE SCRAPERS — Apollo Pharmacy / Netmeds / Tata 1mg / PharmEasy

READ THIS FIRST:
1. None of these sites offer a public pricing API. Prices load via internal
   JSON endpoints their own frontend JS calls (React/Next apps) — plain
   requests.get() on the page HTML usually will NOT contain the price.
2. To get the REAL endpoint: open the site in Chrome -> DevTools -> Network
   tab -> filter "Fetch/XHR" -> search a medicine -> find the request that
   returns JSON with price/product data -> copy its URL pattern + response
   shape here. This sandbox has no network access to these domains, so
   everything below is a best-guess starting point, not verified.
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


def search_apollo(brand_name: str):
    """
    PLACEHOLDER endpoint — verify via DevTools on apollopharmacy.in.
    Expected shape once fixed: JSON list with a price field per product.
    """
    resp = _safe_get(
        "https://www.apollopharmacy.in/api/v1/search",
        params={"q": brand_name},
    )
    if not resp:
        return None
    try:
        data = resp.json()
        item = data["products"][0]
        return {"price": item.get("price") or item.get("mrp"), "in_stock": item.get("in_stock", True)}
    except Exception:
        return None


def search_netmeds(brand_name: str):
    """PLACEHOLDER — verify via DevTools on netmeds.com."""
    resp = _safe_get(
        "https://www.netmeds.com/rest/V2.4.1/catalogsearch/result",
        params={"q": brand_name},
    )
    if not resp:
        return None
    try:
        data = resp.json()
        item = data["products"][0]
        return {"price": item.get("special_price") or item.get("price"), "in_stock": item.get("is_in_stock", True)}
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


def search_all_sources(brand_name: str):
    """Runs every scraper; each one fails independently and returns None on failure."""
    out = {}
    for key, fn in PLATFORMS.items():
        out[key] = fn(brand_name)
    return out
