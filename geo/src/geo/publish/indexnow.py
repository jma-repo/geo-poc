"""IndexNow: tell Bing (and through it Copilot and ChatGPT search) about changed URLs right away.

Freshness matters in AI answers, and Bing's index is a major retrieval source for ChatGPT.
Setup: put a key file at https://<your-domain>/<key>.txt containing the key itself.
"""
from __future__ import annotations

import httpx

from ..catalog import Catalog

ENDPOINT = "https://api.indexnow.org/indexnow"


def payload(cat: Catalog, key: str, urls: list[str] | None = None) -> dict:
    host = cat.brand.site_url.host
    urls = urls or [str(p.url) for p in cat.products]
    foreign = [u for u in urls if httpx.URL(u).host != host]
    if foreign:
        raise ValueError(f"IndexNow URLs must be on {host}: {foreign}")
    return {"host": host, "key": key, "keyLocation": f"{cat.brand.site_url}{key}.txt", "urlList": urls}


def submit(cat: Catalog, key: str, urls: list[str] | None = None, client: httpx.Client | None = None) -> int:
    client = client or httpx.Client(timeout=30)
    r = client.post(ENDPOINT, json=payload(cat, key, urls))
    if r.status_code not in (200, 202):
        raise RuntimeError(f"IndexNow returned {r.status_code}: {r.text[:300]}")
    return r.status_code
