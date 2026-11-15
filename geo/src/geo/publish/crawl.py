"""Files that decide whether AI crawlers can find and read the site."""
from __future__ import annotations

from ..catalog import Catalog

# Fetch-on-demand and search-index bots: blocking these removes you from AI answers.
SEARCH_BOTS = [
    "OAI-SearchBot", "ChatGPT-User",          # ChatGPT search and user-initiated browsing
    "PerplexityBot", "Perplexity-User",       # Perplexity index and live fetches
    "Claude-SearchBot", "Claude-User",        # Claude search index and live fetches
    "Googlebot", "Bingbot", "Applebot",       # classic indexes that also feed AI answers
    "DuckAssistBot",
]
# Model-training crawlers: optional. Allowing them lets models learn about you offline.
TRAINING_BOTS = ["GPTBot", "ClaudeBot", "Google-Extended", "Applebot-Extended", "CCBot", "Meta-ExternalAgent"]


def robots_txt(cat: Catalog) -> str:
    lines = ["# AI search and retrieval crawlers: allowed so the site can be cited."]
    for bot in SEARCH_BOTS:
        lines += [f"User-agent: {bot}", "Allow: /", ""]
    rule = "Allow: /" if cat.allow_ai_training else "Disallow: /"
    lines.append("# Model-training crawlers (catalog.allow_ai_training).")
    for bot in TRAINING_BOTS:
        lines += [f"User-agent: {bot}", rule, ""]
    lines += ["User-agent: *", "Allow: /", "", f"Sitemap: {cat.brand.site_url}sitemap.xml", ""]
    return "\n".join(lines)


def sitemap_xml(cat: Catalog) -> str:
    urls = [f"  <url><loc>{p.url}</loc><lastmod>{p.updated.isoformat()}</lastmod></url>" for p in cat.products]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls) + "\n</urlset>\n")


def llms_txt(cat: Catalog) -> str:
    """llms.txt (llmstxt.org). Cheap to publish; no engine has confirmed it as a ranking input."""
    b = cat.brand
    out = [f"# {b.name}", "", f"> {b.description}", ""]
    if b.city:
        out += [f"Based in {b.city}, {b.region or b.country}. Ships to: {', '.join(b.ships_to)}.", ""]
    out.append("## Products")
    out += [f"- [{p.name}]({p.url}): {p.summary} {p.price:.2f} {p.currency}." for p in cat.products]
    out += ["", "## Optional", f"- [Full product facts]({b.site_url}llms-full.txt)", ""]
    return "\n".join(out)


def llms_full_txt(cat: Catalog) -> str:
    return cat.facts_text() + "\n"
