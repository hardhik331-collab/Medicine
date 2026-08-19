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
