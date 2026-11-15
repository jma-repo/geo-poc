"""Parser tests use response shapes from each API's public documentation.
They prove the mapping logic, not that the live APIs still answer this way: run `--check` for that."""
import json
import os
import shutil
import subprocess

import httpx
import pytest
import respx

from collectors import run, sources


@pytest.fixture
def http():
    return sources.Http(delay=0)


@respx.mock
def test_arctic_shift_posts_and_comments(http):
    route = respx.get("https://arctic-shift.photon-reddit.com/api/posts/search").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "abc", "title": "Where can I buy a hail cover?", "selftext": "Parking outside",
                  "author": "someone", "permalink": "/r/Calgary/comments/abc/x/", "created_utc": 1762000000}]}))
    respx.get("https://arctic-shift.photon-reddit.com/api/comments/search").mock(return_value=httpx.Response(200, json={
        "data": [{"id": "c1", "body": "I wish there was a cheaper one", "author": "other",
                  "link_id": "t3_abc", "created_utc": 1762000100}]}))
    rows = list(sources.arctic_shift(http, ["Calgary"], ["hail"], 7))
    assert [r["external_id"] for r in rows] == ["abc", "c_c1"]
    assert rows[0]["url"] == "https://www.reddit.com/r/Calgary/comments/abc/x/"
    assert rows[1]["url"] == "https://www.reddit.com/r/Calgary/comments/abc/_/c1/"
    assert all(r["geo_hint"] == "calgary" for r in rows)
    assert rows[0]["author_hash"] and "someone" not in json.dumps(rows)
    assert route.calls[0].request.url.params["query"] == "hail"


@respx.mock
def test_hn(http):
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(return_value=httpx.Response(200, json={"hits": [
        {"objectID": "1", "title": None, "story_title": "Ask HN: winter gear", "comment_text": "Need boots",
         "author": "a", "created_at_i": 1762000000}]}))
    (r,) = sources.hn(http, ["boots"], 7)
    assert r["title"] == "Ask HN: winter gear" and r["body"] == "Need boots"
    assert r["posted_at"].startswith("2025-11-01")


@respx.mock
def test_stackexchange_uses_key_when_set(http, monkeypatch):
    monkeypatch.setenv("STACKEXCHANGE_KEY", "k")
    route = respx.get("https://api.stackexchange.com/2.3/search/advanced").mock(return_value=httpx.Response(200, json={
        "items": [{"question_id": 9, "title": "Protect car from hail?", "body": "<p>How?</p>",
                   "link": "https://diy.stackexchange.com/q/9", "owner": {"user_id": 5}, "creation_date": 1762000000}]}))
    (r,) = sources.stackexchange(http, ["diy"], ["hail"], 7)
    assert r["source"] == "stackexchange:diy" and r["external_id"] == "9"
    assert route.calls[0].request.url.params["key"] == "k"


@respx.mock
def test_autocomplete_expands_a_to_z(http):
    route = respx.get("https://suggestqueries.google.com/complete/search").mock(
        side_effect=lambda req: httpx.Response(200, json=[req.url.params["q"], [req.url.params["q"] + " suv"]]))
    rows = list(sources.autocomplete(http, ["hail cover for"]))
    assert len(route.calls) == 27 and len(rows) == 27
    assert rows[1]["title"] == "hail cover for a suv"


@respx.mock
def test_bluesky(http):
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(return_value=httpx.Response(200, json={
        "posts": [{"uri": "at://did:plc:x/app.bsky.feed.post/3k", "author": {"handle": "a.bsky.social"},
                   "record": {"text": "Any good ice cleats?", "createdAt": "2026-11-01T10:00:00.000Z"}}]}))
    (r,) = sources.bluesky(http, ["ice cleats"], 7)
    assert r["url"] == "https://bsky.app/profile/a.bsky.social/post/3k"


@respx.mock
def test_youtube_skips_videos_with_comments_disabled(http, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "y")
    api = "https://www.googleapis.com/youtube/v3"
    respx.get(f"{api}/search").mock(return_value=httpx.Response(200, json={"items": [
        {"id": {"videoId": "v1"}, "snippet": {"title": "Hail cover test"}},
        {"id": {"videoId": "v2"}, "snippet": {"title": "No comments"}}]}))
    respx.get(f"{api}/commentThreads", params={"videoId": "v1"}).mock(return_value=httpx.Response(200, json={"items": [
        {"id": "t1", "snippet": {"topLevelComment": {"snippet": {"textDisplay": "Does it fit a RAV4?",
                                                                    "authorChannelId": {"value": "ch"},
                                                                    "publishedAt": "2026-11-02T00:00:00Z"}}}}]}))
    respx.get(f"{api}/commentThreads", params={"videoId": "v2"}).mock(return_value=httpx.Response(403))
    rows = list(sources.youtube(http, ["hail cover"], 7))
    assert [r["external_id"] for r in rows] == ["t1"]


@respx.mock
def test_retry_on_429_then_success(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    route = respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "1"}), httpx.Response(200, json={"hits": []})])
    assert list(sources.hn(sources.Http(delay=0), ["x"], 1)) == []
    assert route.call_count == 2


@respx.mock
def test_one_failing_source_does_not_stop_others(http, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(return_value=httpx.Response(200, json={"hits": [
        {"objectID": "1", "title": "t", "author": "a", "created_at_i": 1762000000}]}))
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(return_value=httpx.Response(403))
    cfg = {"queries": ["x"]}
    rows, report = run.collect(["hn", "bluesky", "youtube"], cfg, 7, http)
    assert len(rows) == 1
    assert report["hn"] == "ok: 1 rows"
    assert report["bluesky"].startswith("error: HTTPStatusError")
    assert report["youtube"] == "skipped: set YOUTUBE_API_KEY"


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL") or not shutil.which("psql"), reason="needs TEST_DATABASE_URL")
def test_load_postgres_dedupes_and_handles_large_batches():
    url = os.environ["TEST_DATABASE_URL"]
    schema = open(os.path.join(os.path.dirname(__file__), "../../db/schema.sql")).read()
    subprocess.run(["psql", url, "-q", "-v", "ON_ERROR_STOP=1"], input="DROP TABLE IF EXISTS conversions, partner_codes, "
                   "outreach_drafts, waitlist, merchants, opportunities, pain_points, raw_signals CASCADE;" + schema,
                   text=True, check=True, capture_output=True)
    rows = [sources.row("hackernews", i, None, "t", "x" * 8000 + "$rows$ 'quote'", "a", None, 1762000000) for i in range(1200)]
    assert run.load_postgres(rows, url) == 1200   # ~10 MB, three batches, body contains the default dollar tag
    assert run.load_postgres(rows[:10], url) == 0  # duplicates ignored
