"""Static, server-rendered pages.

Most AI crawlers do not run JavaScript, so every fact is in the HTML itself.
Layout is answer-first: the direct answer, then checkable facts with sources, then FAQs.
"""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

from ..catalog import Catalog, Product
from . import crawl, feeds, jsonld

CSS = """body{font:16px/1.6 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a}
table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3rem .6rem;text-align:left}
.answer{font-size:1.1rem;border-left:4px solid #2b6cb0;padding-left:1rem}small{color:#555}"""


def _page(title: str, description: str, body: str, head_extra: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<meta name="description" content="{escape(description)}">
<style>{CSS}</style>
{head_extra}
</head>
<body>
{body}
</body>
</html>
"""


def _answer_cards(cards: list[dict]) -> str:
    out = []
    for c in cards:
        facts = "".join(f"<li>{escape(f)}</li>" for f in c.get("key_facts", []))
        out.append(f"<section><h2>{escape(c['question'])}</h2>\n"
                   f"<p class=\"answer\">{escape(c['answer'])}</p>\n"
                   + (f"<ul>{facts}</ul>\n" if facts else "") + "</section>")
    return "\n".join(out)


def product_html(cat: Catalog, p: Product, cards: list[dict] | None = None) -> str:
    b = cat.brand
    specs = "".join(f"<tr><th>{escape(k)}</th><td>{escape(v)}</td></tr>" for k, v in p.specs.items())
    claims = "".join(
        f"<li>{escape(c.text)}"
        + (f' <small>(<a href="{escape(str(c.source_url))}">source</a>)</small>' if c.source_url else "")
        + "</li>" for c in p.claims)
    uses = "".join(f"<li>{escape(u)}</li>" for u in p.use_cases)
    faqs = "".join(f"<h3>{escape(f.q)}</h3><p>{escape(f.a)}</p>" for f in p.faqs)
    rating = (f"<p>Rated {p.rating_value} out of 5 from {p.review_count} reviews.</p>" if p.review_count else "")
    returns = f" {b.return_days}-day returns." if b.return_days is not None else ""
    body = f"""<header><a href="{escape(str(b.site_url))}">{escape(b.name)}</a></header>
<main>
<article>
<h1>{escape(p.name)}</h1>
<p class="answer">{escape(p.summary)}</p>
<p><strong>{p.price:.2f} {p.currency}</strong> · {escape(p.availability.replace('_', ' '))} ·
ships to {escape(', '.join(b.ships_to))}.{escape(returns)}</p>
<img src="{escape(str(p.image_url))}" alt="{escape(p.name)}" width="480">
<p>{escape(p.description)}</p>
{rating}
{f'<h2>Specifications</h2><table>{specs}</table>' if specs else ''}
{f'<h2>Key facts</h2><ul>{claims}</ul>' if claims else ''}
{f'<h2>Best for</h2><ul>{uses}</ul>' if uses else ''}
{_answer_cards(cards or [])}
{f'<h2>Frequently asked questions</h2>{faqs}' if faqs else ''}
<p><small>Last updated <time datetime="{p.updated.isoformat()}">{p.updated.isoformat()}</time>.</small></p>
</article>
</main>"""
    head = f'<link rel="canonical" href="{escape(str(p.url))}">\n' + jsonld.script_tag(jsonld.product_page_graph(cat, p))
    return _page(f"{p.name} | {b.name}", p.summary, body, head)


def index_html(cat: Catalog) -> str:
    b = cat.brand
    items = "".join(f'<li><a href="{escape(str(p.url))}">{escape(p.name)}</a>: {escape(p.summary)}</li>'
                    for p in cat.products)
    body = f"<main><h1>{escape(b.name)}</h1><p class=\"answer\">{escape(b.description)}</p><ul>{items}</ul></main>"
    head = jsonld.script_tag({"@context": "https://schema.org", "@graph": [jsonld.organization(cat)]})
    return _page(b.name, b.description, body, head)


def load_cards(cards_dir: Path) -> dict[str, list[dict]]:
    """Approved answer cards, grouped by product id. Only cards a human moved to approved/ are used."""
    out: dict[str, list[dict]] = {}
    if cards_dir.is_dir():
        for f in sorted(cards_dir.glob("*.json")):
            card = json.loads(f.read_text(encoding="utf-8"))
            if card.get("problems"):
                raise ValueError(f"{f.name} failed the grounding check and cannot be published: {card['problems']}")
            out.setdefault(card["product_id"], []).append(card)
    return out


def build(cat: Catalog, out: Path, cards_dir: Path | None = None, enable_checkout: bool = False) -> list[Path]:
    """Write the site, feeds and crawler files. Returns the written paths."""
    cards = load_cards(cards_dir) if cards_dir else {}
    files: dict[str, str] = {
        "index.html": index_html(cat),
        "robots.txt": crawl.robots_txt(cat),
        "sitemap.xml": crawl.sitemap_xml(cat),
        "llms.txt": crawl.llms_txt(cat),
        "llms-full.txt": crawl.llms_full_txt(cat),
        "feeds/openai-products.jsonl": feeds.openai_feed_jsonl(cat, enable_checkout),
        "feeds/google-merchant.tsv": feeds.google_feed_tsv(cat),
    }
    for p in cat.products:
        files[f"{p.slug}/index.html"] = product_html(cat, p, cards.get(p.id))
    written = []
    for rel, text in files.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
