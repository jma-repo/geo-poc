"""Product feeds that AI shopping surfaces ingest directly.

- OpenAI commerce product feed (ChatGPT shopping / Instant Checkout). Field names follow
  developers.openai.com/commerce/specs/file-upload/products as of Nov 2026: nine required basics
  (item_id, title, description, url, brand, seller_name, image_url, availability, price) plus the
  required geo fields target_countries and store_country. Re-check the spec before submitting.
- Google Merchant Center feed (Shopping, AI Overviews, AI Mode, Gemini). Also accepted by
  OpenAI's Google-compatible mapping.
"""
from __future__ import annotations

import csv
import io
import json

from ..catalog import Catalog

GOOGLE_AVAIL = {"in_stock": "in_stock", "out_of_stock": "out_of_stock",
                "pre_order": "preorder", "backorder": "backorder", "unknown": "out_of_stock"}


def openai_feed_rows(cat: Catalog, enable_checkout: bool = False) -> list[dict]:
    b = cat.brand
    rows = []
    for p in cat.products:
        row = {
            "item_id": p.id,
            "title": p.name[:150],
            "description": p.description[:5000],
            "url": str(p.url),
            "brand": b.name,
            "seller_name": b.name,
            "image_url": str(p.image_url),
            "availability": p.availability,
            "price": f"{p.price:.2f} {p.currency}",
            "target_countries": b.ships_to,
            "store_country": b.country,
            "is_eligible_search": True,
            "is_eligible_checkout": enable_checkout,
            "product_category": p.category,
        }
        if p.gtin:
            row["gtin"] = p.gtin
        if p.mpn:
            row["mpn"] = p.mpn
        if p.review_count:
            row["review_count"] = p.review_count
            row["review_rating"] = p.rating_value
        if b.return_days is not None:
            row["return_window"] = b.return_days
        rows.append(row)
    return rows


def openai_feed_jsonl(cat: Catalog, enable_checkout: bool = False) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in openai_feed_rows(cat, enable_checkout))


def google_feed_tsv(cat: Catalog) -> str:
    cols = ["id", "title", "description", "link", "image_link", "availability", "price", "brand",
            "condition", "gtin", "mpn", "identifier_exists", "google_product_category", "product_type"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, delimiter="\t", lineterminator="\n")
    w.writeheader()
    for p in cat.products:
        has_id = bool(p.gtin or p.mpn)
        w.writerow({
            "id": p.id,
            "title": p.name[:150],
            "description": " ".join(p.description.split())[:5000],
            "link": str(p.url),
            "image_link": str(p.image_url),
            "availability": GOOGLE_AVAIL[p.availability],
            "price": f"{p.price:.2f} {p.currency}",
            "brand": cat.brand.name,
            "condition": "new",
            "gtin": p.gtin or "",
            "mpn": p.mpn or "",
            "identifier_exists": "yes" if has_id else "no",
            "google_product_category": "",
            "product_type": p.category,
        })
    return buf.getvalue()
