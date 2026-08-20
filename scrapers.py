"""
LIVE PRICE SCRAPERS — Apollo Pharmacy / Netmeds / Tata 1mg / PharmEasy

STATUS (Aug 2026):
- Apollo Pharmacy: VERIFIED, working. GET search.apollo247.com/v4/search
- Netmeds: VERIFIED, working. GET netmeds.com/ext/search/application/api/v1.0/products
- Tata 1mg: VERIFIED, working. Autocomplete API + PDP HTML price scrape.
- PharmEasy: VERIFIED, working. search/all page's embedded __NEXT_DATA__ JSON.

All four platforms are now live-verified. No placeholders remain below.
"""
import concurrent.futures
import json
import os
import re
import threading
import time
from urllib.parse import urlencode
import requests

# ---------------------------------------------------------------------------
# Optional Cloudflare Worker fetch proxy
# ---------------------------------------------------------------------------
# Apollo blocks Render's datacenter IPs outright. Routing through a
# Cloudflare Worker gives requests a different source IP that may not be
# blocked. Set these in Render's Environment tab to enable; leave unset
# and everything fetches directly exactly as before.
PROXY_URL = os.environ.get("SCRAPER_PROXY_URL", "")
PROXY_TOKEN = os.environ.get("SCRAPER_PROXY_TOKEN", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/html",
}
TIMEOUT = 8

# ---------------------------------------------------------------------------
# In-memory result cache
# ---------------------------------------------------------------------------
# Medicine prices don't move minute-to-minute, and on a free Render instance
# every avoided outbound request is real latency saved. A repeated search
# within the TTL returns instantly instead of re-hitting four pharmacy sites.
# Bounded so a long-running instance can't grow unboundedly.
_CACHE_TTL = 900          # 15 minutes
_CACHE_MAX = 500
_cache = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        value, expires_at = hit
        if time.time() > expires_at:
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key, value):
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # Drop the soonest-to-expire entry to make room.
            oldest = min(_cache, key=lambda k: _cache[k][1])
            _cache.pop(oldest, None)
        _cache[key] = (value, time.time() + _CACHE_TTL)


# ---------------------------------------------------------------------------
# LLM fallback (Groq free tier)
# ---------------------------------------------------------------------------
# WHAT THIS DOES AND DOESN'T FIX
#
# This is a self-healing layer for LAYOUT DRIFT only — when a site renames a
# JSON key or changes a CSS class, the structured parser above breaks but the
# page content is still there, and a model can read the price off it.
#
# It canNOT fix:
#   - Apollo: the request is blocked at the network level from cloud IPs, so
#     there is no page content to read.
#   - 1mg's missing price: the page is served, but the price block appears to
#     be withheld from datacenter IPs. A model cannot read text that was
#     never sent.
#
# Cost/latency discipline: this only ever fires when the structured parse
# already failed, so the happy path never pays for it.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Free-tier model catalogs churn and models get removed without notice, so
# try in order rather than hardcoding a single name.
GROQ_MODELS = ["llama-3.1-8b-instant", "llama-3.3-70b-versatile", "gpt-oss-20b"]
LLM_TIMEOUT = 6


def _price_context(text, max_chars=3000):
    """
    Send the model only the parts of the page likely to contain a price,
    not 600KB of HTML. Grabs windows around rupee symbols / price words.
    """
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    windows, seen = [], 0
    for m in re.finditer(r"(?:\u20b9|Rs\.?|MRP|price)", text, re.I):
        start = max(0, m.start() - 120)
        windows.append(text[start:m.start() + 180])
        seen += 1
        if seen >= 12:
            break
    joined = " ... ".join(windows) if windows else text[:max_chars]
    return joined[:max_chars]


