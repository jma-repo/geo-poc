"""schema.org JSON-LD. One @graph per page so entities link to each other by @id."""
from __future__ import annotations

import json

from ..catalog import Catalog, Product

_AVAIL = {
    "in_stock": "https://schema.org/InStock",
    "out_of_stock": "https://schema.org/OutOfStock",
    "pre_order": "https://schema.org/PreOrder",
    "backorder": "https://schema.org/BackOrder",
}


def organization(cat: Catalog) -> dict:
    b = cat.brand
    org = {
        "@type": "Organization",
        "@id": f"{b.site_url}#org",
        "name": b.name,
        "url": str(b.site_url),
        "description": b.description,
    }
    if b.legal_name:
        org["legalName"] = b.legal_name
    if b.aliases:
        org["alternateName"] = b.aliases
    if b.logo_url:
        org["logo"] = str(b.logo_url)
    if b.same_as:
        org["sameAs"] = [str(u) for u in b.same_as]
    if b.city or b.region:
        org["address"] = {"@type": "PostalAddress", "addressLocality": b.city,
                          "addressRegion": b.region, "addressCountry": b.country}
    if b.contact_email:
        org["email"] = b.contact_email
    return org


def product(cat: Catalog, p: Product) -> dict:
    b = cat.brand
    offer = {
        "@type": "Offer",
        "url": str(p.url),
        "price": f"{p.price:.2f}",
        "priceCurrency": p.currency,
        "itemCondition": "https://schema.org/NewCondition",
        "seller": {"@id": f"{b.site_url}#org"},
        "shippingDetails": {
            "@type": "OfferShippingDetails",
            "shippingDestination": [{"@type": "DefinedRegion", "addressCountry": c} for c in b.ships_to],
        },
    }
    if p.availability in _AVAIL:
        offer["availability"] = _AVAIL[p.availability]
    if b.return_days is not None:
        offer["hasMerchantReturnPolicy"] = {
            "@type": "MerchantReturnPolicy",
            "applicableCountry": b.ships_to,
            "returnPolicyCategory": "https://schema.org/MerchantReturnFiniteReturnWindow",
            "merchantReturnDays": b.return_days,
        }
    node = {
        "@type": "Product",
        "@id": f"{p.url}#product",
        "name": p.name,
        "description": p.description,
        "image": str(p.image_url),
        "sku": p.id,
        "brand": {"@type": "Brand", "name": b.name},
        "category": p.category,
        "offers": offer,
    }
    if p.gtin:
        node["gtin"] = p.gtin
    if p.mpn:
        node["mpn"] = p.mpn
    if p.specs:
        node["additionalProperty"] = [{"@type": "PropertyValue", "name": k, "value": v}
                                      for k, v in p.specs.items()]
    if p.review_count:
        node["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": p.rating_value,
                                   "reviewCount": p.review_count}
    return node


def faq(p: Product) -> dict | None:
    if not p.faqs:
        return None
    return {
        "@type": "FAQPage",
        "@id": f"{p.url}#faq",
        "mainEntity": [{"@type": "Question", "name": f.q,
                        "acceptedAnswer": {"@type": "Answer", "text": f.a}} for f in p.faqs],
    }


def product_page_graph(cat: Catalog, p: Product) -> dict:
    page = {
        "@type": "WebPage",
        "@id": f"{p.url}#page",
        "url": str(p.url),
        "name": p.name,
        "description": p.summary,
        "dateModified": p.updated.isoformat(),
        "about": {"@id": f"{p.url}#product"},
        "publisher": {"@id": f"{cat.brand.site_url}#org"},
    }
    graph = [page, organization(cat), product(cat, p)]
    if (f := faq(p)):
        graph.append(f)
    return {"@context": "https://schema.org", "@graph": graph}


def script_tag(data: dict) -> str:
    # Escape "</" so a value can never close the script element.
    body = json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")
    return f'<script type="application/ld+json">\n{body}\n</script>'
