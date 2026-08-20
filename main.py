"""
MediFind live-price backend.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --reload

Deploy (free tier works): Render, Railway, Fly.io, or PythonAnywhere.
Once deployed, put your backend's public URL into BACKEND_URL at the top
of index.html's <script> block.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scrapers import search_all_sources

app = FastAPI(title="MediFind Live Price API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to your site's domain once deployed
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/live-price")
def live_price(brand: str, pincode: str = ""):
    """
    Called when the user expands a medicine card on the frontend.
    Returns {"1mg": {...} | null, "pharmeasy": ..., "apollo": ..., "netmeds": ...}
    Any platform that fails (blocked, changed layout, timeout) comes back null —
    the frontend falls back to a plain deep-link for that one.
    """
    return search_all_sources(brand, pincode)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/diagnose")
def diagnose(brand: str = "Dolo 650"):
    """
    Tells you WHY a platform is failing, instead of just returning null.

    For each platform: is the proxy in use, what HTTP status came back,
    how big was the response, and did we get a price. This is what
    distinguishes "blocked at the network" from "fetched fine but the
    parser broke" — two problems with completely different fixes.
    """
    import scrapers as s

    targets = {
        "apollo": ("https://search.apollo247.com/v4/search",
                   {"query": brand, "pincode": "380001"},
                   {"Referer": "https://www.apollopharmacy.in/"}),
        "netmeds": ("https://www.netmeds.com/ext/search/application/api/v1.0/products",
                    {"q": brand}, {}),
        "1mg": ("https://www.1mg.com/pwa-dweb-api/api/v4/search/autocomplete",
                {"q": brand, "types": "allopathy,brand,sku,udp,disease", "per_page": 12},
                {"X-City": "Gurgaon", "X-Access-Key": "1mg_client_access_key",
                 "Accept": "application/vnd.healthkartplus.v4+json",
                 "Referer": "https://www.1mg.com/"}),
        "pharmeasy": ("https://pharmeasy.in/search/all", {"name": brand}, {}),
    }

    out = {
        "proxy_configured": bool(s.PROXY_URL),
        "groq_configured": bool(s.GROQ_API_KEY),
        "platforms": {},
    }

    for key, (url, params, hdrs) in targets.items():
        resp = s._safe_get(url, headers=hdrs, params=params)
        out["platforms"][key] = {
            "reachable": resp is not None,
            "status": getattr(resp, "status_code", None),
            "bytes": len(resp.content) if resp is not None else 0,
            "upstream_status_via_proxy": (
                resp.headers.get("x-upstream-status") if resp is not None else None
            ),
        }

    # Also report what the real scrapers produce end to end.
    out["live_result"] = s.search_all_sources(brand, "380001")
    return out