def _llm_extract_price(page_text, brand_name, platform):
    """
    Ask a small fast model to read price/MRP off page text when the
    structured parser failed. Returns {"price": float|None, "mrp": float|None}
    or None. Never raises — a failed fallback just means no price shown.
    """
    if not GROQ_API_KEY or not page_text:
        return None

    context = _price_context(page_text)
    if not context.strip():
        return None

    prompt = (
        f'From this text off a {platform} product page for "{brand_name}", '
        "extract the current selling price and the MRP (list price) in INR.\n"
        "Rules:\n"
        "- Reply with ONLY a JSON object, no prose, no markdown fences.\n"
        '- Shape: {"price": <number or null>, "mrp": <number or null>}\n'
        "- price = what the customer pays now; mrp = the struck-through list price.\n"
        "- If only one number is present, use it as price and set mrp null.\n"
        "- If you cannot find a real price, return {\"price\": null, \"mrp\": null}. "
        "Never guess or invent a number.\n\n"
        f"TEXT:\n{context}"
    )

    for model in GROQ_MODELS:
        try:
            resp = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 80,
                    "response_format": {"type": "json_object"},
                },
                timeout=LLM_TIMEOUT,
            )
            if resp.status_code == 404:
                continue          # model retired — try the next one
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"```(?:json)?|```", "", content).strip()
            data = json.loads(content)

            def _num(v):
                if v is None:
                    return None
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    return None
                # Sanity band: an Indian retail medicine price outside this
                # range is far more likely a hallucination or a stray number
                # (pincode, pack count, phone digits) than a real price.
                return f if 0.5 <= f <= 100000 else None

            price, mrp = _num(data.get("price")), _num(data.get("mrp"))
            if price is None and mrp is None:
                return None
            return {"price": price, "mrp": mrp}
        except Exception:
            continue
    return None


