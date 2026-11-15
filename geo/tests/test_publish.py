import csv
import io
import json
import re

import httpx
import pytest
import respx

from geo import catalog
from geo.publish import crawl, feeds, indexnow, jsonld, site


def ld_blocks(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">\n(.*?)\n</script>', html, re.S)]


def test_product_page_jsonld_is_linked_and_complete(cat):
    p = cat.products[0]
    html = site.product_html(cat, p)
    (graph,) = ld_blocks(html)
    nodes = {n["@id"]: n for n in graph["@graph"]}
    types = {n["@type"] for n in nodes.values()}
    assert types == {"WebPage", "Organization", "Product", "FAQPage"}
    prod = nodes[f"{p.url}#product"]
    offer = prod["offers"]
    assert offer["price"] == "189.00" and offer["priceCurrency"] == "CAD"
    assert offer["availability"] == "https://schema.org/InStock"
    assert offer["hasMerchantReturnPolicy"]["merchantReturnDays"] == 30
    assert "aggregateRating" not in prod  # no reviews in the example, so no rating
    # every @id reference inside the graph resolves
    refs = re.findall(r'\{"@id": "([^"]+)"\}', json.dumps(graph))
    assert refs and all(r in nodes for r in refs)
    assert nodes[f"{p.url}#page"]["dateModified"] == "2026-11-10"


def test_facts_are_in_the_html_not_only_in_jsonld(cat):
    html = site.product_html(cat, cat.products[0])
    visible = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    for needle in ["189.00 CAD", "10 mm", "Will it stop golf-ball-sized hail?", "30-day returns", "2026-11-10"]:
        assert needle in visible


def test_script_tag_cannot_be_broken_out_of():
    tag = jsonld.script_tag({"name": "</script><script>alert(1)</script>"})
    assert tag.count("</script>") == 1


def test_openai_feed_has_required_fields(cat):
    rows = [json.loads(line) for line in feeds.openai_feed_jsonl(cat).splitlines()]
    required = {"item_id", "title", "description", "url", "brand", "seller_name", "image_url",
                "availability", "price", "target_countries", "store_country"}
    assert required <= rows[0].keys()
    assert rows[0]["price"] == "189.00 CAD"
    assert rows[0]["target_countries"] == ["CA"] and rows[0]["store_country"] == "CA"
    assert rows[0]["is_eligible_checkout"] is False


def test_google_feed(cat):
    rows = list(csv.DictReader(io.StringIO(feeds.google_feed_tsv(cat)), delimiter="\t"))
    assert rows[0]["price"] == "189.00 CAD"
    assert rows[0]["identifier_exists"] == "yes"  # has an MPN
    assert "\n" not in rows[0]["description"]


def test_robots_allows_ai_search_and_respects_training_choice(cat):
    txt = crawl.robots_txt(cat)
    assert "User-agent: OAI-SearchBot\nAllow: /" in txt
    assert "User-agent: GPTBot\nAllow: /" in txt
    cat.allow_ai_training = False
    txt = crawl.robots_txt(cat)
    assert "User-agent: GPTBot\nDisallow: /" in txt
    assert "User-agent: Claude-SearchBot\nAllow: /" in txt
    assert txt.rstrip().endswith("Sitemap: https://www.foothillgear.example/sitemap.xml")


def test_llms_txt(cat):
    txt = crawl.llms_txt(cat)
    assert txt.startswith("# Foothill Gear Co.\n\n> ")
    assert "[Foothill Hail Cover for Sedans](https://www.foothillgear.example/hail-cover-sedan/)" in txt


def test_build_writes_everything_and_uses_only_clean_cards(cat, tmp_path):
    approved = tmp_path / "approved"
    approved.mkdir()
    (approved / "a.json").write_text(json.dumps({
        "product_id": "FG-HC-01", "question": "Can a car cover stop hail in Calgary?",
        "answer": "Yes, a padded cover with a 10 mm foam core helps.", "key_facts": ["10 mm foam"], "problems": []}))
    out = tmp_path / "site"
    site.build(cat, out, approved)
    for rel in ["index.html", "robots.txt", "sitemap.xml", "llms.txt", "llms-full.txt",
                "feeds/openai-products.jsonl", "feeds/google-merchant.tsv", "hail-cover-sedan/index.html"]:
        assert (out / rel).is_file(), rel
    assert "Can a car cover stop hail in Calgary?" in (out / "hail-cover-sedan/index.html").read_text()

    (approved / "b.json").write_text(json.dumps({"product_id": "FG-HC-01", "question": "q", "answer": "a",
                                                 "key_facts": [], "problems": ["numbers not in catalog: ['99']"]}))
    with pytest.raises(ValueError, match="grounding check"):
        site.build(cat, out, approved)


def test_rating_requires_reviews(cat):
    data = cat.model_dump(mode="json")
    data["products"][0]["rating_value"] = 4.8
    with pytest.raises(ValueError, match="rating_value and review_count"):
        catalog.Catalog.model_validate(data)


def test_duplicate_ids_rejected(cat):
    data = cat.model_dump(mode="json")
    data["products"].append(dict(data["products"][0], slug="other"))
    with pytest.raises(ValueError, match="duplicate product id"):
        catalog.Catalog.model_validate(data)


def test_numbers_in():
    assert catalog.numbers_in("costs 1,299.00 CAD, 4.50 kg, 10 mm") == {"1299", "4.5", "10"}


@respx.mock
def test_indexnow(cat):
    route = respx.post(indexnow.ENDPOINT).mock(return_value=httpx.Response(202))
    assert indexnow.submit(cat, "abc123") == 202
    sent = json.loads(route.calls[0].request.content)
    assert sent["host"] == "www.foothillgear.example"
    assert sent["keyLocation"] == "https://www.foothillgear.example/abc123.txt"
    with pytest.raises(ValueError, match="must be on"):
        indexnow.payload(cat, "k", ["https://elsewhere.example/x"])