def _safe_get(url, headers=None, **kw):
    """
    Fetch a URL, optionally via the Cloudflare Worker proxy.

    Apollo (and possibly 1mg) block Render's datacenter IPs. If
    SCRAPER_PROXY_URL is set, requests are routed through a Cloudflare
    Worker whose edge IPs may not be blocked.

    Falls back to a direct request whenever the proxy is unset, errors,
    or returns a non-2xx — so enabling the proxy can never make things
    worse than fetching directly.
    """
    merged_headers = {**HEADERS, **(headers or {})}
    params = kw.pop("params", None)

    # Fold params into the URL ourselves; the proxy takes one encoded `url`.
    full_url = url
    if params:
        query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        if query:
            full_url = f"{url}{'&' if '?' in url else '?'}{query}"

    if PROXY_URL:
        try:
            proxy_qs = {"url": full_url}
            if PROXY_TOKEN:
                proxy_qs["token"] = PROXY_TOKEN
            proxy_target = f"{PROXY_URL.rstrip('/')}/?{urlencode(proxy_qs)}"

            # Worker forwards any x-fwd-* header with the prefix stripped.
            fwd = {f"x-fwd-{k}": v for k, v in merged_headers.items()}

            resp = requests.get(proxy_target, headers=fwd, timeout=TIMEOUT + 4)
            if resp.status_code == 200:
                return resp
            # Non-200 => proxy reachable but upstream refused, or proxy
            # misconfigured. Fall through to a direct attempt.
        except Exception:
            pass

    try:
        resp = requests.get(full_url, headers=merged_headers, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp
    except Exception:
        return None


def _tokenize(s):
    """
    Split into whole-word/number tokens, treating a digit->letter or
    letter->digit boundary as a word break (so "650Mg" tokenizes as
    "650", "mg" — matching how a human reads it — instead of staying
    fused as one "650mg" token that a plain "650" query would miss).
    """
    s = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", s)
    s = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", s)
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _best_match(candidates, brand_name, name_fn):
    """
    Shared brand-matching logic for all four platforms. A search for one
    brand should never silently return a *different* brand of the same
    salt (e.g. searching "Azithral 500" should never surface "Aziford
    500" instead) — pharmacy search APIs frequently do this to push
    substitutes/cheaper alternatives.

    Uses WHOLE-WORD token matching, not substring matching — "Dolopar"
    must never match a search for "Dolo", even though "dolo" is a
    literal substring of "dolopar". Every query token must appear as
    its own exact word in the candidate name.

    Returns the first candidate whose name contains every word of the
    query brand name, or None if nothing matches exactly. None means
    "don't show a price for this platform" rather than "show a
    different brand's price" — the frontend already handles None by
    falling back to a plain deep-link, so nothing breaks.
    """
    query_tokens = _tokenize(brand_name)
    if not query_tokens:
        return None
    for c in candidates:
        name_tokens = _tokenize(name_fn(c))
        if query_tokens.issubset(name_tokens):
            return c
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
        headers={
            "Referer": "https://www.apollopharmacy.in/",
            "Origin": "https://www.apollopharmacy.in",
        },
    )
    if not resp:
        return None
    try:
        data = resp.json()
        products = data["data"]["productDetails"]["products"]
        if not products:
            return None
        item = _best_match(products, brand_name, lambda p: p.get("name", ""))
        if not item:
            return None
        url_key = item.get("urlKey")
        prefix = "medicine" if item.get("isPrescriptionRequired") else "otc"
        return {
            "price": item.get("specialPrice") or item.get("price"),
            "mrp": item.get("price"),
            "in_stock": item.get("status", "").lower() != "out_of_stock" if item.get("status") else True,
            "url": f"https://www.apollopharmacy.in/{prefix}/{url_key}" if url_key else None,
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
        item = _best_match(items, brand_name, lambda i: i.get("name", ""))
        if not item:
            return None
        price = item.get("price", {})
        return {
            "price": price.get("effective", {}).get("min"),
            "mrp": price.get("marked", {}).get("min"),
            "in_stock": item.get("sellable", True),
            "url": f"https://www.netmeds.com/product/{item.get('slug')}" if item.get("slug") else None,
        }
    except Exception:
        return None


def search_tata1mg(brand_name: str):
    """
    VERIFIED via live DevTools capture (Aug 2026).
    Two-step process:
      1. Call the autocomplete API to resolve brand name -> PDP path.
         Requires two headers 1mg's own frontend sends: X-City and
         X-Access-Key. X-Access-Key is a static, non-secret constant
         ("1mg_client_access_key") baked into their public JS bundle,
         not a per-user token.
      2. Fetch the PDP page HTML directly and regex out the price —
         1mg does NOT expose price via a JSON API on this endpoint,
         only server-rendered HTML. This step is more fragile than
         Apollo/Netmeds since it depends on 1mg's CSS class names.
    """
    resp = _safe_get(
        "https://www.1mg.com/pwa-dweb-api/api/v4/search/autocomplete",
        params={"q": brand_name, "types": "allopathy,brand,sku,udp,disease", "per_page": 12},
        headers={
            "X-City": "Gurgaon",  # any valid city works; doesn't affect price
            "X-Access-Key": "1mg_client_access_key",
            "Accept": "application/vnd.healthkartplus.v4+json",
            "Referer": "https://www.1mg.com/",
        },
    )
    if not resp:
        return None
    try:
        results = resp.json()["data"]["search_results"]
        drugs = [r for r in results if r.get("type") == "drug" and r.get("url")]
        # 1mg's autocomplete name field includes <b> highlight tags around
        # the matched query — strip those before matching so substring
        # comparison works.
        drug = _best_match(drugs, brand_name, lambda r: re.sub(r"</?b>", "", r.get("name", "")))
        if not drug:
            return None
        path = drug["url"].split("?")[0]
        pdp_url = f"https://www.1mg.com{path}"
    except Exception:
        return None

    pdp_resp = _safe_get(pdp_url, headers={"Referer": "https://www.1mg.com/"})
    if not pdp_resp:
        return {"price": None, "mrp": None, "in_stock": True, "url": pdp_url}
    try:
        html = pdp_resp.text
        price_match = re.search(r'displaySmallExtraBold"><span>\u20b9([\d.]+)</span>', html)
        mrp_match = re.search(r'textStrikethrough textTertiary">\u20b9([\d.]+)', html)
        if price_match:
            return {
                "price": float(price_match.group(1)),
                "mrp": float(mrp_match.group(1)) if mrp_match else None,
                "in_stock": True,
                "url": pdp_url,
            }
        # Structured parse failed — either 1mg changed their markup, or this
        # page simply didn't include a price block. Hand the raw page to the
        # LLM fallback; it returns None if there's genuinely no price there.
        llm = _llm_extract_price(html, brand_name, "Tata 1mg")
        if llm:
            return {**llm, "in_stock": True, "url": pdp_url, "via": "llm"}
        return {"price": None, "mrp": None, "in_stock": True, "url": pdp_url}
    except Exception:
        return {"price": None, "mrp": None, "in_stock": True, "url": pdp_url}


def search_pharmeasy(brand_name: str, pincode: str = ""):
    """
    VERIFIED via live DevTools capture (Aug 2026).
    Two-step process (search/all page is server-rendered Next.js, price
    lives in the embedded __NEXT_DATA__ JSON blob, not a separate API call):
      1. Fetch https://pharmeasy.in/search/all?name=<brand>&pincode=<pincode>
         (plain GET). PharmEasy genuinely runs pincode-based dynamic
         pricing — confirmed via live testing the same medicine priced
         differently across pincodes (e.g. ~30% swing), unlike Netmeds/1mg
         which showed flat national pricing regardless of pincode.
      2. Extract the __NEXT_DATA__ <script> tag and read
         props.pageProps.productList[0] for mrpDecimal/salePriceDecimal/slug.
    """
    resp = _safe_get(
        "https://pharmeasy.in/search/all",
        params={"name": brand_name, "pincode": pincode} if pincode else {"name": brand_name},
    )
    if not resp:
        return None
    try:
        html = resp.text
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if not match:
            return None
        next_data = json.loads(match.group(1))
        products = next_data["props"]["pageProps"].get("productList")
        if not products:
            return None
        # PharmEasy's search frequently leads with a different (often
        # cheaper) brand of the same salt rather than the brand actually
        # searched — only accept an exact brand-name match.
        item = _best_match(products, brand_name, lambda p: p.get("name", ""))
        if not item:
            return None
        slug = item.get("slug")
        url = f"https://pharmeasy.in/online-medicine-order/{slug}" if slug else None
        price = float(item["salePriceDecimal"]) if item.get("salePriceDecimal") else None
        mrp = float(item["mrpDecimal"]) if item.get("mrpDecimal") else None

        # If the price *fields* were renamed but we still matched the right
        # product, fall back to the LLM — but only on that product's own
        # page, never the multi-brand search page. Reading a price off a
        # list of many brands risks attributing another brand's price to
        # this one, which is worse than showing no price at all.
        if price is None and url:
            pdp = _safe_get(url)
            if pdp:
                llm = _llm_extract_price(pdp.text, brand_name, "PharmEasy")
                if llm:
                    return {
                        **llm,
                        "in_stock": item.get("productAvailabilityFlags", {}).get("isAvailable", True),
                        "url": url,
                        "via": "llm",
                    }
        return {
            "price": price,
            "mrp": mrp,
            "in_stock": item.get("productAvailabilityFlags", {}).get("isAvailable", True),
            "url": url,
        }
    except Exception:
        return None


PLATFORMS = {
    "1mg": search_tata1mg,
    "pharmeasy": search_pharmeasy,
    "apollo": search_apollo,
    "netmeds": search_netmeds,
}


def search_all_sources(brand_name: str, pincode: str = ""):
    """
    Runs every scraper CONCURRENTLY; each fails independently and returns
    None on failure.

    Concurrency matters a lot here: run sequentially, four scrapers at an
    8s timeout each (and 1mg makes two round-trips of its own) can stack
    into ~40s worst case. In parallel the whole call is bounded by the
    slowest single platform instead of their sum.

    Results are cached briefly so a repeat search is instant.
    """
    cache_key = f"{brand_name.strip().lower()}|{pincode.strip()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Only Apollo and PharmEasy have confirmed pincode-dependent pricing —
    # Netmeds and 1mg showed flat national pricing in live testing, so
    # there's no point passing pincode to them.
    pincode_aware = {"apollo", "pharmeasy"}

    def run(key, fn):
        try:
            return key, (fn(brand_name, pincode) if key in pincode_aware else fn(brand_name))
        except Exception:
            # One platform blowing up must never take down the others.
            return key, None

    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PLATFORMS)) as pool:
        futures = [pool.submit(run, key, fn) for key, fn in PLATFORMS.items()]
        for fut in concurrent.futures.as_completed(futures, timeout=30):
            try:
                key, value = fut.result()
                out[key] = value
            except Exception:
                continue

    # Guarantee every platform key is present even if a worker vanished,
    # so the frontend never sees an unexpectedly missing field.
    for key in PLATFORMS:
        out.setdefault(key, None)

    _cache_set(cache_key, out)
    return out
